"""DADS -- Drone Audio Detection Samples -- loader (MIT licence).

    https://huggingface.co/datasets/geronimobasso/drone-audio-detection-samples

180 320 rows in 39 parquet shards: 163 591 drone clips (median 0.5 s, slices of
longer recordings) and 16 729 non-drone clips (10 s, UrbanSound8K / TUT /
ESC-50 / DNC). One label, no official split, 16 kHz 16-bit mono WAV blobs.

Two things the loader has to get right, both learned on the sheila spotter:

  * **A split that is disjoint at the *recording* level.** Rows are named
    `drone-N.wav`; consecutive N are consecutive slices of the same source
    recording, so a random split would put slice N in train and slice N+1 in
    test and inflate every number. The split is by blocks of N instead.
  * **Clips the chip actually sees.** The detector looks at 16 x 42 ms
    windows. A 0.5 s drone slice is shorter than one window, so it is
    loop-tiled to 1 s (rotor noise is quasi-stationary; the seam is
    harmless). 10 s non-drone clips are cut into several 1 s segments.

Only needs pyarrow, `wave` and numpy.
"""

from __future__ import annotations

import glob
import hashlib
import io
import os
import re
import wave
from dataclasses import dataclass

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "artifacts", "data", "dads", "data")
AUDIO_HZ = 16_000
CLIP_LEN = AUDIO_HZ                       # 1 s, same as the sheila pipeline

# Consecutive file indices come from the same recording. Block sizes are a
# judgement call: drone slices are 0.5 s so a block of 400 spans ~200 s of
# one source; non-drone clips are 10 s so 40 is a similar span.
BLOCK_POS = 400
BLOCK_NEG = 40

_NAME = re.compile(r"^(no-drone|drone)-(\d+)\.wav$")


@dataclass(frozen=True)
class Row:
    label: int          # 1 = drone
    index: int          # N from the file name
    split: int          # 0/1/2 = train/val/test


def parquet_files() -> list[str]:
    return sorted(glob.glob(os.path.join(ROOT, "*.parquet")))


def parse_name(path: str) -> tuple[int, int]:
    """'drone-83355.wav' -> (1, 83355); 'no-drone-100.wav' -> (0, 100)."""
    m = _NAME.match(os.path.basename(path or ""))
    if not m:
        raise ValueError(f"unexpected DADS file name {path!r}")
    return (1 if m.group(1) == "drone" else 0), int(m.group(2))


def which_split(label: int, index: int) -> int:
    """Hash of the recording block -> 0/1/2. Stable, recording-disjoint."""
    block = index // (BLOCK_POS if label else BLOCK_NEG)
    h = int(hashlib.sha1(f"{label}-{block}".encode()).hexdigest(), 16) % 100
    return 1 if h < 10 else (2 if h < 20 else 0)


def keep_fraction(index: int, frac: float) -> bool:
    """Deterministic per-row subsampling (Knuth multiplicative hash)."""
    if frac >= 1.0:
        return True
    return ((index * 2654435761) & 0xFFFFFFFF) / 2**32 < frac


def decode_wav(blob: bytes) -> np.ndarray:
    """16-bit PCM mono WAV bytes -> float32 in [-1, 1]. Validates the header."""
    with wave.open(io.BytesIO(blob)) as w:
        if w.getsampwidth() != 2 or w.getframerate() != AUDIO_HZ:
            raise ValueError(f"unexpected WAV format {w.getparams()}")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(1)
    return x.astype(np.float32) / 32768.0


def loop_tile(x: np.ndarray, length: int = CLIP_LEN) -> np.ndarray:
    """Repeat a short clip until it fills `length` samples."""
    if len(x) == 0:
        return np.zeros(length, dtype=np.float32)
    reps = -(-length // len(x))
    return np.tile(x, reps)[:length]


def segments(x: np.ndarray, n_max: int, length: int = CLIP_LEN,
             min_peak: float = 0.01) -> list[np.ndarray]:
    """Up to n_max evenly spaced `length`-sample segments of a long clip.

    Near-silent segments are dropped: peak-normalising them would turn
    quantisation noise into a full-scale 'sound' the chip never sees.
    """
    if len(x) <= length:
        return [loop_tile(x, length)]
    n = max(1, min(n_max, len(x) // length))
    starts = np.linspace(0, len(x) - length, n).astype(int)
    out = [x[s:s + length] for s in starts]
    return [s for s in out if np.abs(s).max() >= min_peak]


def peak_normalise(a: np.ndarray, target: float = 0.7) -> np.ndarray:
    return a / np.maximum(np.abs(a).max(-1, keepdims=True), 1e-3) * target


def synthetic_silence(n: int, seed: int = 0, peak: float = 0.02) -> np.ndarray:
    """Room-tone negatives so the detector has seen 'nothing' during training."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, CLIP_LEN)).astype(np.float32)
    return peak_normalise(x, peak)


def describe_rows(rows: list[Row]) -> str:
    lab = np.array([r.label for r in rows])
    spl = np.array([r.split for r in rows])
    lines = [f"{len(rows)} clips, {int(lab.sum())} drone / {int((lab == 0).sum())} non-drone"]
    for s, name in enumerate(["train", "val", "test"]):
        m = spl == s
        lines.append(f"  {name:5s} {int(m.sum()):6d}  drone {int(lab[m].sum()):6d}")
    return "\n".join(lines)
