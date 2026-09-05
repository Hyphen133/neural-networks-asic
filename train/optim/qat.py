"""Parametrised quantisation-aware trainer for the tt_um_wakeword classifier.

A superset of ``train/train_sheila.py``: identical hardware semantics (ternary
weights, per-frame saturating accumulator, ``clamp(acc >> HSHIFT, 0, 15)``
requantiser, ternary output layer, max over the staggered windows), with every
training-side choice exposed as a field of :class:`Cfg` so a grid search can
move one dimension at a time.

With :func:`Cfg.baseline` the results match ``train_sheila.py`` run with the
shipped flags, which is what ``--selftest`` checks. Everything beyond that is
opt-in, so a regression in this file cannot silently change the tape-out
recipe.

Dimensions split into three cost classes, and the class matters more than the
gain:

* **free** -- training only (epochs, schedule, pooling, augmentation, seed).
  Nothing about the silicon changes.
* **near-free** -- constants baked into the weight ROM (``nframe``, ``shift``,
  ``feat_off``, ``WL``). No extra flip-flops; combinational area only, so an
  ``area_check.sh`` run decides.
* **costly** -- ``H``, ``accw``, ``nphase`` add flip-flops at ~73 um^2 each on
  this flow, and the shipped design already sits at 95.9 % core utilisation.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, asdict, replace, fields

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wwhw  # noqa: E402
from train_sheila import QW, SatSTE, auc, operating_points, emit_weights  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")

HID_MAX = 15          # requantiser saturates at 15: the hidden value is 4 bits
EVAL_CHUNK = 32768


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Cfg:
    """One experiment. Frozen so a config can key a result cache."""

    # --- data / architecture (costly or near-free in silicon) ---
    tag: str = "sheila_hw"
    stats: str = ""                  # NSTAT: subset of a fe_stats.py extraction's
                                     # per-frame statistics, e.g. "max,ema3".
                                     # "" = whatever the extraction holds.
    H: int = 4                       # NHID          costly
    WL: int = 1                      # 1=ternary 3=int3 7=int4   near-free
    shift: int = 1                   # HSHIFT        free constant
    nphase: int = 2                  # NPHASE        costly
    nframe: int = 16                 # NFRAME        near-free (ROM only)
    accw: int = 6                    # HACC_W        costly
    feat_off: int = -1               # -1 = train-set mean, as shipped

    # --- optimisation (free) ---
    epochs: int = 250
    warmup: float = 0.4              # fraction of epochs on mean pooling
    batch: int = 1024
    seed: int = 0
    lr_w1: float = 0.08
    lr_b1: float = 0.5
    lr_w2: float = 0.02
    lr_b: float = 0.02
    lr_t: float = 0.01
    wd: float = 0.0
    sched: str = "cos"               # cos | cos_warm | const
    leak: float = 0.1                # requantiser dead-region gradient

    # --- pooling / loss (free) ---
    pool: str = "lse"                # lse | mean | max | topk
    topk: int = 2
    label_smooth: float = 0.0
    pos_weight: float = 0.0          # 0 = off

    # --- augmentation (free) ---
    train_hop: int = 0               # 0 = hardware hop; 1 = every start
    aug_level: float = 0.0           # P(per-clip +-1 level shift)
    aug_band: float = 0.0            # P(per-band +-1 level shift)
    aug_time: int = 0                # +-frames of extra window jitter
    aug_mix: float = 0.0             # mixup alpha on features (0 = off)

    # --- selection / averaging (free) ---
    eval_every: int = 5
    ema: float = 0.0                 # 0 = off; else decay on latent weights

    # --- distillation (free) ---
    kd: str = ""                     # teacher npz path
    kd_alpha: float = 0.5
    kd_temp: float = 2.0

    @staticmethod
    def baseline(tag: str = "sheila_hw", **kw) -> "Cfg":
        """Exactly what train_sheila.py does with the shipped flags."""
        return Cfg(tag=tag, eval_every=25, **kw)

    def key(self) -> str:
        """Stable identity of this experiment, used to resume a grid."""
        base = Cfg(tag=self.tag)
        diff = {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) != getattr(base, f.name)}
        return json.dumps(diff, sort_keys=True)

    def label(self) -> str:
        base = Cfg(tag=self.tag)
        diff = [f"{f.name}={getattr(self, f.name)}" for f in fields(self)
                if getattr(self, f.name) != getattr(base, f.name) and f.name != "seed"]
        return ",".join(diff) or "baseline"


# ---------------------------------------------------------------------------
# Requantiser with a configurable leak (train_sheila's RQ has it as a constant)
# ---------------------------------------------------------------------------
class RQLeak(torch.autograd.Function):
    """clamp(acc >> shift, 0, hi) forward; leaky straight-through backward."""

    @staticmethod
    def forward(ctx, a, s, hi, leak):
        ctx.save_for_backward(a)
        ctx.s = float(1 << s)
        ctx.hi = hi
        ctx.leak = leak
        return torch.clamp(torch.floor(a / ctx.s), 0, hi)

    @staticmethod
    def backward(ctx, g):
        (a,) = ctx.saved_tensors
        z = a / ctx.s
        live = ((z >= 0) & (z <= ctx.hi)).float()
        return g * (live + ctx.leak * (1 - live)), None, None, None


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
class Data:
    """Cached features for one tag, kept on the device and shared by every run.

    Windows are cut on the fly so a run can use a finer training hop than the
    hardware one without a second copy of the tensor.
    """

    _cache: dict[str, "Data"] = {}

    def __init__(self, tag: str, device: str, stats: str = ""):
        d = np.load(os.path.join(ART, f"ww_feats_{tag}.npz"), allow_pickle=True)
        self.tag = tag
        self.raw = d["feats"]                          # (N, T, NB) uint8
        self.labels = d["labels"]
        self.splits = d["splits"]
        # extract_clips.py --silence writes a NEGATIVE index for each of its
        # synthetic room-tone negatives (extract_clips.py:103). extract.py
        # writes no index column at all, hence the guard.
        self.index = d["index"] if "index" in d.files else None
        self.cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
        if stats:
            # A train/optim/fe_stats.py extraction carries several per-frame
            # statistics laid out band-major as [band][statistic]. Keep only the
            # ones this configuration's silicon computes: an NSTAT=2 build reads
            # the max and one average, not the whole set the sweep extracted.
            have = [str(s) for s in d["stats"]]
            sel = [s.strip() for s in stats.split(",") if s.strip()]
            missing = [s for s in sel if s.partition("@")[0] not in have]
            if missing:
                raise SystemExit(f"stats {missing} not in {tag} extraction {have}")
            nb = self.cfg.nband // len(have)
            # "max,smean6@3-5" is every band's max plus the mean of bands 3..5,
            # which is the RTL's AVG_N: only the deepest AVG_N taps carry an
            # accumulator. Columns come out in the RTL's order -- all the maxima
            # in band order, then the means in band order -- so a header emitted
            # from this trains and decodes against the same layout.
            cols = []
            for s in sel:
                name, _, where = s.partition("@")
                for b in range(nb):
                    if where and where != "all":
                        lo, _, hi = where.partition("-")
                        if not (int(lo) <= b <= int(hi or lo)):
                            continue
                    cols.append(b * len(have) + have.index(name))
            self.raw = self.raw[:, :, cols]
            self.cfg.nband = len(cols)
        self.dev = torch.device(device)
        self.X = torch.from_numpy(self.raw.astype(np.float32)).to(self.dev)
        self.y = torch.from_numpy((self.labels > 0).astype(np.float32))[:, None].to(self.dev)
        self.is_pos = self.labels > 0
        self.tr = np.where(self.splits == 0)[0]
        self.va = self.splits == 1
        self.te = self.splits == 2
        # Clips from a real corpus. The synthetic negatives are a fixed 2000
        # hash-split ~10/10/80, so ~202 land in validation and ~202 in test
        # whatever the split sizes are -- which makes them a far larger share
        # of the smaller split (67.6 % of catmeow's validation negatives
        # against 33.6 % of its test negatives) and inflates validation by an
        # amount that has nothing to do with the detector. See
        # docs/val_test_gap.md.
        self.real = (self.index >= 0) if self.index is not None \
            else np.ones(len(self.labels), dtype=bool)
        self.feat_max = self.cfg.feat_max

    @classmethod
    def get(cls, tag: str, device: str, stats: str = "") -> "Data":
        k = f"{tag}|{stats}@{device}"
        if k not in cls._cache:
            cls._cache[k] = cls(tag, device, stats)
        return cls._cache[k]

    def starts(self, nframe: int, hop: int) -> list[int]:
        return list(range(0, self.raw.shape[1] - nframe + 1, hop))

    def windows(self, x: torch.Tensor, nframe: int, starts) -> torch.Tensor:
        """(n, T, NB) -> (n, W, nframe, NB)."""
        return torch.stack([x[:, s:s + nframe] for s in starts], 1)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def _build(c: Cfg, data: Data, x0: torch.Tensor):
    """Parameters plus the hardware-exact forward. x0 primes the bias median."""
    dev = data.dev
    NB = data.cfg.nband
    bias_lim = ((1 << (c.accw - 1)) - 1) if c.accw else 0

    w1 = nn.Parameter(torch.empty(c.H, c.nframe, NB, device=dev).uniform_(-1.2, 1.2))
    with torch.no_grad():
        a0 = torch.einsum('nwfb,hfb->nwh', x0, QW.apply(w1, c.WL))
        b1v = -a0.reshape(-1, c.H).median(0).values
        if bias_lim:
            b1v = b1v.clamp(-bias_lim - 1, bias_lim)
    b1 = nn.Parameter(b1v.clone())
    w2 = nn.Parameter(torch.tensor([[1.0 if h % 2 == 0 else -1.0 for h in range(c.H)]],
                                   device=dev))
    b = nn.Parameter(torch.zeros(1, device=dev))

    def fwd(x):
        q1 = QW.apply(w1, c.WL)
        if c.accw:
            # Frame by frame, saturating each step -- exactly the RTL's loop.
            lim = (1 << (c.accw - 1)) - 1
            a = torch.round(b1).expand(x.shape[0], x.shape[1], -1)
            for f in range(x.shape[2]):
                a = SatSTE.apply(a + torch.einsum('nwb,hb->nwh', x[:, :, f, :],
                                                  q1[:, f, :]), lim)
        else:
            a = torch.einsum('nwfb,hfb->nwh', x, q1) + b1
        h = RQLeak.apply(a, c.shift, HID_MAX, c.leak)
        return torch.einsum('nwh,kh->nwk', h, QW.apply(w2, c.WL)) + b

    groups = [{'params': [w1], 'lr': c.lr_w1, 'weight_decay': c.wd},
              {'params': [b1], 'lr': c.lr_b1},
              {'params': [w2], 'lr': c.lr_w2, 'weight_decay': c.wd},
              {'params': [b], 'lr': c.lr_b}]
    return groups, fwd, [w1, w2], [w1, b1, w2, b], bias_lim


def _pool(s: torch.Tensor, c: Cfg, mean_phase: bool) -> torch.Tensor:
    """Bag score over window positions. s: (n, W, 1)."""
    if mean_phase or c.pool == "mean":
        return s.mean(1)
    if c.pool == "max":
        return s.max(1).values
    if c.pool == "topk":
        k = min(c.topk, s.shape[1])
        return s.topk(k, dim=1).values.mean(1)
    return s.logsumexp(1) - math.log(s.shape[1])


def _augment(xb: torch.Tensor, c: Cfg, feat_max: int, gen) -> torch.Tensor:
    """Level jitter on the raw (un-centred) feature map. xb: (n, T, NB)."""
    if c.aug_level > 0:
        n = xb.shape[0]
        pick = torch.rand(n, 1, 1, device=xb.device, generator=gen) < c.aug_level
        step = torch.where(torch.rand(n, 1, 1, device=xb.device, generator=gen) < 0.5,
                           -1.0, 1.0)
        xb = xb + pick * step
    if c.aug_band > 0:
        n, _, nb = xb.shape
        pick = torch.rand(n, 1, nb, device=xb.device, generator=gen) < c.aug_band
        step = torch.where(torch.rand(n, 1, nb, device=xb.device, generator=gen) < 0.5,
                           -1.0, 1.0)
        xb = xb + pick * step
    return xb.clamp_(0, feat_max)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
@torch.no_grad()
def _score_all(fwd, data: Data, c: Cfg, starts, centre: float) -> np.ndarray:
    """Hardware score for every clip: max over the staggered windows."""
    out = []
    for i in range(0, data.X.shape[0], EVAL_CHUNK):
        xb = data.X[i:i + EVAL_CHUNK] - centre
        out.append(fwd(data.windows(xb, c.nframe, starts)).max(1).values[:, 0])
    return torch.cat(out).cpu().numpy()


def run(c: Cfg, device: str = "cuda", verbose: bool = False) -> dict:
    """Train one configuration; returns metrics and the selected tensors."""
    data = Data.get(c.tag, device, c.stats)
    torch.manual_seed(c.seed)
    np.random.seed(c.seed)
    gen = torch.Generator(device=data.dev).manual_seed(c.seed + 977)

    centre = float(c.feat_off if c.feat_off >= 0
                   else int(round(data.raw[data.splits == 0].mean())))

    hop = c.nframe // c.nphase
    eval_starts = data.starts(c.nframe, hop)
    train_hop = c.train_hop or hop
    train_starts = data.starts(c.nframe, train_hop)
    lo, hi = 0, data.raw.shape[1] - c.nframe

    idx = data.tr
    x0 = data.windows(data.X[idx[:4096]] - centre, c.nframe, eval_starts)
    groups, fwd, clampers, tensors, bias_lim = _build(c, data, x0)
    lt = nn.Parameter(torch.tensor(2.0, device=data.dev))
    groups.append({'params': [lt], 'lr': c.lr_t})
    opt = torch.optim.Adam(groups)
    if c.sched == "cos":
        sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, c.epochs)
    elif c.sched == "cos_warm":
        sch = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            opt, max(c.epochs // 4, 1))
    else:
        sch = torch.optim.lr_scheduler.ConstantLR(opt, factor=1.0, total_iters=1)

    Y = data.y
    if c.kd:
        tl = torch.from_numpy(np.load(c.kd)["logits"]).to(data.dev)[:, None]
        if tl.shape[0] != Y.shape[0]:
            raise SystemExit(f"teacher {c.kd} has {tl.shape[0]} clips, features "
                             f"have {Y.shape[0]}")
        Y = (1 - c.kd_alpha) * Y + c.kd_alpha * torch.sigmoid(tl / c.kd_temp)
    if c.label_smooth:
        Y = Y * (1 - c.label_smooth) + 0.5 * c.label_smooth
    pw = torch.tensor(c.pos_weight, device=data.dev) if c.pos_weight else None

    ema = [t.detach().clone() for t in tensors] if c.ema else None
    nwarm = int(c.warmup * c.epochs)
    best = (-1.0, None, -1)
    hist = []
    for ep in range(c.epochs):
        mean_phase = ep < nwarm
        perm = np.random.permutation(idx)
        for i in range(0, len(idx), c.batch):
            j = perm[i:i + c.batch]
            xb = data.X[j]
            if c.aug_level or c.aug_band:
                xb = _augment(xb.clone(), c, data.feat_max, gen)
            st = train_starts
            if c.aug_time:
                off = int(torch.randint(-c.aug_time, c.aug_time + 1, (1,),
                                        device=data.dev, generator=gen).item())
                st = [min(max(s + off, lo), hi) for s in train_starts]
            xw = data.windows(xb - centre, c.nframe, st)
            yb = Y[j]
            if c.aug_mix > 0:
                lam = float(np.random.beta(c.aug_mix, c.aug_mix))
                k = torch.randperm(xw.shape[0], device=data.dev, generator=gen)
                xw = lam * xw + (1 - lam) * xw[k]
                yb = lam * yb + (1 - lam) * yb[k]
            pooled = _pool(fwd(xw), c, mean_phase)
            loss = F.binary_cross_entropy_with_logits(pooled / torch.exp(lt), yb,
                                                      pos_weight=pw)
            opt.zero_grad()
            loss.backward()
            opt.step()
            with torch.no_grad():
                for t in clampers:
                    t.clamp_(-(c.WL + 0.5), c.WL + 0.5)
                if bias_lim:
                    tensors[1].clamp_(-bias_lim - 1, bias_lim)
                if ema is not None:
                    for e, t in zip(ema, tensors):
                        e.mul_(c.ema).add_(t.detach(), alpha=1 - c.ema)
        sch.step()
        if (ep + 1) % c.eval_every == 0 or ep == c.epochs - 1:
            probe = ema if (ema is not None and ep >= nwarm) else None
            if probe is not None:
                saved = [t.detach().clone() for t in tensors]
                with torch.no_grad():
                    for t, e in zip(tensors, probe):
                        t.copy_(e)
            sv = _score_all(fwd, data, c, eval_starts, centre)
            a = auc(sv[data.va], data.is_pos[data.va])
            if probe is not None:
                with torch.no_grad():
                    for t, s in zip(tensors, saved):
                        t.copy_(s)
            hist.append((ep + 1, round(a, 5)))
            if a > best[0]:
                best = (a, [t.detach().clone() for t in tensors], ep + 1)
            if verbose:
                print(f"  ep {ep+1:4d}  loss {loss.item():.4f}  val AUC {a*100:5.1f}%")

    with torch.no_grad():
        for t, bv in zip(tensors, best[1]):
            t.copy_(bv)
    sv = _score_all(fwd, data, c, eval_starts, centre)
    va_auc, te_auc = best[0], auc(sv[data.te], data.is_pos[data.te])
    # `va_auc` is a MAXIMUM over len(hist) checkpoints, so it is optimistically
    # biased; `te_auc` is read once, at the checkpoint that maximum chose. Part
    # of every val-test gap is therefore manufactured by the metric rather than
    # by distribution shift. Recording the last checkpoint's val AUC makes the
    # two separable: val_final is unbiased at a checkpoint nothing selected, so
    # (val_auc - val_final) bounds the max-picking inflation, and best_epoch
    # says whether the winner was a real late gain or an early fluke that 200
    # draws happened to surface.
    val_final = hist[-1][1] if hist else float("nan")
    # The same two AUCs with the synthetic room-tone negatives dropped, so both
    # splits are scored against real audio only. Their difference is the part
    # of the val-test gap that is mixture rather than detector; comparing it
    # with (va_auc - te_auc) says how much of the headline gap is an artefact
    # of a fixed-size easy negative class landing in a small validation split.
    va_r, te_r = data.va & data.real, data.te & data.real
    val_nosil = auc(sv[va_r], data.is_pos[va_r]) if va_r.sum() > 4 else float("nan")
    test_nosil = auc(sv[te_r], data.is_pos[te_r]) if te_r.sum() > 4 else float("nan")
    ops, eph = operating_points(sv[data.te], data.is_pos[data.te], hop,
                                data.cfg.frame_ms)
    vops, _ = operating_points(sv[data.va], data.is_pos[data.va], hop,
                               data.cfg.frame_ms, (1.0,))
    return dict(cfg=asdict(c), key=c.key(), label=c.label(), seed=c.seed,
                val_auc=va_auc, test_auc=te_auc, best_epoch=best[2],
                val_final=val_final, val_nosil=val_nosil, test_nosil=test_nosil,
                centre=int(centre), nwin=len(eval_starts), eph=eph,
                recall_1fa=ops[1]["recall"], thr=int(np.floor(vops[0]["thr"])),
                hist=hist, tensors=[t.detach().cpu().numpy() for t in tensors])


def run_seeds(c: Cfg, seeds, device: str = "cuda") -> dict:
    """Same config over several seeds. Selection uses the mean validation AUC.

    Reporting the mean over seeds rather than the best is what keeps a wide
    grid honest: with ~1 300 validation clips a single run's AUC moves by about
    a point on noise alone, so a best-of-N number mostly measures N.
    """
    rs = [run(replace(c, seed=int(s)), device) for s in seeds]
    va = np.array([r["val_auc"] for r in rs])
    te = np.array([r["test_auc"] for r in rs])
    pick = rs[int(va.argmax())]
    return dict(key=c.key(), label=c.label(), n=len(rs), seeds=[int(s) for s in seeds],
                val_mean=float(va.mean()), val_std=float(va.std()),
                val_max=float(va.max()), test_mean=float(te.mean()),
                test_std=float(te.std()),
                test_at_best_val=float(pick["test_auc"]), best_seed=int(pick["seed"]),
                # Selection diagnostics: val_final is the last checkpoint's val
                # AUC (nothing selected it), best_epoch says where the winning
                # checkpoint actually sat. Together they separate max-picking
                # inflation from genuine distribution shift.
                val_final_mean=float(np.mean([r["val_final"] for r in rs])),
                best_epoch_mean=float(np.mean([r["best_epoch"] for r in rs])),
                # Both splits scored against real audio only; the gap between
                # these two is the honest one. docs/val_test_gap.md 3.
                val_nosil_mean=float(np.mean([r["val_nosil"] for r in rs])),
                test_nosil_mean=float(np.mean([r["test_nosil"] for r in rs])),
                cfg=asdict(c), per_seed=[dict(seed=r["seed"], val=r["val_auc"],
                                              test=r["test_auc"],
                                              final=r["val_final"]) for r in rs])


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def emit(res: dict, path: str, name: str) -> str:
    """Write a ww_weights*.svh from a :func:`run` result."""
    c = Cfg(**res["cfg"])
    t = res["tensors"]
    W1 = np.clip(np.round(t[0]), -c.WL, c.WL).astype(np.int64)
    W2 = np.clip(np.round(t[2]), -c.WL, c.WL).astype(np.int64).reshape(-1)
    HB = np.round(t[1]).astype(np.int64)
    bo = float(np.asarray(t[3]).reshape(-1)[0])
    thr = int(np.floor(res["thr"] - bo))
    emit_weights(W1, HB, W2, thr, c.accw or 8, 10, path,
                 f"{name}, H={c.H} WL={c.WL}, AUC {res['test_auc']*100:.1f}%, "
                 f"nphase={c.nphase} shift={c.shift} centre={res['centre']} "
                 f"nframe={c.nframe}")
    return path


def _selftest(device: str) -> None:
    """Cfg.baseline must reproduce train_sheila.py's shipped recipe."""
    r = run(Cfg.baseline("sheila_hw", H=4, WL=1, shift=1, nphase=2, accw=6,
                         epochs=250, seed=3), device, verbose=True)
    print(f"sheila baseline seed 3: val {r['val_auc']*100:.2f}%  "
          f"test {r['test_auc']*100:.2f}%   (train_sheila.py: val 87.9  test 89.93)")


if __name__ == "__main__":
    _selftest(sys.argv[1] if len(sys.argv) > 1 else "cuda")
