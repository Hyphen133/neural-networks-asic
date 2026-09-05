"""Run the bit-exact hardware front end over a file-based corpus and cache it.

The generic sibling of ``extract_dads.py``: that one knows how to read DADS
parquet, this one takes any task registered in ``train/tasks.py`` and produces
the same ``artifacts/ww_feats_<tag>.npz`` that ``train/optim/qat.py`` trains on.

    python train/extract_clips.py --task dogbark --jobs 24

Two phases, for the same reason as DADS: decoding is cheap and embarrassingly
parallel, the front end is a per-PDM-tick loop whose cost barely depends on
batch size. Decoded 1 s clips are cached as int16, so re-extracting with
different front-end parameters skips the decode pass entirely.

Front-end defaults are the *current* sheila geometry (TAP0=3, NBAND=6): bands
3..8 span 243 Hz - 15.5 kHz and are a strict superset of the drone geometry's
bands 4..8, so one extraction serves both.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipset  # noqa: E402
import tasks  # noqa: E402
import wwhw  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")
CACHE = os.path.join(ART, "data")
NFRAME_EXT = 24                  # 24 x 41.9 ms = 1.006 s, the whole clip

# The geometry that hardened as runs/sheila_nb6 (docs/nn_optimization.md).
GEOMETRY = dict(nstage=9, nband=6, tap0=3, state_w=10, mant=1, feat_w=4,
                k_shift=2, frame_log2=16, nphase=2, score_w=10)


# ---------------------------------------------------------------------------
# Phase 1: source files -> 1 s int16 clips
# ---------------------------------------------------------------------------
_A: dict = {}


def _decode_init(cut_kw):
    _A["cut"] = cut_kw


def _decode_batch(items: list[clipset.Item]):
    clips, lab, spl, grp, gains, bad = [], [], [], [], [], 0
    for it in items:
        try:
            x = clipset.read_audio(it.path)
        except Exception:                       # a corrupt file must not kill a run
            bad += 1
            continue
        gh = clipset.group_hash(it.group)
        for clip, g in clipset.cut(it, x, **_A["cut"]):
            clips.append(clipset.to_int16(clip, g))
            lab.append(it.label); spl.append(it.split); grp.append(gh); gains.append(g)
    if not clips:
        return None
    return (np.stack(clips), np.array(lab, np.int8), np.array(spl, np.int8),
            np.array(grp, np.int32), np.array(gains, np.float16), bad)


def decode_all(items, args, cache_prefix):
    cut_kw = dict(pos_segs=args.pos_segs, neg_segs=args.neg_segs,
                  gain_min=args.gain_min, pos_aug=args.pos_aug)
    batches = [items[i:i + 64] for i in range(0, len(items), 64)]
    print(f"phase 1: decoding {len(items)} source files in {len(batches)} batches, "
          f"{args.jobs} jobs", flush=True)
    parts, nbad, t0 = [], 0, time.time()
    with mp.Pool(args.jobs, initializer=_decode_init, initargs=(cut_kw,)) as pool:
        for k, out in enumerate(pool.imap_unordered(_decode_batch, batches)):
            if out is not None:
                parts.append(out[:5]); nbad += out[5]
            if k % 50 == 0 or k == len(batches) - 1:
                n = sum(len(p[1]) for p in parts)
                print(f"  {k+1}/{len(batches)} batches  {n} clips  "
                      f"{time.time()-t0:5.0f}s", flush=True)
    if nbad:
        print(f"  warning: {nbad} files could not be decoded and were skipped")
    clips = np.concatenate([p[0] for p in parts])
    labels = np.concatenate([p[1] for p in parts]).astype(np.int64)
    splits = np.concatenate([p[2] for p in parts]).astype(np.int64)
    index = np.concatenate([p[3] for p in parts])
    gains = np.concatenate([p[4] for p in parts])

    if args.silence:
        # "Nothing is happening" has to be in the training set or the detector
        # has never seen the condition it spends most of its life in.
        sil = clipset.synthetic_silence(args.silence, seed=1)
        clips = np.concatenate([clips, (sil * 32767).astype(np.int16)])
        labels = np.concatenate([labels, np.zeros(args.silence, np.int64)])
        splits = np.concatenate([splits, np.array(
            [clipset.hash_split(f"silence-{i}") for i in range(args.silence)])])
        index = np.concatenate([index, -np.arange(1, args.silence + 1, dtype=np.int32)])
        gains = np.concatenate([gains, np.ones(args.silence, np.float16)])

    os.makedirs(os.path.dirname(cache_prefix), exist_ok=True)
    mm = np.lib.format.open_memmap(cache_prefix + "_clips.npy", mode="w+",
                                   dtype=np.int16, shape=clips.shape)
    mm[:] = clips
    mm.flush()
    del mm                                      # close before the workers reopen it
    np.savez(cache_prefix + "_meta.npz", labels=labels, splits=splits, index=index,
             gains=gains, args=json.dumps(dict(cut_kw, silence=args.silence)))
    print(f"phase 1 done: {len(labels)} clips cached at "
          f"{os.path.relpath(cache_prefix)}_*", flush=True)


# ---------------------------------------------------------------------------
# Phase 2: int16 clips -> front-end features
# ---------------------------------------------------------------------------
_F: dict = {}


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
    ap.add_argument("--task", required=True, choices=sorted(tasks.TASKS))
    ap.add_argument("--tag", default="", help="feature tag; default = the task name")
    ap.add_argument("--cache-tag", default="",
                    help="reuse another tag's decoded 1 s clip cache. A new front "
                         "end changes only phase 2, so a geometry sweep must not "
                         "re-decode the corpus once per variant; default = --tag")
    ap.add_argument("--pos-segs", type=int, default=3,
                    help="loudest 1 s windows taken from each positive recording")
    ap.add_argument("--neg-segs", type=int, default=0,
                    help="evenly spaced 1 s windows per negative recording; "
                         "0 = choose it so the two classes end up balanced")
    ap.add_argument("--max-neg-segs", type=int, default=8,
                    help="ceiling for the automatic --neg-segs")
    ap.add_argument("--pos-aug", type=int, default=2,
                    help="copies of each positive window at independent gains")
    ap.add_argument("--gain-min", type=float, default=0.25,
                    help="lower end of the log-uniform level augmentation (0.25 = -12 dB)")
    ap.add_argument("--silence", type=int, default=2000,
                    help="synthetic room-tone negatives")
    ap.add_argument("--frames", type=int, default=0, help=f"0 = {NFRAME_EXT}")
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--limit", type=int, default=0, help="debug: only this many clips")
    ap.add_argument("--redecode", action="store_true", help="ignore the clip cache")
    for k, dv in GEOMETRY.items():
        ap.add_argument(f"--{k.replace('_', '-')}", dest=k, type=int, default=dv)
    args = ap.parse_args()

    cfg = wwhw.HWConfig(**{k: getattr(args, k) for k in GEOMETRY})
    if not args.frames:
        args.frames = (NFRAME_EXT << (wwhw.FRAME_LOG2 - cfg.frame_log2)
                       if cfg.frame_log2 <= wwhw.FRAME_LOG2
                       else NFRAME_EXT >> (cfg.frame_log2 - wwhw.FRAME_LOG2))
    tag = args.tag or args.task
    cache_prefix = os.path.join(CACHE, f"cache_{args.cache_tag or tag}")

    if args.redecode or not os.path.exists(cache_prefix + "_meta.npz"):
        spec = tasks.TASKS[args.task]
        items = spec.build()
        print(spec.summarise(items), flush=True)
        if not args.neg_segs:
            # Positives are scarce and are used pos_segs*pos_aug times each;
            # negatives are plentiful. Take just enough windows from each
            # negative recording that the two classes end up near balance.
            n_pos = sum(1 for i in items if i.label)
            n_neg = len(items) - n_pos
            args.neg_segs = max(1, min(args.max_neg_segs, round(
                n_pos * args.pos_segs * args.pos_aug / max(n_neg, 1))))
            print(f"  auto --neg-segs {args.neg_segs} "
                  f"({n_pos} positive / {n_neg} negative recordings)", flush=True)
        decode_all(items, args, cache_prefix)

    meta = np.load(cache_prefix + "_meta.npz")
    labels, splits, index = meta["labels"], meta["splits"], meta["index"]
    n = len(labels) if not args.limit else min(args.limit, len(labels))
    print(clipset.describe(labels[:n], splits[:n], index[:n]))
    print(wwhw.describe(cfg))

    spans = [(i, min(i + args.batch, n)) for i in range(0, n, args.batch)]
    feats = np.zeros((n, args.frames, cfg.nband), dtype=np.uint8)
    print(f"phase 2: front end over {n} clips, {len(spans)} batches, {args.jobs} jobs",
          flush=True)
    t0 = time.time()
    with mp.Pool(args.jobs, initializer=_fe_init,
                 initargs=(cfg.to_dict(), args.frames,
                           cache_prefix + "_clips.npy")) as pool:
        done = 0
        for span, out in zip(spans, pool.imap(_fe_run, spans)):
            feats[span[0]:span[1]] = out
            done += len(out)
            el = time.time() - t0
            if (done // args.batch) % 10 == 0 or done == n:
                print(f"  {done}/{n}  {el:6.0f}s  eta {el/done*(n-done):6.0f}s", flush=True)

    out = os.path.join(ART, f"ww_feats_{tag}.npz")
    np.savez_compressed(out, feats=feats, labels=labels[:n], splits=splits[:n],
                        index=index[:n], gains=meta["gains"][:n],
                        targets=np.array([args.task]), cfg=json.dumps(cfg.to_dict()))
    print(f"\nwrote {os.path.relpath(out)}  {feats.nbytes/1e6:.1f} MB uncompressed")
    print(f"feature range {feats.min()}..{feats.max()}  mean {feats.mean():.2f}")


if __name__ == "__main__":
    main()
