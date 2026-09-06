"""Per-clip scores from an emitted header, for slicing an AUC apart.

``train/eval_header.py`` prints one pooled AUC. To ask *which* clips a detector
gets wrong -- by corpus, by recording, by level -- the scores themselves are
needed, so this runs the same integer chip model and saves them. It never
rewrites the header (``eval_header.py --set-fpr`` does).

    python train/optim/score_header.py --header artifacts/headers/ww_weights_babycry.svh \\
        --tag babycry_fin --nframe 8 --nphase 2 --feat-off 4 --out /tmp/scores.npy

The score is the per-clip maximum over window positions, exactly what the chip
compares against its threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "train"))
import wwhw  # noqa: E402
from eval_header import parse_header, window_scores  # noqa: E402
from train_sheila import auc  # noqa: E402

ART = os.path.join(ROOT, "artifacts")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--header", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--nframe", type=int, default=0)
    ap.add_argument("--nphase", type=int, default=0)
    ap.add_argument("--feat-off", type=int, default=6)
    ap.add_argument("--stats", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    d = np.load(os.path.join(ART, f"ww_feats_{args.tag}.npz"), allow_pickle=True)
    feats, labels, splits = d["feats"], d["labels"], d["splits"]
    cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
    if args.stats:
        have = [str(s) for s in d["stats"]]
        sel = [s.strip() for s in args.stats.split(",") if s.strip()]
        nb = cfg.nband // len(have)
        cols = []
        for s in sel:
            name, _, where = s.partition("@")
            for b in range(nb):
                if where and where != "all":
                    lo, _, hi = where.partition("-")
                    if not (int(lo) <= b <= int(hi or lo)):
                        continue
                cols.append(b * len(have) + have.index(name))
        feats = feats[:, :, cols]
        cfg.nband = len(cols)
    if args.nframe:
        cfg.nframe = args.nframe
    if args.nphase:
        cfg.nphase = args.nphase

    W1, HB, W2, thr, hacc_w = parse_header(args.header, cfg.nframe, cfg.nband)
    sc = window_scores(feats, W1, HB, W2, cfg.nframe // cfg.nphase, hacc_w,
                       feat_off=args.feat_off).max(1)
    np.save(args.out, sc)
    te = splits == 2
    print(f"wrote {args.out}  {len(sc)} clips; test AUC "
          f"{auc(sc[te], labels[te] > 0)*100:.2f}%")


if __name__ == "__main__":
    main()
