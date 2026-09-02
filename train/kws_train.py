"""Quantisation-aware training for the wake-word tile's int4 classifier.

    python3 train/kws_train.py --sweep            # pick a config on the data
    python3 train/kws_train.py --config best      # train + save the winner
    python3 train/kws_train.py --gain-sweep       # accuracy vs microphone level

The model is deliberately trivial -- one int4 linear layer over NBAND*NSLOT
integer log-magnitude features -- because that is all a 1x1 tile has room for
once the filterbank and slot registers are paid for. Everything interesting is
in the objective, not the architecture:

  * Features are already integers straight out of the bit-exact front end. No
    input scaling is learned or applied, so the trained weights ARE the
    silicon weights; the only quantisation is on the weights themselves.

  * A keyword clip is a bag of frames and only one of them needs to fire, so
    keyword loss is taken at the single best-scoring frame (multiple-instance
    learning). Background clips get loss at EVERY frame, because a false
    trigger anywhere in a clip is a false trigger. That asymmetry is what
    keeps the LED dark in a noisy room.

  * `hold_frames` latching and the ui trim nibble are evaluated after
    training through `kws_hw`, on integers, so reported numbers are what the
    gates will do -- not a float approximation of it.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kws_data as D                                            # noqa: E402
from kws_hw import (KWSConfig, W_MIN, W_MAX, classify_int,       # noqa: E402
                    clip_decision_batch, save_weights, bank_report)

DEV = "cpu"          # the whole model is <100 weights; a GPU would be slower


# ---------------------------------------------------------------------------
# Quantised linear classifier
# ---------------------------------------------------------------------------
class RoundSTE(torch.autograd.Function):
    """round() with a straight-through gradient."""

    @staticmethod
    def forward(ctx, x):
        return torch.round(x)

    @staticmethod
    def backward(ctx, g):
        return g


def quant_w(w):
    """Fake-quantise to the int4 range the RTL's weight ROM stores."""
    return torch.clamp(RoundSTE.apply(w), W_MIN, W_MAX)


class TileClassifier(nn.Module):
    """score[c] = bias[c] + sum_f w[c,f] * feat[f], all integers on chip.

    `logit_scale` only softens the softmax during training; it never appears
    in hardware because argmax is scale-invariant.
    """

    def __init__(self, cfg: KWSConfig, logit_scale: float | None = None):
        super().__init__()
        self.cfg = cfg
        self.w = nn.Parameter(torch.zeros(cfg.nclass, cfg.nfeat))
        self.b = nn.Parameter(torch.zeros(cfg.nclass))
        nn.init.uniform_(self.w, -1.0, 1.0)
        # Integer features (0..feat_max) times int4 weights put raw scores in
        # the hundreds. Feeding that straight to softplus/softmax leaves the
        # loss in its linear regime, where the gradient carries no information
        # about *how* wrong a margin is and training collapses to always-
        # background. Normalise by a typical score magnitude instead.
        self.logit_scale = logit_scale or max(
            1.0, cfg.nfeat * cfg.feat_max * W_MAX / 24.0)

    def forward(self, feats):
        """feats (B, T, NFEAT) float -> logits (B, T, NCLASS)."""
        wq = quant_w(self.w)
        bq = RoundSTE.apply(self.b * self.bias_unit)
        return (feats @ wq.T + bq) / self.logit_scale

    # Biases need a much wider range than weights (they offset a sum of up to
    # NFEAT*15*8), so they are parameterised in units of `bias_unit` to keep
    # the optimiser's step size sane.
    bias_unit = 16.0

    def int_weights(self):
        with torch.no_grad():
            w = quant_w(self.w).to(torch.int64).cpu().numpy()
            b = RoundSTE.apply(self.b * self.bias_unit).to(torch.int64).cpu().numpy()
        return w.reshape(self.cfg.nclass, self.cfg.nslot, self.cfg.nband), b


# ---------------------------------------------------------------------------
# Objective
# ---------------------------------------------------------------------------
def ce_max_loss(logits, labels, nclass: int, bg_weight: float = 1.0):
    """Cross-entropy on the max-over-frames logits.

    Measured best of the objectives tried for this model (see docs/kws.md):
    per-frame cross-entropy trains a model that is right on most frames and
    still fires on most clips, and a pure softplus margin loss collapses to
    always-background once int4 weights put the margins in the hundreds. This
    keeps a softmax (so the scale is normalised away) while scoring the clip
    the way the tile does.
    """
    mx = logits.max(dim=1).values                       # (B, NCLASS)
    wgt = logits.new_ones(nclass)
    wgt[nclass - 1] = bg_weight
    return F.cross_entropy(mx, labels, weight=wgt)


def mil_loss(logits, labels, nclass: int, bg_weight: float = 8.0):
    """Loss on the statistic the tile actually thresholds.

    The chip fires keyword c on a clip when ANY frame's score for c beats the
    background score, so the decision variable is

        margin_c = max over frames of (score_c - score_bg)

    and the loss is written on exactly that, not on per-frame cross-entropy.
    Measured on the validation split, optimising per-frame cross-entropy
    instead costs roughly half the recall at a fixed false-alarm rate: a model
    can be right on 95 % of frames and still fire on 58 % of clips, because a
    clip is 17 frames and one is enough.

    Background clips are weighted up because their frames must ALL stay below
    zero while a keyword clip needs only its best frame above it.
    """
    margin = (logits[..., :nclass - 1] - logits[..., nclass - 1:nclass])
    mx = margin.max(dim=1).values                       # (B, nclass-1)
    is_bg = labels == (nclass - 1)
    loss = logits.new_zeros(())

    if is_bg.any():
        loss = loss + bg_weight * F.softplus(mx[is_bg]).mean()

    if (~is_bg).any():
        m, y = mx[~is_bg], labels[~is_bg]
        tgt = m.gather(1, y[:, None]).squeeze(1)
        loss = loss + F.softplus(-tgt).mean()
        if nclass > 2:                                  # and beat the rival keyword
            other = m.clone().scatter_(1, y[:, None], -1e4)
            loss = loss + 0.5 * F.softplus(other.max(dim=1).values - tgt).mean()

    return loss


def roc(feats, labels, w, bias, cfg: KWSConfig,
        fa_targets=(0.01, 0.02, 0.05, 0.10, 0.20)):
    """Recall at fixed clip-level false-alarm rates, on the integer path.

    Raising the background bias is exactly what the ui trim nibble does, so
    this curve is the operating range a user can dial in live rather than a
    training-time abstraction.
    """
    _, scores = classify_int(feats, w, bias, cfg)
    margin = (scores[..., :cfg.nclass - 1]
              - scores[..., cfg.nclass - 1:cfg.nclass]).max(axis=1)
    bg = labels == cfg.nclass - 1
    fire = margin.max(axis=1)
    rows = []
    for fa in fa_targets:
        thr = np.quantile(fire[bg], 1 - fa)
        rec = []
        for c in range(cfg.nclass - 1):
            sel = labels == c
            ok = margin[sel, c] > thr
            if cfg.nclass > 2:
                ok = ok & (margin[sel, c] >= margin[sel].max(axis=1))
            rec.append(float(ok.mean()) if sel.any() else float("nan"))
        rows.append((fa, rec, float(thr)))
    return rows


def closed_set(feats, labels, w, bias, cfg: KWSConfig):
    """Accuracy when the clip is known to contain one of the keywords.

    This drops open-set rejection, which the measurements show is what the
    front end cannot do; it is reported alongside the ROC so the two failure
    modes are not conflated.
    """
    _, scores = classify_int(feats, w, bias, cfg)
    kw = scores[..., :cfg.nclass - 1].max(axis=1)        # (N, nclass-1)
    sel = labels < cfg.nclass - 1
    pred = kw[sel].argmax(axis=1)
    true = labels[sel]
    acc = float((pred == true).mean())
    rec = [float((pred[true == c] == c).mean()) for c in range(cfg.nclass - 1)]
    return acc, rec


# ---------------------------------------------------------------------------
# Integer evaluation -- exactly what the tile decides
# ---------------------------------------------------------------------------
def evaluate(feats, labels, w, bias, cfg: KWSConfig, trim: int = 0):
    """Clip-level confusion over the integer path.

    Returns (accuracy, per-keyword recall, false-alarm rate on background).
    """
    preds = clip_decision_batch(feats, w, bias, cfg, trim)
    acc = float((preds == labels).mean())
    recall = [float((preds[labels == c] == c).mean()) if (labels == c).any()
              else float("nan") for c in range(cfg.nclass - 1)]
    bg = labels == cfg.nclass - 1
    far = float((preds[bg] != cfg.nclass - 1).mean()) if bg.any() else float("nan")
    return acc, recall, far


def frame_accuracy(feats, labels, w, bias, cfg: KWSConfig):
    cls, _ = classify_int(feats, w, bias, cfg)
    return float((cls == labels[:, None]).mean())


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
def train_one(cfg: KWSConfig, aug: int = 2, epochs: int = 60, lr: float = 0.03,
              bg_weight: float = 2.0, batch: int = 1024, seed: int = 0,
              quiet: bool = False):
    torch.manual_seed(seed)
    xtr, ytr = D.load_for(cfg, "train", aug=aug)
    xva, yva = D.load_for(cfg, "val", aug=1)

    ftr = torch.tensor(xtr.reshape(len(xtr), xtr.shape[1], -1), dtype=torch.float32)
    ltr = torch.tensor(ytr)

    # An all-zero feature vector is what the tile sees in a genuinely quiet
    # room, once every envelope has decayed to nothing. It never occurs in the
    # dataset -- even the "silence" clips are ambient noise -- so without this
    # the biases are unconstrained there, and the cocotb silence test caught a
    # trained model whose largest bias was a keyword's: the chip announced
    # "sheila" into a silent room. Pin the zero point down explicitly.
    n_quiet = max(64, len(ftr) // 32)
    ftr = torch.cat([ftr, torch.zeros(n_quiet, ftr.shape[1], ftr.shape[2])])
    ltr = torch.cat([ltr, torch.full((n_quiet,), cfg.nclass - 1, dtype=ltr.dtype)])
    model = TileClassifier(cfg).to(DEV)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)

    best = (-1.0, None)
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(ftr))
        tot = 0.0
        for s in range(0, len(perm), batch):
            idx = perm[s:s + batch]
            opt.zero_grad()
            loss = ce_max_loss(model(ftr[idx]), ltr[idx], cfg.nclass, bg_weight)
            loss.backward()
            opt.step()
            tot += float(loss.detach()) * len(idx)
        sched.step()

        w, b = model.int_weights()
        # Select on mean recall at a 5 % false-alarm rate. Selecting on plain
        # accuracy just picks whatever predicts background most often, since
        # background is 74 % of the validation split.
        rows = roc(xva, yva, w, b, cfg, fa_targets=(0.05,))
        score = float(np.nanmean(rows[0][1]))
        acc, rec, far = evaluate(xva, yva, w, bias=b, cfg=cfg)
        if score > best[0]:
            best = (score, (w.copy(), b.copy()))
        if not quiet and (ep % 10 == 9 or ep == 0):
            print(f"    ep {ep+1:3d} loss {tot/len(perm):.4f}  "
                  f"val acc {acc*100:5.2f}%  recall "
                  f"{'/'.join(f'{r*100:.0f}' for r in rec)}  FA {far*100:4.1f}%")

    return best[1], best[0], (xva, yva)


# ---------------------------------------------------------------------------
# Config sweep
# ---------------------------------------------------------------------------
SWEEP = [
    dict(nbank=5, nslot=2, frame_log2=13, feat_w=4),
    dict(nbank=5, nslot=4, frame_log2=12, feat_w=4),
    dict(nbank=5, nslot=4, frame_log2=12, feat_w=5),
    dict(nbank=5, nslot=4, frame_log2=13, feat_w=4),
    dict(nbank=5, nslot=6, frame_log2=12, feat_w=4),
    dict(nbank=7, nslot=2, frame_log2=13, feat_w=4),
    dict(nbank=7, nslot=4, frame_log2=12, feat_w=4),
    dict(nbank=7, nslot=4, frame_log2=13, feat_w=4),
    dict(nbank=7, nslot=4, frame_log2=12, feat_w=5),
]


def sweep(keywords, aug: int = 2, epochs: int = 60):
    rows = []
    for spec in SWEEP:
        cfg = KWSConfig(keywords=list(keywords), **spec)
        est = cfg.flop_estimate()["TOTAL"]
        print(f"\n--- nbank={cfg.nbank} ({cfg.nband} bands) nslot={cfg.nslot} "
              f"frame={1000/cfg.frame_hz:.0f}ms feat_w={cfg.feat_w} "
              f"| {cfg.nfeat} feats, {est} flops, ctx {cfg.context_ms:.0f}ms")
        (w, b), acc, (xva, yva) = train_one(cfg, aug=aug, epochs=epochs, quiet=True)
        a, rec, far = evaluate(xva, yva, w, b, cfg)
        rows.append((est, a, rec, far, spec, cfg))
        print(f"    val acc {a*100:5.2f}%  recall "
              f"{'/'.join(f'{r*100:.0f}' for r in rec)}  FA {far*100:4.1f}%")

    print("\n" + "=" * 92)
    print(f"{'flops':>6} {'feats':>6} {'wts':>5} {'ctx/ms':>7} "
          f"{'val acc':>8} {'recall':>12} {'FA':>6}  config")
    for est, a, rec, far, spec, cfg in sorted(rows, key=lambda r: -r[1]):
        print(f"{est:6d} {cfg.nfeat:6d} {cfg.nclass*cfg.nfeat:5d} "
              f"{cfg.context_ms:7.0f} {a*100:7.2f}% "
              f"{'/'.join(f'{r*100:.0f}' for r in rec):>12} {far*100:5.1f}%  {spec}")
    return rows


def gain_sweep(w, b, cfg: KWSConfig, gains=(-24, -18, -12, -6, 0, 6),
               n: int = 200, seed: int = 11):
    """Accuracy as a function of microphone level, on the integer path.

    The tile has no AGC, so this table is the honest answer to "how far away
    can you stand", and it is the reason augmentation spans -18..+6 dB.
    """
    from kws_hw import pdm_decimate, front_end, fine_config, derive_features
    rng = np.random.default_rng(seed)
    noise = D.load_noise()
    clips, labels = [], []
    for ci, kw in enumerate(list(cfg.keywords) + [None]):
        if kw is None:
            words = [x for x in D.list_words() if x not in cfg.keywords]
            picks = []
            for word in words:
                d = os.path.join(D.GSC, word)
                fl = [f for f in sorted(os.listdir(d)) if D.which_set(f) == "test"]
                picks += [os.path.join(d, f) for f in fl[:4]]
            rng.shuffle(picks)
            picks = picks[:n]
        else:
            d = os.path.join(D.GSC, kw)
            fl = [f for f in sorted(os.listdir(d)) if D.which_set(f) == "test"]
            picks = [os.path.join(d, f) for f in fl[:n]]
        for p in picks:
            clips.append(D.place(D.read_wav(p), rng))
            labels.append(ci if kw is not None else cfg.nclass - 1)
    base = np.stack(clips).astype(np.float32)
    labels = np.asarray(labels)

    print(f"\n  gain      acc   " +
          "  ".join(f"{k[:6]:>6}" for k in cfg.keywords) + "     FA")
    out = []
    for g in gains:
        clips_g = np.clip(base * 10 ** (g / 20.0), -1, 1)
        x = pdm_decimate(clips_g, sr_in=D.SR).astype(np.int64)
        fine = front_end(x, fine_config(cfg.nbank))
        feats = derive_features(fine, cfg)
        acc, rec, far = evaluate(feats, labels, w, b, cfg)
        out.append((g, acc, rec, far))
        print(f"  {g:+3d} dB {acc*100:7.2f}%  " +
              "  ".join(f"{r*100:5.1f}%" for r in rec) + f"  {far*100:5.1f}%")
    return out


def trim_sweep(feats, labels, w, b, cfg: KWSConfig):
    """What the four ui trim bits actually buy: a live sensitivity dial."""
    print("\n  trim   acc   recall            FA")
    for t in (0, 2, 4, 6, 8, 12, 15):
        acc, rec, far = evaluate(feats, labels, w, b, cfg, trim=t)
        print(f"  {t:4d} {acc*100:6.2f}%  "
              f"{'/'.join(f'{r*100:.0f}' for r in rec):>10}  {far*100:5.1f}%")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--config", default=None,
                    help="nbank,nslot,frame_log2,feat_w  e.g. 5,4,12,4")
    ap.add_argument("--keywords", default="marvin,sheila")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--aug", type=int, default=2)
    ap.add_argument("--tag", default="default")
    ap.add_argument("--gain-sweep", action="store_true")
    a = ap.parse_args()
    kws = a.keywords.split(",")

    if a.sweep:
        sweep(kws, aug=a.aug, epochs=a.epochs)

    if a.config:
        nb, ns, fl, fw = (int(x) for x in a.config.split(","))
        cfg = KWSConfig(nbank=nb, nslot=ns, frame_log2=fl, feat_w=fw,
                        keywords=kws)
        print(f"=== training {cfg.nband} bands x {cfg.nslot} slots, "
              f"feat_w={cfg.feat_w} ===")
        print(bank_report(cfg.nbank))
        est = cfg.flop_estimate()
        print("  flops: " + ", ".join(f"{k}={v}" for k, v in est.items()))
        (w, b), vacc, _ = train_one(cfg, aug=a.aug, epochs=a.epochs)

        xte, yte = D.load_for(cfg, "test", aug=1)
        acc, rec, far = evaluate(xte, yte, w, b, cfg)
        print(f"\n  TEST clip acc {acc*100:.2f}%  recall "
              f"{'/'.join(f'{r*100:.1f}' for r in rec)}  FA {far*100:.2f}%")
        print(f"  TEST frame acc {frame_accuracy(xte, yte, w, b, cfg)*100:.2f}%")
        cs_acc, cs_rec = closed_set(xte, yte, w, b, cfg)
        print(f"\n  CLOSED SET (clip known to hold a keyword): {cs_acc*100:.2f}%  "
              f"per-word {'/'.join(f'{x*100:.1f}' for x in cs_rec)}")
        print("\n  OPEN SET operating curve (raising the background bias is what"
              "\n  the ui[4:1] trim nibble does at run time):")
        print(f"    {'clip FA':>8}  {'recall':>16}  bias offset")
        for fa, rec, thr in roc(xte, yte, w, b, cfg):
            print(f"    {fa*100:7.0f}%  {'/'.join(f'{x*100:5.1f}' for x in rec):>16}  "
                  f"{thr:+.0f}")
        trim_sweep(xte, yte, w, b, cfg)
        if a.gain_sweep:
            gain_sweep(w, b, cfg)

        path = save_weights(a.tag, w, b, cfg, extra={
            "val_acc": vacc, "test_acc": acc, "test_recall": rec,
            "test_fa": far, "flops": est,
        })
        print(f"\n  saved {path}")

    if not (a.sweep or a.config):
        ap.print_help()
