"""Headroom map: how much AUC a cached feature set can support at all.

``train/ceiling_probe.py`` answers this with one fixed MLP-64 read off at its
*final* epoch. That is not a ceiling -- on small-positive tasks it overfits and
reports a number the hardware model beats (``clap``, ``vad`` in
``docs/new_tasks.md``). This does the same job honestly:

* a ladder of capacities (linear -> MLP-256), so "the features are the limit"
  and "the classifier is the limit" can be told apart;
* every model selected on **validation** and reported at that epoch, never at
  the last one;
* several seeds, reported as a mean, because a single seed on 111 validation
  positives is noise.

    python train/optim/probe.py --tag catmeow --seeds 3

The number to compare against the hardware model is ``best test`` -- the
capacity that won on validation, scored on test. If that number is below the
target, no amount of classifier work reaches the target and the *front end*
has to change.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wwhw  # noqa: E402
from train_sheila import auc  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "..", "artifacts")

# (name, hidden units, depth). depth 0 is a bare linear read-out: the same
# family the ternary template lives in, only unquantised.
LADDER = [("linear", 0, 0), ("mlp32", 32, 2), ("mlp64", 64, 2),
          ("mlp256", 256, 2)]


def build(nin: int, hidden: int, depth: int, dev) -> nn.Module:
    if depth == 0:
        return nn.Linear(nin, 1).to(dev)
    layers: list[nn.Module] = [nn.Linear(nin, hidden), nn.ReLU()]
    for _ in range(depth - 1):
        layers += [nn.Linear(hidden, hidden), nn.ReLU()]
    layers += [nn.Linear(hidden, 1)]
    return nn.Sequential(*layers).to(dev)


def train_one(Xf, Y, tr_idx, va, te, labels, hidden, depth, epochs, seed, dev,
              wd=1e-4, lr=1e-3, batch=512):
    """Train once, return (best val AUC, test AUC at that epoch)."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    W = Xf.shape[1]
    net = build(Xf.shape[-1], hidden, depth, dev)
    opt = torch.optim.Adam(net.parameters(), lr, weight_decay=wd)
    best = (-1.0, 0.0)
    ypos = labels > 0
    for ep in range(epochs):
        net.train()
        perm = np.random.permutation(tr_idx)
        for i in range(0, len(perm), batch):
            j = perm[i:i + batch]
            s = net(Xf[j]).squeeze(-1).logsumexp(1) - np.log(W)
            loss = F.binary_cross_entropy_with_logits(s, Y[j])
            opt.zero_grad()
            loss.backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            sv = net(Xf).squeeze(-1).max(1).values.cpu().numpy()
        v = auc(sv[va], ypos[va])
        if v > best[0]:
            best = (v, auc(sv[te], ypos[te]))
    return best


def load(tag: str, nframe: int, dev):
    d = np.load(os.path.join(ART, f"ww_feats_{tag}.npz"), allow_pickle=True)
    feats, labels, splits = d["feats"], d["labels"], d["splits"]
    cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
    NF = nframe or cfg.nframe
    if NF > feats.shape[1]:
        raise SystemExit(f"--nframe {NF} > {feats.shape[1]} cached frames")
    hop = max(1, NF // cfg.nphase)
    starts = list(range(0, feats.shape[1] - NF + 1, hop))
    X = feats.astype(np.float32)
    tr = splits == 0
    mu, sd = X[tr].mean((0, 1)), X[tr].std((0, 1)) + 1e-3
    Xw = np.stack([(X[:, s:s + NF] - mu) / sd for s in starts], 1)
    Xf = torch.from_numpy(Xw.reshape(len(X), len(starts), -1)).to(dev)
    Y = torch.from_numpy((labels > 0).astype(np.float32)).to(dev)
    return Xf, Y, labels, splits, cfg, len(starts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--nframe", type=int, default=8, help="0 = the extraction's")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--ladder", default="", help="comma-separated subset of the ladder")
    ap.add_argument("--out", default=os.path.join(ART, "optim", "probe.jsonl"))
    ap.add_argument("--note", default="", help="free-text label for the jsonl row")
    args = ap.parse_args()

    dev = torch.device(args.device)
    Xf, Y, labels, splits, cfg, W = load(args.tag, args.nframe, dev)
    tr_idx = np.where(splits == 0)[0]
    va, te = splits == 1, splits == 2
    want = set(args.ladder.split(",")) if args.ladder else None
    rungs = [r for r in LADDER if not want or r[0] in want]

    print(f"tag={args.tag}  nframe={args.nframe or cfg.nframe} nband={cfg.nband} "
          f"windows/clip={W} clips={len(labels)} "
          f"pos={(labels > 0).sum()} val_pos={(labels[va] > 0).sum()} "
          f"test_pos={(labels[te] > 0).sum()}", flush=True)

    rows, best = [], None
    for name, hidden, depth in rungs:
        vs, ts = [], []
        for s in range(args.seeds):
            v, t = train_one(Xf, Y, tr_idx, va, te, labels, hidden, depth,
                             args.epochs, s, dev)
            vs.append(v * 100)
            ts.append(t * 100)
        row = dict(rung=name, val=round(float(np.mean(vs)), 2),
                   val_sd=round(float(np.std(vs)), 2),
                   test=round(float(np.mean(ts)), 2),
                   test_sd=round(float(np.std(ts)), 2))
        rows.append(row)
        print(f"  {name:8s} val {row['val']:6.2f} +- {row['val_sd']:.2f}   "
              f"test {row['test']:6.2f} +- {row['test_sd']:.2f}", flush=True)
        if best is None or row["val"] > best["val"]:
            best = row

    out = dict(tag=args.tag, note=args.note, nframe=args.nframe or cfg.nframe,
               nband=cfg.nband, tap0=cfg.tap0, mant=cfg.mant,
               frame_log2=cfg.frame_log2, k_shift=cfg.k_shift,
               seeds=args.seeds, epochs=args.epochs, ladder=rows,
               best_rung=best["rung"], best_val=best["val"], best_test=best["test"])
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "a") as f:
        f.write(json.dumps(out) + "\n")
    print(f"HEADROOM {args.tag}: best rung {best['rung']} "
          f"val {best['val']:.2f} test {best['test']:.2f}")


if __name__ == "__main__":
    main()
