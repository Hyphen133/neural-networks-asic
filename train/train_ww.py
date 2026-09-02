"""Train the ternary template that gets hard-wired into tt_um_wakeword.

The front end is fixed silicon, so the only learnable thing on the chip is
NWORD x NFRAME x NBAND ternary weights plus one threshold per word -- 256
trits and two numbers for a two-keyword build.

At that capacity, hard labels waste most of what the data could tell you, so
there is an optional distillation path: a small log-mel CNN teacher is trained
on the same speaker-disjoint split and its probabilities become soft targets.
`--kd 0` reproduces the hard-label baseline for the ablation.

    python train/train_ww.py --tag marvin_sheila --kd 0.7 --emit

Detection is multiple-instance: the chip fires if *any* staggered window
crosses the threshold, so training maximises over windows too. Getting that
wrong is the classic way to build a spotter that scores well offline and
false-alarms constantly in the room.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wwdata  # noqa: E402
import wwhw  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")


# ---------------------------------------------------------------------------
# Windows -- exactly the staggered evaluation the RTL performs
# ---------------------------------------------------------------------------
def make_windows(feats: np.ndarray, nframe: int, hop: int) -> np.ndarray:
    """(N, T, B) -> (N, W, nframe, B) for every window start the chip evaluates."""
    starts = list(range(0, feats.shape[1] - nframe + 1, hop))
    return np.stack([feats[:, s:s + nframe] for s in starts], axis=1), starts


# ---------------------------------------------------------------------------
# Student: ternary template, multiple-instance max over windows
# ---------------------------------------------------------------------------
class TernarySTE(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, t):
        ctx.save_for_backward(x)
        return (x > t).float() - (x < -t).float()

    @staticmethod
    def backward(ctx, g):
        (x,) = ctx.saved_tensors
        return g * (x.abs() <= 1.5), None


class Template(nn.Module):
    def __init__(self, nword, nframe, nband, thresh=0.5):
        super().__init__()
        self.w = nn.Parameter(torch.empty(nword, nframe, nband).uniform_(-0.9, 0.9))
        self.bias = nn.Parameter(torch.zeros(nword))
        self.log_temp = nn.Parameter(torch.tensor(3.0))
        self.thresh = thresh

    def ternary(self):
        return TernarySTE.apply(self.w, self.thresh)

    def scores(self, win):
        """win: (N, W, F, B) float -> (N, W, NWORD) integer-valued scores."""
        return torch.einsum("nwfb,kfb->nwk", win, self.ternary())

    def forward(self, win, hard=False):
        s = self.scores(win)
        pooled = s.max(1).values if hard else s.logsumexp(1) - np.log(s.shape[1])
        return (pooled + self.bias) / torch.exp(self.log_temp), s

    @torch.no_grad()
    def clip_(self):
        self.w.clamp_(-1.5, 1.5)


# ---------------------------------------------------------------------------
# Teacher: log-mel CNN. Only used to produce soft targets.
# ---------------------------------------------------------------------------
def mel_filterbank(n_fft=512, n_mels=40, sr=16000, fmin=20.0, fmax=7600.0):
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    bins = np.floor((n_fft + 1) * pts / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(n_mels):
        l, c, r = bins[m], bins[m + 1], bins[m + 2]
        if c == l:
            c = l + 1
        if r == c:
            r = c + 1
        fb[m, l:c] = (np.arange(l, c) - l) / max(c - l, 1)
        fb[m, c:r] = (r - np.arange(c, r)) / max(r - c, 1)
    return fb


def log_mel(audio: np.ndarray, n_fft=512, hop=160, n_mels=40) -> np.ndarray:
    win = np.hanning(n_fft).astype(np.float32)
    fb = mel_filterbank(n_fft, n_mels)
    n = (audio.shape[1] - n_fft) // hop + 1
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    frames = audio[:, idx] * win
    spec = np.abs(np.fft.rfft(frames, axis=-1)) ** 2
    return np.log(spec @ fb.T + 1e-6).astype(np.float32)     # (N, T, n_mels)


class TeacherCNN(nn.Module):
    def __init__(self, nclass):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
            nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.2),
            nn.Linear(64, nclass))

    def forward(self, x):
        return self.net(x.unsqueeze(1))


def train_teacher(mel, y, splits, nclass, epochs=12, seed=0):
    torch.manual_seed(seed)
    m = torch.from_numpy((mel - mel.mean()) / (mel.std() + 1e-6))
    Y = torch.from_numpy(y)
    tr, te = splits == 0, splits == 2
    net = TeacherCNN(nclass)
    opt = torch.optim.Adam(net.parameters(), 2e-3, weight_decay=1e-4)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(int(tr.sum()))
        Xtr, Ytr = m[tr][perm], Y[tr][perm]
        for i in range(0, len(Xtr), 128):
            loss = F.cross_entropy(net(Xtr[i:i + 128]), Ytr[i:i + 128])
            opt.zero_grad(); loss.backward(); opt.step()
        sch.step()
    net.eval()
    with torch.no_grad():
        acc = (net(m[te]).argmax(1) == Y[te]).float().mean().item()
        logits = torch.cat([net(m[i:i + 512]) for i in range(0, len(m), 512)])
    print(f"teacher: log-mel CNN, test accuracy {acc*100:.2f}%")
    return logits, acc


# ---------------------------------------------------------------------------
# Evaluation -- wake-word metrics, not plain accuracy
# ---------------------------------------------------------------------------
def eval_word(scores_max, is_pos, hop_frames, frame_ms, fa_targets=(1.0, 5.0)):
    """scores_max: (N,) per-clip max window score. Returns operating points."""
    pos, neg = scores_max[is_pos], scores_max[~is_pos]
    order = np.argsort(-neg)
    evals_per_hour = 3600_000.0 / (hop_frames * frame_ms)
    out = []
    for fa in fa_targets:
        k = fa / evals_per_hour * len(neg)          # allowed false windows
        if k < 1:
            thr = neg.max() + 1                     # below the measurement floor
            floor = True
        else:
            thr = neg[order[int(k) - 1]]
            floor = False
        out.append(dict(fa_per_hour=fa, thresh=float(thr),
                        recall=float((pos > thr).mean()), below_floor=floor))
    auc = float((pos[:, None] > neg[None, :]).mean())
    return out, auc, evals_per_hour


def pack_weights(W: np.ndarray) -> tuple[int, int]:
    """(NWORD, NFRAME, NBAND) ternary -> the WW_ROW constant the RTL indexes."""
    nword, nframe, nband = W.shape
    rows = []
    for w in range(nword):
        for f in range(nframe):
            row = 0
            for b in range(nband):
                row |= {0: 0b00, 1: 0b01, -1: 0b11}[int(W[w, f, b])] << (2 * b)
            rows.append(row)
    val, width = 0, len(rows) * 2 * nband
    for i, r in enumerate(rows):
        val |= r << (2 * nband * i)
    return val, width


def emit_svh(W, thresholds, path, meta):
    val, width = pack_weights(W)
    nz = int((W != 0).sum())
    with open(path, "w") as f:
        f.write("// Auto-generated by train/train_ww.py -- do not edit.\n")
        f.write(f"// {meta}\n")
        f.write(f"// {nz}/{W.size} weights non-zero ({100*nz/W.size:.0f}%); "
                "zeros drop out of the adder tree entirely.\n")
        f.write(f"localparam [{width-1}:0] WW_ROW = {width}'h{val:0{width//4}x};\n")
        # Packed, not an unpacked array parameter: Icarus rejects those.
        tw = len(thresholds) * 12
        tv = 0
        for i, t in enumerate(thresholds):
            tv |= (int(t) & 0xFFF) << (12 * i)
        f.write(f"localparam [{tw-1}:0] WW_THRESH_PK = {tw}'h{tv:0{max(tw//4,1)}x};\n")
        f.write("// thresholds: " + ", ".join(str(int(t)) for t in thresholds) + "\n")
    return nz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="marvin_sheila")
    ap.add_argument("--kd", type=float, default=0.7, help="0 = hard labels only")
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--fa", type=float, default=1.0, help="false alarms/hour target")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--teacher-epochs", type=int, default=12)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    d = np.load(os.path.join(ART, f"ww_feats_{args.tag}.npz"), allow_pickle=True)
    feats, labels, splits = d["feats"], d["labels"], d["splits"]
    targets = [str(t) for t in d["targets"]]
    cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
    nword = len(targets)
    print(f"targets={targets}  clips={len(feats)}  feats={feats.shape}  "
          f"kd={args.kd}")

    hop = cfg.nframe // cfg.nphase
    win, starts = make_windows(feats.astype(np.float32), cfg.nframe, hop)
    print(f"window starts {starts}  -> {win.shape[1]} evaluations per clip")

    Xw = torch.from_numpy(win)
    Y = torch.from_numpy((labels[:, None] == np.arange(1, nword + 1)[None, :])
                         .astype(np.float32))
    tr, va, te = splits == 0, splits == 1, splits == 2

    # ---- teacher -------------------------------------------------------
    soft = None
    if args.kd > 0:
        t0 = time.time()
        cache = os.path.join(ART, f"ww_teacher_{args.tag}.npz")
        if os.path.exists(cache):
            soft = np.load(cache)["soft"]
            print(f"teacher: loaded cached soft targets {soft.shape}")
        else:
            # Rebuild extract.py's plan: base items, then (aug-1) jittered
            # copies of the positives. Teacher probabilities are per *clip*,
            # so the augmented copies inherit their source clip's targets.
            base = wwdata.index(targets, n_neg_per_word=180)
            n_base = len(base)
            pos_idx = np.array([i for i, (_, l, _) in enumerate(base) if l > 0])
            n_extra = len(feats) - n_base
            assert n_extra >= 0 and n_extra % max(len(pos_idx), 1) == 0, \
                f"feature/plan mismatch ({len(feats)} vs {n_base}); re-run extract.py"
            n_aug = 1 + n_extra // len(pos_idx)
            print(f"teacher: {n_base} base clips, aug x{n_aug}")
            audio = wwdata.load_audio(base)
            audio = audio / np.maximum(np.abs(audio).max(1, keepdims=True), 1e-3) * 0.7
            mel = log_mel(audio)
            logits, _ = train_teacher(mel, labels[:n_base], splits[:n_base],
                                      nword + 1, args.teacher_epochs, args.seed)
            p = torch.softmax(logits, 1)[:, 1:].numpy()      # P(each keyword)
            soft = np.concatenate([p] + [p[pos_idx]] * (n_aug - 1), axis=0)
            assert len(soft) == len(feats)
            np.savez_compressed(cache, soft=soft)
            print(f"teacher done in {time.time()-t0:.0f}s")
        soft = torch.from_numpy(soft.astype(np.float32))

    # ---- student -------------------------------------------------------
    model = Template(nword, cfg.nframe, cfg.nband)
    opt = torch.optim.Adam(model.parameters(), args.lr)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, args.epochs)
    idx_tr = np.where(tr)[0]
    best = (-1.0, None)

    for ep in range(args.epochs):
        model.train()
        perm = np.random.permutation(idx_tr)
        for i in range(0, len(perm), 512):
            j = perm[i:i + 512]
            logit, _ = model(Xw[j])
            loss = F.binary_cross_entropy_with_logits(logit, Y[j])
            if soft is not None:
                loss = (1 - args.kd) * loss + args.kd * \
                    F.binary_cross_entropy_with_logits(logit, soft[j])
            opt.zero_grad(); loss.backward(); opt.step(); model.clip_()
        sch.step()

        if (ep + 1) % 10 == 0 or ep == args.epochs - 1:
            model.eval()
            with torch.no_grad():
                s = model.scores(Xw).max(1).values.numpy()      # (N, NWORD)
            rec = []
            for k in range(nword):
                ops, auc, eph = eval_word(s[va, k], labels[va] == k + 1,
                                          hop, cfg.frame_ms, (args.fa,))
                rec.append(ops[0]["recall"])
            m = float(np.mean(rec))
            if m > best[0]:
                best = (m, {kk: v.detach().clone() for kk, v in model.state_dict().items()})
            print(f"ep {ep+1:4d}  loss {loss.item():.4f}  "
                  f"val recall @{args.fa}/h " +
                  "  ".join(f"{t}={r*100:5.1f}%" for t, r in zip(targets, rec)))

    model.load_state_dict(best[1])
    model.eval()
    with torch.no_grad():
        W = model.ternary().numpy().astype(np.int64)
        s = model.scores(Xw).max(1).values.numpy()

    print(f"\n=== test set ({int(te.sum())} clips) ===")
    thresholds, summary = [], []
    for k, t in enumerate(targets):
        ops, auc, eph = eval_word(s[te, k], labels[te] == k + 1, hop, cfg.frame_ms,
                                  (0.5, 1.0, 5.0, 20.0))
        vops, _, _ = eval_word(s[va, k], labels[va] == k + 1, hop, cfg.frame_ms,
                               (args.fa,))
        thresholds.append(int(np.floor(vops[0]["thresh"])))
        print(f"  {t}: AUC {auc*100:.2f}%   threshold {thresholds[-1]} "
              f"(chosen on val at {args.fa} FA/h)")
        for o in ops:
            flag = "  [below measurement floor]" if o["below_floor"] else ""
            print(f"      recall @ {o['fa_per_hour']:5.1f} FA/h = "
                  f"{o['recall']*100:5.1f}%  (thr {o['thresh']:.0f}){flag}")
        summary.append(dict(word=t, auc=auc, ops=ops, thresh=thresholds[-1]))
    print(f"  detector evaluates {eph:.0f} windows/hour "
          f"(one every {hop*cfg.frame_ms:.0f} ms)")

    nz = int((W != 0).sum())
    print(f"\nweights: {nz}/{W.size} non-zero ({100*nz/W.size:.0f}%), "
          f"{int((W>0).sum())} positive / {int((W<0).sum())} negative")

    out = os.path.join(ART, f"ww_model_{args.tag}_kd{args.kd}.npz")
    np.savez(out, W=W, thresholds=np.array(thresholds), targets=np.array(targets),
             summary=json.dumps(summary), cfg=json.dumps(cfg.to_dict()))
    print(f"wrote {os.path.relpath(out)}")

    if args.emit:
        p = os.path.join(SRC, "ww_weights.svh")
        meta = (f"targets={targets} kd={args.kd} "
                f"AUC=" + ",".join(f"{x['auc']*100:.1f}%" for x in summary))
        emit_svh(W, thresholds, p, meta)
        print(f"wrote {os.path.relpath(p)}")


if __name__ == "__main__":
    main()
