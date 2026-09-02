"""Run the bit-exact hardware front end over Speech Commands and cache features.

The front end is fixed silicon -- nothing about it is learned -- so it is run
once and the 24 x NBAND log-magnitude maps are cached. Training then only ever
touches the template weights.

    python wakeword/train/extract.py --targets marvin --aug 3

Writes artifacts/ww_feats_<tag>.npz with feats (N, NFRAME_EXT, NBAND) uint8,
labels, splits, and the HWConfig used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wwdata  # noqa: E402
import wwhw  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "artifacts")
NFRAME_EXT = 24          # 24 x 41.9 ms = 1.006 s -- the whole clip


def peak_normalise(a: np.ndarray, target: float = 0.7) -> np.ndarray:
    return a / np.maximum(np.abs(a).max(1, keepdims=True), 1e-3) * target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", nargs="+", default=["marvin"])
    ap.add_argument("--neg-per-word", type=int, default=180)
    ap.add_argument("--aug", type=int, default=3, help="jittered copies of each positive")
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--frames", type=int, default=NFRAME_EXT)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--limit", type=int, default=0)
    for k, dv in [("nstage", wwhw.NSTAGE), ("nband", wwhw.NBAND), ("tap0", wwhw.TAP0),
                  ("state-w", wwhw.STATE_W), ("mant", wwhw.MANT),
                  ("feat-w", wwhw.FEAT_W), ("k-shift", wwhw.K_SHIFT)]:
        ap.add_argument(f"--{k}", type=int, default=dv)
    args = ap.parse_args()

    cfg = wwhw.HWConfig(nstage=args.nstage, nband=args.nband, tap0=args.tap0,
                        state_w=args.state_w, mant=args.mant, feat_w=args.feat_w,
                        k_shift=args.k_shift)

    tag = args.tag or "_".join(args.targets)
    items = wwdata.index(args.targets, n_neg_per_word=args.neg_per_word)
    if args.limit:
        rng = np.random.default_rng(0)
        items = [items[i] for i in rng.permutation(len(items))[:args.limit]]

    # Positives get extra time-jittered copies; the detector's staggered windows
    # mean alignment robustness is the thing that actually matters in the field.
    plan = [(p, l, s, 0.0) for p, l, s in items]
    for rep in range(1, max(args.aug, 1)):
        plan += [(p, l, s, 0.06 * rep) for p, l, s in items if l > 0]

    n = len(plan)
    pos = sum(1 for r in plan if r[1] > 0)
    print(f"targets={args.targets}  clips={n}  positives={pos}  negatives={n-pos}")
    print(wwhw.describe(cfg))
    print(f"extracting {args.frames} frames/clip in batches of {args.batch}")

    feats = np.zeros((n, args.frames, cfg.nband), dtype=np.uint8)
    labels = np.array([r[1] for r in plan], dtype=np.int64)
    splits = np.array([{"train": 0, "val": 1, "test": 2}[r[2]] for r in plan],
                      dtype=np.int64)

    t0 = time.time()
    for i in range(0, n, args.batch):
        chunk = plan[i:i + args.batch]
        audio = np.zeros((len(chunk), wwdata.CLIP_LEN), dtype=np.float32)
        for j, (path, _, _, jit) in enumerate(chunk):
            x = wwdata.read_wav(path)
            if jit:
                x = np.roll(x, int(jit * wwdata.AUDIO_HZ) * (1 if j % 2 else -1))
            audio[j] = x
        audio = peak_normalise(audio)
        feats[i:i + len(chunk)] = wwhw.frontend_batch(audio, cfg, n_frames=args.frames)
        el = time.time() - t0
        done = i + len(chunk)
        print(f"  {done}/{n}  {el:6.0f}s elapsed  eta {el/done*(n-done):6.0f}s", flush=True)

    os.makedirs(ART, exist_ok=True)
    out = os.path.join(ART, f"ww_feats_{tag}.npz")
    np.savez_compressed(out, feats=feats, labels=labels, splits=splits,
                        targets=np.array(args.targets),
                        cfg=json.dumps(cfg.to_dict()))
    print(f"\nwrote {os.path.relpath(out)}  {feats.nbytes/1e6:.1f} MB uncompressed")
    print(f"feature range {feats.min()}..{feats.max()}  mean {feats.mean():.2f}")
    for s, name in enumerate(["train", "val", "test"]):
        m = splits == s
        print(f"  {name:5s} {m.sum():6d}  positives {int((labels[m] > 0).sum())}")


if __name__ == "__main__":
    main()
