"""Evaluate an emitted ww_weights*.svh header with the bit-exact chip model.

Closes the loop between training and silicon: the header is parsed exactly as
test/test.py parses it, fed through wwhw.Detector semantics (per-frame
saturating accumulator, requantiser, ternary output layer) over the cached
features, and scored on the held-out split. If the training-time AUC and this
AUC disagree, the export is wrong, not the model.

    python train/eval_header.py --header src/ww_weights_drone.svh --tag dads
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wwhw  # noqa: E402
from train_sheila import auc  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")
HACC_W, HSHIFT, FEAT_OFF, SCORE_W = 6, 1, 6, 10   # tt_um_wakeword defaults


def parse_header(path: str, NF: int, NB: int):
    """Same decoding as test/test.py:load_weights.

    H and the accumulator width are taken from the header's own bit widths
    (WW_ROW is H*NF*2*NB bits, WW_HBIAS is H*HACC_W bits), so a header emitted
    with a different --H or --accw is decoded correctly or rejected, never
    silently mis-sliced.
    """
    with open(path) as f:
        txt = f.read()

    def const(name):
        m = re.search(name + r"\s*=\s*(\d+)'h([0-9a-fA-F]+)", txt)
        if not m:
            raise ValueError(f"{name} not found in {path}")
        return int(m.group(2), 16), int(m.group(1))

    v, vw = const("WW_ROW")
    if vw % (NF * 2 * NB):
        raise ValueError(f"WW_ROW width {vw} is not a multiple of NF*2*NB={NF*2*NB}")
    H = vw // (NF * 2 * NB)
    _, hbw = const("WW_HBIAS")
    if hbw % H:
        raise ValueError(f"WW_HBIAS width {hbw} not divisible by H={H}")
    hacc_w = hbw // H
    W1 = np.zeros((H, NF, NB), dtype=np.int64)
    for h in range(H):
        for f in range(NF):
            row = (v >> (2 * NB * (h * NF + f))) & ((1 << (2 * NB)) - 1)
            for b in range(NB):
                c = (row >> (2 * b)) & 0b11
                W1[h, f, b] = 1 if c == 0b01 else (-1 if c == 0b11 else 0)
    hv, _ = const("WW_HBIAS")
    HB = np.array([((hv >> (hacc_w * h)) & ((1 << hacc_w) - 1)) for h in range(H)])
    HB = np.where(HB >> (hacc_w - 1), HB - (1 << hacc_w), HB)
    wv, _ = const("WW_W2")
    W2 = np.array([1 if ((wv >> (2 * h)) & 3) == 1 else (-1 if ((wv >> (2 * h)) & 3) == 3 else 0)
                   for h in range(H)])
    tv, tw = const("WW_THRESH_PK")
    thr = tv - (1 << tw) if tv >> (tw - 1) else tv
    return W1, HB, W2, int(thr), hacc_w


def window_scores(feats: np.ndarray, W1, HB, W2, hop: int, hacc_w=HACC_W,
                  hshift=HSHIFT, feat_off=FEAT_OFF) -> np.ndarray:
    """Vectorised Detector: (N, T, NB) uint8 -> (N, n_windows) integer scores."""
    H, NF, NB = W1.shape
    lim = (1 << (hacc_w - 1)) - 1
    x = feats.astype(np.int64) - feat_off
    starts = range(0, feats.shape[1] - NF + 1, hop)
    out = []
    for s in starts:
        acc = np.broadcast_to(HB, (feats.shape[0], H)).copy()
        for f in range(NF):
            acc = np.clip(acc + x[:, s + f, :] @ W1[:, f, :].T, -lim - 1, lim)
        h = np.clip(acc >> hshift, 0, 15)
        h[acc < 0] = 0
        out.append(h @ W2)
    return np.stack(out, 1)


def set_threshold(path, feats, labels, splits, W1, HB, W2, hop, fpr, hacc_w=HACC_W):
    """Pick the threshold on the validation split and patch it into the header.

    Weights are untouched; only the WW_THRESH_PK constant and its comment
    change, so a header can be re-pointed without retraining.
    """
    v = splits == 1
    neg = np.sort(window_scores(feats[v], W1, HB, W2, hop, hacc_w).max(1)[labels[v] == 0])
    thr = int(neg[int(np.ceil((1 - fpr) * len(neg))) - 1])
    with open(path) as f:
        txt = f.read()
    tv = thr & ((1 << SCORE_W) - 1)
    txt, n = re.subn(r"(WW_THRESH_PK\s*=\s*)\d+'h[0-9a-fA-F]+",
                     rf"\g<1>{SCORE_W}'h{tv:0{SCORE_W // 4 + 1}x}", txt)
    if n != 1:
        raise ValueError(f"WW_THRESH_PK not found in {path}")
    txt = re.sub(r"threshold: -?\d+.*", f"threshold: {thr}  (validation clip FPR "
                 f"{fpr*100:.0f}%, set by eval_header.py --set-fpr)", txt)
    with open(path, "w") as f:
        f.write(txt)
    return thr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--header", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "ww_weights_drone.svh"))
    ap.add_argument("--tag", default="dads")
    ap.add_argument("--split", default="test", choices=["train", "val", "test"])
    ap.add_argument("--set-fpr", type=float, default=0.0,
                    help="rewrite WW_THRESH_PK so that this fraction of *validation* "
                         "non-drone clips fires (drone-detector operating point; the "
                         "wake-word FA/hour rule in train_sheila.py is far too strict here)")
    args = ap.parse_args()

    d = np.load(os.path.join(ART, f"ww_feats_{args.tag}.npz"), allow_pickle=True)
    feats, labels, splits = d["feats"], d["labels"], d["splits"]
    cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
    W1, HB, W2, thr, hacc_w = parse_header(args.header, cfg.nframe, cfg.nband)
    nz = int((W1 != 0).sum())
    print(f"header {os.path.relpath(args.header)}: H={W1.shape[0]} {hacc_w}-bit accumulator, "
          f"{nz}/{W1.size} non-zero weights, bias {HB.tolist()}, W2 {W2.tolist()}, "
          f"threshold {thr}")

    if args.set_fpr:
        thr = set_threshold(args.header, feats, labels, splits, W1, HB, W2,
                            cfg.nframe // cfg.nphase, args.set_fpr, hacc_w)
        print(f"threshold set to {thr} at {args.set_fpr*100:.0f}% validation clip FPR")

    m = splits == {"train": 0, "val": 1, "test": 2}[args.split]
    sc = window_scores(feats[m], W1, HB, W2, cfg.nframe // cfg.nphase, hacc_w).max(1)
    pos = labels[m] > 0
    print(f"{args.split}: {m.sum()} clips, {pos.sum()} drone   AUC {auc(sc, pos)*100:.2f}%")
    print(f"  score range {sc.min()}..{sc.max()}   fires (> {thr}) on "
          f"{(sc[pos] > thr).mean()*100:.1f}% of drone, {(sc[~pos] > thr).mean()*100:.1f}% of non-drone")
    neg = np.sort(sc[~pos])
    for fpr in (0.01, 0.02, 0.05, 0.10):
        t = neg[int(np.ceil((1 - fpr) * len(neg))) - 1]
        print(f"  threshold {t:3d}: recall {(sc[pos] > t).mean()*100:5.1f}%  at <= {fpr*100:.0f}% "
              f"non-drone clips firing (actual {(sc[~pos] > t).mean()*100:.1f}%)")
    if "index" in d.files:
        sil = m & (d["index"] < 0)
        if sil.any():
            print(f"  synthetic room tone: {(sc[sil[m]] > thr).mean()*100:.1f}% fire at the shipped threshold")


if __name__ == "__main__":
    main()
