"""What the fixed front end can support, before any hardware constraint.

Trains a plain fp32 MLP on standardised, non-pooled features from a cached
ww_feats_<tag>.npz and reports test AUC. This is the ceiling the ternary,
6-bit-accumulator model in train_sheila.py is measured against: if the ceiling
is low the features are the limit, if the gap is large the model is.

    python train/ceiling_probe.py --tag dads --hidden 64 --device cuda
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wwhw  # noqa: E402
from train_sheila import auc  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="dads")
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--nframe", type=int, default=0, help="0 = cfg.nframe (16)")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--save-teacher", action="store_true",
                    help="write artifacts/ww_teacher_<tag>.npz with per-clip logits "
                         "for distillation (train_sheila.py --kd)")
    args = ap.parse_args()
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    d = np.load(os.path.join(ART, f"ww_feats_{args.tag}.npz"), allow_pickle=True)
    feats, labels, splits = d["feats"], d["labels"], d["splits"]
    cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
    NF = args.nframe or cfg.nframe
    hop = NF // cfg.nphase
    starts = list(range(0, feats.shape[1] - NF + 1, hop))
    dev = torch.device(args.device)

    X = feats.astype(np.float32)
    tr, va, te = splits == 0, splits == 1, splits == 2
    mu, sd = X[tr].mean((0, 1)), X[tr].std((0, 1)) + 1e-3
    Xw = torch.from_numpy(np.stack([(X[:, s:s + NF] - mu) / sd for s in starts], 1)).to(dev)
    Y = torch.from_numpy((labels > 0).astype(np.float32)).to(dev)
    N, W = Xw.shape[:2]
    Xf = Xw.reshape(N, W, -1)

    net = nn.Sequential(nn.Linear(NF * cfg.nband, args.hidden), nn.ReLU(),
                        nn.Linear(args.hidden, args.hidden), nn.ReLU(),
                        nn.Linear(args.hidden, 1)).to(dev)
    opt = torch.optim.Adam(net.parameters(), 1e-3, weight_decay=1e-4)
    idx = np.where(tr)[0]

    def scores():
        with torch.no_grad():
            return net(Xf).squeeze(-1).max(1).values.cpu().numpy()

    print(f"tag={args.tag}  clips={N}  windows/clip={W}  window={NF} frames  "
          f"hidden={args.hidden}  device={dev}")
    for ep in range(args.epochs):
        net.train()
        perm = np.random.permutation(idx)
        for i in range(0, len(perm), 512):
            j = perm[i:i + 512]
            # multiple-instance: the clip is positive if any window is
            s = net(Xf[j]).squeeze(-1).logsumexp(1) - np.log(W)
            loss = F.binary_cross_entropy_with_logits(s, Y[j])
            opt.zero_grad(); loss.backward(); opt.step()
        net.eval()
        sv = scores()
        print(f"  ep {ep+1:3d}  loss {loss.item():.4f}  val AUC {auc(sv[va], labels[va] > 0)*100:5.2f}%")
    sv = scores()
    print(f"CEILING fp32 MLP-{args.hidden}: test AUC {auc(sv[te], labels[te] > 0)*100:.2f}%  "
          f"(val {auc(sv[va], labels[va] > 0)*100:.2f}%)")
    if args.save_teacher:
        out = os.path.join(ART, f"ww_teacher_{args.tag}.npz")
        np.savez(out, logits=sv.astype(np.float32), hidden=args.hidden)
        print(f"wrote {os.path.relpath(out)} (clip-level max-window logits)")
    pos, neg = sv[te][labels[te] > 0], sv[te][labels[te] == 0]
    for fpr in (0.01, 0.05, 0.10):
        thr = np.quantile(neg, 1 - fpr)
        print(f"  recall at {fpr*100:4.0f}% false-positive clips: {(pos > thr).mean()*100:5.1f}%")


if __name__ == "__main__":
    main()
