"""Exploratory front end: more than one statistic per band per frame.

The shipped front end keeps exactly one number per band per frame -- the
**maximum** log magnitude over 41.9 ms. Everything else the band did in that
frame is discarded, and rounds 1-3 say that discarding is what caps these
detectors: an unconstrained fp32 model on those features tops out in the low
80s.

Adding a second statistic is the one capacity increase that does not need
another cascade stage. In silicon it is one more FEAT_W register and one more
comparator per band (the max path already exists), which is far cheaper than
NBAND=7's extra 10-bit state, its extra decimator and its extra ROM column.

This module runs the *same* bit-exact cascade as ``wwhw.frontend_batch`` and
records four running statistics per band per frame instead of one:

    max   the shipped feature
    min   the frame's floor -- max-min is the band's modulation depth,
          which is what separates a transient from a steady tone
    mean  a rounded running mean of the log magnitude (an accumulator and a
          shift in silicon, not a divider, since the tick count per frame per
          band is a power of two)
    last  the value at the frame boundary

Nothing here is bit-exact hardware yet, and nothing in ``wwhw.py`` or the RTL
is touched: this exists to find out whether a second statistic is worth
designing before any of it is designed.

    python train/optim/fe_stats.py --task clap --stats max,min --tag clap_mm
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wwhw  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ART = os.path.join(ROOT, "artifacts")
CACHE = os.path.join(ART, "data")

STATS = ("max", "min", "mean", "last")


def frontend_stats(audio: np.ndarray, cfg: wwhw.HWConfig, n_frames: int,
                   gain: float = 0.5) -> np.ndarray:
    """(B, L) audio -> (B, n_frames, NBAND, 4) uint8, one plane per statistic.

    The cascade, the band difference and the log feature are character for
    character the ones in ``wwhw.frontend_batch``; only the per-frame
    reduction differs.
    """
    B = audio.shape[0]
    n_ticks = n_frames << cfg.frame_log2
    state = np.zeros((cfg.nstage, B), dtype=np.int32)
    out = np.zeros((B, n_frames, cfg.nband, len(STATS)), dtype=np.uint8)

    fmax = np.zeros((cfg.nband, B), dtype=np.int32)
    fmin = np.full((cfg.nband, B), cfg.feat_max, dtype=np.int32)
    fsum = np.zeros((cfg.nband, B), dtype=np.int64)
    flast = np.zeros((cfg.nband, B), dtype=np.int32)
    fcnt = np.zeros(cfg.nband, dtype=np.int64)

    frame_mask = (1 << cfg.frame_log2) - 1
    taps = [cfg.tap0 + i for i in range(cfg.nband)]

    for n, bit in enumerate(wwhw.pdm_encode_batch(audio, n_ticks, cfg, gain)):
        x = (bit * cfg.in_amp).astype(np.int32)
        prev = x
        for b in range(cfg.nstage):
            if n & ((1 << b) - 1):
                break
            s = state[b]
            state[b] = s + ((prev - s) >> cfg.k_shift)
            prev = state[b]

        for i, b in enumerate(taps):
            if n & ((1 << b) - 1):
                continue
            f = wwhw.log_feature(state[b - 1] - state[b], cfg.mant, cfg.feat_max)
            np.maximum(fmax[i], f, out=fmax[i])
            np.minimum(fmin[i], f, out=fmin[i])
            fsum[i] += f
            flast[i] = f
            fcnt[i] += 1

        if (n & frame_mask) == frame_mask:
            fr = n >> cfg.frame_log2
            cnt = np.maximum(fcnt, 1)[:, None]
            out[:, fr, :, 0] = fmax.T
            out[:, fr, :, 1] = fmin.T
            out[:, fr, :, 2] = ((fsum + cnt // 2) // cnt).astype(np.int32).T
            out[:, fr, :, 3] = flast.T
            fmax[:] = 0
            fmin[:] = cfg.feat_max
            fsum[:] = 0
            fcnt[:] = 0
    return out


# ---------------------------------------------------------------------------
# Batch driver over a cached clip set, mirroring extract_clips.py phase 2
# ---------------------------------------------------------------------------
_F: dict = {}


def _init(cfg_d, frames, clips_path):
    _F["cfg"] = wwhw.HWConfig(**cfg_d)
    _F["frames"] = frames
    _F["clips"] = np.load(clips_path, mmap_mode="r")


def _run(span):
    lo, hi = span
    a = np.asarray(_F["clips"][lo:hi], dtype=np.float32) / 32768.0
    return frontend_stats(a, _F["cfg"], _F["frames"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="only used to name the clip cache")
    ap.add_argument("--cache-tag", default="")
    ap.add_argument("--tag", required=True, help="output feature tag")
    ap.add_argument("--stats", default="max,min",
                    help=f"comma-separated subset of {','.join(STATS)}, in order")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--nstage", type=int, default=9)
    ap.add_argument("--nband", type=int, default=6)
    ap.add_argument("--tap0", type=int, default=3)
    ap.add_argument("--state-w", dest="state_w", type=int, default=10)
    ap.add_argument("--mant", type=int, default=1)
    ap.add_argument("--feat-w", dest="feat_w", type=int, default=4)
    ap.add_argument("--k-shift", dest="k_shift", type=int, default=2)
    ap.add_argument("--frame-log2", dest="frame_log2", type=int, default=16)
    args = ap.parse_args()

    want = [s.strip() for s in args.stats.split(",") if s.strip()]
    bad = [s for s in want if s not in STATS]
    if bad:
        raise SystemExit(f"unknown statistic(s) {bad}; known: {list(STATS)}")
    planes = [STATS.index(s) for s in want]

    cfg = wwhw.HWConfig(nstage=args.nstage, nband=args.nband, tap0=args.tap0,
                        state_w=args.state_w, mant=args.mant, feat_w=args.feat_w,
                        k_shift=args.k_shift, frame_log2=args.frame_log2,
                        nphase=2, score_w=10)
    prefix = os.path.join(CACHE, f"cache_{args.cache_tag or args.task}")
    meta = np.load(prefix + "_meta.npz")
    labels, splits, index = meta["labels"], meta["splits"], meta["index"]
    n = len(labels)

    spans = [(i, min(i + args.batch, n)) for i in range(0, n, args.batch)]
    feats = np.zeros((n, args.frames, cfg.nband * len(planes)), dtype=np.uint8)
    print(f"{args.tag}: {n} clips, {args.frames} frames, {cfg.nband} bands x "
          f"{len(planes)} statistic(s) {want} -> {cfg.nband*len(planes)} features/frame",
          flush=True)
    t0 = time.time()
    with mp.Pool(args.jobs, initializer=_init,
                 initargs=(cfg.to_dict(), args.frames, prefix + "_clips.npy")) as pool:
        done = 0
        for span, out in zip(spans, pool.imap(_run, spans)):
            # (b, f, band, stat) -> (b, f, band*stat) with band-major order, so
            # a template row stays contiguous per band.
            feats[span[0]:span[1]] = out[:, :, :, planes].reshape(
                out.shape[0], args.frames, -1)
            done += len(out)
            if (done // args.batch) % 10 == 0 or done == n:
                el = time.time() - t0
                print(f"  {done}/{n}  {el:6.0f}s  eta {el/done*(n-done):6.0f}s",
                      flush=True)

    # nband is overwritten with the *feature* count so probe.py and qat.py,
    # which both read cfg.nband as "features per frame", need no change.
    d = cfg.to_dict()
    d["nband"] = cfg.nband * len(planes)
    out = os.path.join(ART, f"ww_feats_{args.tag}.npz")
    np.savez_compressed(out, feats=feats, labels=labels, splits=splits, index=index,
                        gains=meta["gains"], targets=np.array([args.task]),
                        stats=np.array(want), cfg=json.dumps(d))
    print(f"wrote {os.path.relpath(out, ROOT)}  "
          f"range {feats.min()}..{feats.max()}  mean {feats.mean():.2f}")


if __name__ == "__main__":
    main()
