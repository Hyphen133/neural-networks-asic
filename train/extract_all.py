"""Cache shipped-front-end features for *every* Speech Commands word.

The front end is fixed silicon and carries no notion of a keyword, so its
output can be extracted once and then re-labelled for any candidate wake word.
That is what makes a 35-way keyword sweep affordable: one extraction pass,
then one cheap training run per candidate (see sweep_words.py).

    python train/extract_all.py --per-word 1500 --jobs 8

Writes artifacts/ww_feats_allwords.npz with
    feats   (N, frames, nband) uint8
    word    (N,) int64   index into `words`
    splits  (N,) int64   0/1/2 = train/val/test, dataset's speaker-disjoint hash
    words   (35,) str
    cfg     json of the HWConfig used (defaults = the shipped reduced front end)
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

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")
NFRAME_EXT = 24

# The shipped reduced front end (see FINDINGS.md): NSTAGE=9, NBAND=5,
# STATE_W=10, FEAT_W=4, TAP0=4.
SHIPPED = dict(nstage=9, nband=5, tap0=4, state_w=10, mant=1, feat_w=4)

_G = {}


def peak_normalise(a: np.ndarray, target: float = 0.7) -> np.ndarray:
    return a / np.maximum(np.abs(a).max(1, keepdims=True), 1e-3) * target


def _init(cfg_d, frames):
    _G["cfg"] = wwhw.HWConfig(**cfg_d)
    _G["frames"] = frames


def _run(chunk):
    """chunk: list of (path, jitter_s). Returns (n, frames, nband) uint8."""
    cfg, frames = _G["cfg"], _G["frames"]
    audio = np.zeros((len(chunk), wwdata.CLIP_LEN), dtype=np.float32)
    for j, (path, jit) in enumerate(chunk):
        x = wwdata.read_wav(path)
        if jit:
            x = np.roll(x, int(jit * wwdata.AUDIO_HZ) * (1 if j % 2 else -1))
        audio[j] = x
    return wwhw.frontend_batch(peak_normalise(audio), cfg, n_frames=frames)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-word", type=int, default=1500,
                    help="cap clips per word so every candidate gets equal support")
    ap.add_argument("--frames", type=int, default=NFRAME_EXT)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--jitter", type=float, default=0.0,
                    help="if >0, add one time-jittered copy of every clip")
    ap.add_argument("--out", default="ww_feats_allwords")
    args = ap.parse_args()

    cfg = wwhw.HWConfig(**SHIPPED)
    words = wwdata.WORDS
    assert words, "speech_commands not found under artifacts/data"

    rng = np.random.default_rng(0)
    plan, word_id, split = [], [], []
    for wi, w in enumerate(words):
        files = sorted(f for f in os.listdir(os.path.join(wwdata.ROOT, w))
                       if f.endswith(".wav"))
        if args.per_word and len(files) > args.per_word:
            files = sorted(rng.choice(files, args.per_word, replace=False))
        for f in files:
            rel = f"{w}/{f}"
            plan.append((os.path.join(wwdata.ROOT, rel), 0.0))
            word_id.append(wi)
            split.append({"train": 0, "val": 1, "test": 2}[wwdata._which_set(rel)])
    n_clean = len(plan)
    if args.jitter:
        plan += [(p, args.jitter) for p, _ in plan[:n_clean]]
        word_id += word_id[:n_clean]
        split += split[:n_clean]

    n = len(plan)
    print(f"words={len(words)}  clips={n} ({n_clean} clean"
          + (f" + {n - n_clean} jittered)" if args.jitter else ")"))
    print(wwhw.describe(cfg))

    chunks = [plan[i:i + args.batch] for i in range(0, n, args.batch)]
    feats = np.zeros((n, args.frames, cfg.nband), dtype=np.uint8)
    t0 = time.time()
    if args.jobs > 1:
        import multiprocessing as mp
        with mp.Pool(args.jobs, initializer=_init,
                     initargs=(cfg.to_dict(), args.frames)) as pool:
            done = 0
            for k, out in enumerate(pool.imap(_run, chunks)):
                i = k * args.batch
                feats[i:i + len(out)] = out
                done += len(out)
                el = time.time() - t0
                print(f"  {done}/{n}  {el:6.0f}s  eta {el/done*(n-done):6.0f}s",
                      flush=True)
    else:
        _init(cfg.to_dict(), args.frames)
        for k, ch in enumerate(chunks):
            i = k * args.batch
            feats[i:i + len(ch)] = _run(ch)
            el = time.time() - t0
            print(f"  {i+len(ch)}/{n}  {el:6.0f}s", flush=True)

    os.makedirs(ART, exist_ok=True)
    out = os.path.join(ART, f"{args.out}.npz")
    np.savez_compressed(out, feats=feats,
                        word=np.array(word_id, dtype=np.int64),
                        splits=np.array(split, dtype=np.int64),
                        clean=np.array([1] * n_clean + [0] * (n - n_clean),
                                       dtype=np.int64),
                        words=np.array(words),
                        cfg=json.dumps(cfg.to_dict()))
    print(f"\nwrote {os.path.relpath(out)}   {feats.nbytes/1e6:.1f} MB raw")
    for s, name in enumerate(["train", "val", "test"]):
        m = np.array(split) == s
        print(f"  {name:5s} {int(m.sum()):6d}")


if __name__ == "__main__":
    main()
