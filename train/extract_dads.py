"""Run the bit-exact hardware front end over DADS and cache features.

Same output format as extract.py, so train_sheila.py trains on it unchanged:

    python train/extract_dads.py --pos-frac 0.35 --neg-segs 4 --jobs 16

Writes artifacts/ww_feats_<tag>.npz with feats (N, 24, NBAND) uint8, labels
(1 = drone), splits (0/1/2, recording-disjoint), cfg (the shipped front end).

Two phases, because the front end is a per-PDM-tick Python loop whose cost is
almost independent of batch size (see FINDINGS.md): decoding is cheap and
parallel per parquet row group; the front end then runs on 1024-clip batches.
Decoded 1 s clips are cached as int16 under artifacts/data/dads/, so a second
extraction with different front-end parameters skips the parquet pass.

Level augmentation: each clip is peak-normalised to 0.7 and then attenuated
by a deterministic log-uniform gain in [--gain-min, 1]. Drones at a distance
are quiet, and a 4-bit log feature moves 4 steps over 12 dB, so the detector
has to learn the *shape* of the spectrum rather than its level.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dadsdata  # noqa: E402
import wwhw  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")
CACHE = os.path.join(dadsdata.ROOT, "..")
NFRAME_EXT = 24                  # 24 x 41.9 ms = 1.006 s, the whole clip

# The shipped reduced front end (FINDINGS.md): what tapes out.
SHIPPED = dict(nstage=9, nband=5, tap0=4, state_w=10, mant=1, feat_w=4,
               nphase=2, score_w=10)


def clip_gain(label: int, index: int, seg: int, gain_min: float) -> float:
    """Deterministic log-uniform gain in [gain_min, 1] per (clip, segment)."""
    if gain_min >= 1.0:
        return 1.0
    h = int(hashlib.sha1(f"g{label}-{index}-{seg}".encode()).hexdigest()[:8], 16)
    u = h / 2**32
    return float(np.exp(np.log(gain_min) * u))


# ---------------------------------------------------------------------------
# Phase 1: parquet row group -> 1 s int16 clips
# ---------------------------------------------------------------------------
_A = {}


def _decode_init(args_d):
    _A.update(args_d)


def _decode_rowgroup(task):
    """(file, rowgroup) -> (clips int16 (n, CLIP_LEN), labels, splits, index, gains)."""
    path, rg = task
    t = pq.ParquetFile(path).read_row_group(rg, columns=["audio", "label"])
    audio = t.column("audio").to_pylist()
    labels = t.column("label").to_pylist()
    clips, lab, spl, idx, gains = [], [], [], [], []
    for a, l in zip(audio, labels):
        label, index = dadsdata.parse_name(a["path"])
        if label != int(l):
            raise ValueError(f"name/label disagree for {a['path']}: {l}")
        if label and not dadsdata.keep_fraction(index, _A["pos_frac"]):
            continue
        split = dadsdata.which_split(label, index)
        x = dadsdata.decode_wav(a["bytes"])
        segs = (dadsdata.segments(x, _A["neg_segs"]) if label == 0
                else [dadsdata.loop_tile(x)])
        for k, s in enumerate(segs):
            g = clip_gain(label, index, k, _A["gain_min"])
            y = dadsdata.peak_normalise(s) * g
            clips.append(np.clip(y * 32767, -32768, 32767).astype(np.int16))
            lab.append(label); spl.append(split); idx.append(index); gains.append(g)
    if not clips:
        return None
    return (np.stack(clips), np.array(lab, np.int8), np.array(spl, np.int8),
            np.array(idx, np.int32), np.array(gains, np.float16))


def decode_all(args, cache_prefix):
    files = dadsdata.parquet_files()
    if not files:
        sys.exit(f"no parquet shards under {dadsdata.ROOT}; download DADS first")
    tasks = [(f, rg) for f in files for rg in range(pq.ParquetFile(f).num_row_groups)]
    print(f"phase 1: decoding {len(files)} shards, {len(tasks)} row groups, "
          f"{args.jobs} jobs", flush=True)
    parts = []
    t0 = time.time()
    with mp.Pool(args.jobs, initializer=_decode_init,
                 initargs=(dict(pos_frac=args.pos_frac, neg_segs=args.neg_segs,
                                gain_min=args.gain_min),)) as pool:
        for k, out in enumerate(pool.imap(_decode_rowgroup, tasks, chunksize=2)):
            if out is not None:
                parts.append(out)
            if k % 200 == 0 or k == len(tasks) - 1:
                n = sum(len(p[1]) for p in parts)
                print(f"  {k+1}/{len(tasks)} row groups  {n} clips  "
                      f"{time.time()-t0:5.0f}s", flush=True)
    clips = np.concatenate([p[0] for p in parts])
    labels = np.concatenate([p[1] for p in parts]).astype(np.int64)
    splits = np.concatenate([p[2] for p in parts]).astype(np.int64)
    index = np.concatenate([p[3] for p in parts])
    gains = np.concatenate([p[4] for p in parts])

    if args.silence:
        sil = dadsdata.synthetic_silence(args.silence, seed=1)
        clips = np.concatenate([clips, (sil * 32767).astype(np.int16)])
        labels = np.concatenate([labels, np.zeros(args.silence, np.int64)])
        # spread the synthetic negatives over the three splits the same way
        splits = np.concatenate([splits, np.array(
            [dadsdata.which_split(0, 10**7 + i) for i in range(args.silence)])])
        index = np.concatenate([index, -np.arange(1, args.silence + 1, dtype=np.int32)])
        gains = np.concatenate([gains, np.ones(args.silence, np.float16)])

    mm = np.lib.format.open_memmap(cache_prefix + "_clips.npy", mode="w+",
                                   dtype=np.int16, shape=clips.shape)
    mm[:] = clips
    mm.flush()
    del mm                                   # close before the workers reopen it
    np.savez(cache_prefix + "_meta.npz", labels=labels, splits=splits,
             index=index, gains=gains,
             args=json.dumps(dict(pos_frac=args.pos_frac, neg_segs=args.neg_segs,
                                  gain_min=args.gain_min, silence=args.silence)))
    print(f"phase 1 done: {len(labels)} clips cached at {os.path.relpath(cache_prefix)}_*")


# ---------------------------------------------------------------------------
# Phase 2: int16 clips -> front-end features
# ---------------------------------------------------------------------------
_F = {}


def _fe_init(cfg_d, frames, clips_path):
    _F["cfg"] = wwhw.HWConfig(**cfg_d)
    _F["frames"] = frames
    _F["clips"] = np.load(clips_path, mmap_mode="r")


def _fe_run(span):
    lo, hi = span
    audio = np.asarray(_F["clips"][lo:hi], dtype=np.float32) / 32768.0
    return wwhw.frontend_batch(audio, _F["cfg"], n_frames=_F["frames"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="dads")
    ap.add_argument("--pos-frac", type=float, default=0.35,
                    help="fraction of the 163k drone slices to keep (deterministic)")
    ap.add_argument("--neg-segs", type=int, default=4,
                    help="1 s segments per 10 s non-drone clip")
    ap.add_argument("--gain-min", type=float, default=0.25,
                    help="lower end of the log-uniform level augmentation (0.25 = -12 dB)")
    ap.add_argument("--silence", type=int, default=2000,
                    help="synthetic room-tone negatives")
    ap.add_argument("--frames", type=int, default=NFRAME_EXT)
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--jobs", type=int, default=max(1, os.cpu_count() - 2))
    ap.add_argument("--limit", type=int, default=0, help="debug: only this many clips")
    ap.add_argument("--redecode", action="store_true", help="ignore the clip cache")
    args = ap.parse_args()

    cfg = wwhw.HWConfig(**SHIPPED)
    cache_prefix = os.path.join(CACHE, f"cache_{args.tag}")
    if args.redecode or not os.path.exists(cache_prefix + "_meta.npz"):
        decode_all(args, cache_prefix)
    meta = np.load(cache_prefix + "_meta.npz")
    labels, splits = meta["labels"], meta["splits"]
    n = len(labels) if not args.limit else min(args.limit, len(labels))
    print(dadsdata.describe_rows([dadsdata.Row(int(l), 0, int(s))
                                  for l, s in zip(labels[:n], splits[:n])]))
    print(wwhw.describe(cfg))

    spans = [(i, min(i + args.batch, n)) for i in range(0, n, args.batch)]
    feats = np.zeros((n, args.frames, cfg.nband), dtype=np.uint8)
    print(f"phase 2: front end over {n} clips, {len(spans)} batches, {args.jobs} jobs",
          flush=True)
    t0 = time.time()
    with mp.Pool(args.jobs, initializer=_fe_init,
                 initargs=(cfg.to_dict(), args.frames, cache_prefix + "_clips.npy")) as pool:
        done = 0
        for span, out in zip(spans, pool.imap(_fe_run, spans)):
            feats[span[0]:span[1]] = out
            done += len(out)
            el = time.time() - t0
            print(f"  {done}/{n}  {el:6.0f}s  eta {el/done*(n-done):6.0f}s", flush=True)

    os.makedirs(ART, exist_ok=True)
    out = os.path.join(ART, f"ww_feats_{args.tag}.npz")
    np.savez_compressed(out, feats=feats, labels=labels[:n], splits=splits[:n],
                        index=meta["index"][:n], gains=meta["gains"][:n],
                        targets=np.array(["drone"]), cfg=json.dumps(cfg.to_dict()))
    print(f"\nwrote {os.path.relpath(out)}  {feats.nbytes/1e6:.1f} MB uncompressed")
    print(f"feature range {feats.min()}..{feats.max()}  mean {feats.mean():.2f}")


if __name__ == "__main__":
    main()
