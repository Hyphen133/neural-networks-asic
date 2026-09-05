"""Corpus-independent plumbing for building a binary task the chip can learn.

The DADS pipeline (``dadsdata.py`` + ``extract_dads.py``) proved out a recipe:
1 s clips at 16 kHz, peak-normalised then attenuated by a per-clip log-uniform
gain, cut from longer recordings, split *by recording* rather than by clip.
Everything in that recipe except "how do I enumerate DADS" is generic, so it
lives here and each new corpus only has to produce a list of :class:`Item`.

A task is then:

    items = corpus.index(task)         # path, label, group, official split
    -> train/extract_clips.py          # cut, cache, run the front end
    -> artifacts/ww_feats_<tag>.npz    # what train/optim/qat.py already eats

Row length is fixed at 1 s / 24 frames, exactly as sheila and drone, so the
cached features drop into the existing trainer unchanged.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dadsdata import (AUDIO_HZ, CLIP_LEN, loop_tile, peak_normalise,  # noqa: E402
                      segments, synthetic_silence)

TRAIN, VAL, TEST = 0, 1, 2
SPLIT_NAMES = ("train", "val", "test")


@dataclass(frozen=True)
class Item:
    """One source recording, before it is cut into the clips the chip sees."""

    path: str            # absolute path to an audio file
    label: int           # 1 = the task's positive class
    group: str           # recording / uploader / fold identity -- splits respect it
    split: int           # 0/1/2, from the corpus' official split where one exists


# ---------------------------------------------------------------------------
# Determinism. Anything that could differ between two runs of the same command
# is a hash of stable identifiers, never an RNG draw.
# ---------------------------------------------------------------------------
def _h(*parts) -> int:
    return int(hashlib.sha1("-".join(str(p) for p in parts).encode()).hexdigest(), 16)


def hash_split(group: str, val_pct: int = 10, test_pct: int = 10) -> int:
    """Stable group -> train/val/test. Only for corpora with no official split."""
    h = _h("split", group) % 100
    if h < val_pct:
        return VAL
    if h < val_pct + test_pct:
        return TEST
    return TRAIN


def stratified_split(members: dict, val_pct: int = 10, test_pct: int = 10,
                     splits=(TRAIN, VAL, TEST)) -> dict:
    """Assign whole groups to splits so every *class* hits its target share.

    ``members`` maps a group id to its per-label counts, e.g.
    ``{"cry:0001": {1: 12}, "musan:noise-free-sound-0003": {0: 1}}``.

    A plain hash of the group id is recording-disjoint but not stratified: with
    a few hundred groups the class balance of a 10 % validation split drifts by
    several points, and for a corpus whose groups differ wildly in size (MUSAN
    files run from seconds to minutes) it drifts further. This walks the groups
    in hash order -- so the result is deterministic and independent of corpus
    file order -- and puts each one where the *deficit* against target is
    largest, counted per label.

    Only for corpora with no official split. Never used to override one.
    """
    target = {TRAIN: (100 - val_pct - test_pct) / 100,
              VAL: val_pct / 100, TEST: test_pct / 100}
    total = {}
    for counts in members.values():
        for lab, n in counts.items():
            total[lab] = total.get(lab, 0) + n
    have = {s: {lab: 0 for lab in total} for s in splits}
    out = {}
    for g in sorted(members, key=group_hash):
        counts = members[g]
        # The split that is furthest below its quota for this group's labels.
        def deficit(s):
            return sum(target[s] * total[lab] - have[s][lab]
                       for lab, n in counts.items() if total[lab])
        pick = max(splits, key=lambda s: (deficit(s), -s))
        out[g] = pick
        for lab, n in counts.items():
            have[pick][lab] += n
    return out


def group_hash(group: str) -> int:
    """int32 handle on the group, so the split can be audited from the npz alone."""
    return _h("group", group) % (2**31 - 1)


def keep_fraction(key: str, frac: float) -> bool:
    """Deterministic subsampling of a class that would otherwise dominate."""
    if frac >= 1.0:
        return True
    return (_h("keep", key) % 2**32) / 2**32 < frac


def clip_gain(key: str, gain_min: float) -> float:
    """Log-uniform gain in [gain_min, 1], fixed per segment.

    A 4-bit log feature moves 4 steps over 12 dB. Without this the detector can
    key on absolute level, which is a property of the recording rather than of
    the sound.
    """
    if gain_min >= 1.0:
        return 1.0
    u = (_h("gain", key) % 2**32) / 2**32
    return float(np.exp(np.log(gain_min) * u))


# ---------------------------------------------------------------------------
# Audio I/O: anything soundfile can open -> 16 kHz mono float32
# ---------------------------------------------------------------------------
MAX_SECONDS = 120          # MUSAN and HumBugDB hold recordings minutes long


def read_audio(path: str, target_hz: int = AUDIO_HZ,
               max_seconds: float = MAX_SECONDS) -> np.ndarray:
    """Decode to 16 kHz mono float32, reading at most `max_seconds`.

    Only a handful of 1 s windows is ever taken from one recording, so
    decoding a ten-minute MUSAN file in full is pure I/O.
    """
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        x = f.read(frames=int(max_seconds * sr) if max_seconds else -1,
                   dtype="float32", always_2d=True)
    x = x.mean(1)
    if sr != target_hz:
        g = int(np.gcd(int(sr), int(target_hz)))
        x = resample_poly(x, target_hz // g, sr // g)
    return np.ascontiguousarray(x, dtype=np.float32)


def read_duration(path: str) -> float:
    info = sf.info(path)
    return info.frames / float(info.samplerate)


# ---------------------------------------------------------------------------
# Source recording -> the 1 s clips the chip sees
# ---------------------------------------------------------------------------
def loudest_segments(x: np.ndarray, n: int, length: int = CLIP_LEN) -> list[np.ndarray]:
    """The `n` most energetic non-overlapping `length`-sample windows.

    Positives in an event corpus are weakly labelled: a 10 s clip tagged "Bark"
    is mostly the silence around a bark. Evenly spaced segments would label that
    silence positive. Ranking candidate windows by energy is blunt but honest --
    it cannot invent an event, it only declines to label the gaps.
    """
    if len(x) <= length:
        return [loop_tile(x, length)]
    hop = length // 2
    starts = np.arange(0, len(x) - length + 1, hop)
    e = np.array([float(x[s:s + length] @ x[s:s + length]) for s in starts])
    picked: list[int] = []
    for s in starts[np.argsort(-e)]:
        if len(picked) >= n:
            break
        if all(abs(int(s) - p) >= length for p in picked):
            picked.append(int(s))
    return [x[s:s + length] for s in sorted(picked)] or [x[:length]]


def cut(item: Item, x: np.ndarray, *, pos_segs: int, neg_segs: int,
        gain_min: float, pos_aug: int) -> list[tuple[np.ndarray, float]]:
    """One decoded recording -> the (clip, gain) pairs it contributes.

    Positives take the loudest windows and are repeated at independent gains
    (event classes are small; the negative class never is). Negatives take
    evenly spaced windows, near-silent ones dropped by ``segments``.
    """
    raw = (loudest_segments(x, pos_segs) if item.label else segments(x, neg_segs))
    out: list[tuple[np.ndarray, float]] = []
    for k, s in enumerate(raw):
        for r in range(pos_aug if item.label else 1):
            out.append((s, clip_gain(f"{item.path}|{k}|{r}", gain_min)))
    return out


def to_int16(clip: np.ndarray, gain: float, target: float = 0.7) -> np.ndarray:
    """Peak-normalise, attenuate, quantise -- the cache format extract_* uses."""
    y = clip / max(float(np.abs(clip).max()), 1e-3) * target * gain
    return np.clip(y * 32767, -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def describe(labels: np.ndarray, splits: np.ndarray, groups: np.ndarray) -> str:
    lines = [f"{len(labels)} clips, {int((labels > 0).sum())} positive / "
             f"{int((labels == 0).sum())} negative"]
    for s, name in enumerate(SPLIT_NAMES):
        m = splits == s
        lines.append(f"  {name:5s} {int(m.sum()):6d}  positive {int((labels[m] > 0).sum()):6d}"
                     f"  groups {len(np.unique(groups[m])) if m.any() else 0:5d}")
    shared: set[int] = set()
    for a in range(3):
        for b in range(a + 1, 3):
            shared |= (set(np.unique(groups[splits == a]).tolist())
                       & set(np.unique(groups[splits == b]).tolist()))
    lines.append(f"  groups shared between splits: {len(shared)}"
                 f"{'   <-- LEAK' if shared else '   (disjoint)'}")
    return "\n".join(lines)


__all__ = ["Item", "TRAIN", "VAL", "TEST", "SPLIT_NAMES", "AUDIO_HZ", "CLIP_LEN",
           "hash_split", "stratified_split", "group_hash", "keep_fraction",
           "clip_gain", "read_audio", "read_duration",
           "loudest_segments", "cut", "to_int16", "describe",
           "peak_normalise", "synthetic_silence", "loop_tile", "segments"]
