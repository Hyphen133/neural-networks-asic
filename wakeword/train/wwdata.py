"""Google Speech Commands v0.02 loader (CC BY 4.0).

    https://arxiv.org/abs/1804.03209
    http://download.tensorflow.org/data/speech_commands_v0.02.tar.gz

Only needs `wave` and numpy. Honours the dataset's official
validation_list.txt / testing_list.txt speaker-disjoint split, which matters:
a random split leaks the same speaker into train and test and inflates every
number by several points.
"""

from __future__ import annotations

import hashlib
import os
import re
import wave

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "artifacts", "data", "speech_commands")
AUDIO_HZ = 16_000
CLIP_LEN = AUDIO_HZ                      # 1 s

WORDS = sorted(w for w in (os.listdir(ROOT) if os.path.isdir(ROOT) else [])
               if os.path.isdir(os.path.join(ROOT, w)) and not w.startswith("_"))


def read_wav(path: str, length: int = CLIP_LEN) -> np.ndarray:
    """16-bit PCM mono -> float32 in [-1, 1], centre-padded/cropped to `length`."""
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, path
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if len(x) >= length:
        off = (len(x) - length) // 2
        return x[off:off + length]
    out = np.zeros(length, dtype=np.float32)
    out[(length - len(x)) // 2:][:len(x)] = x
    return out


def _which_set(rel: str) -> str:
    """The dataset's own hash-based split, so it is stable and speaker-disjoint."""
    name = os.path.basename(rel)
    speaker = re.sub(r"_nohash_.*$", "", name)
    h = int(hashlib.sha1(speaker.encode()).hexdigest(), 16) % 100
    if h < 10:
        return "val"
    if h < 20:
        return "test"
    return "train"


def index(targets: list[str], n_neg_per_word: int | None = None, seed: int = 0):
    """Build the file index.

    Returns list of (path, label, split); label 0 = not-a-keyword,
    1..len(targets) = the keyword's index + 1.
    """
    rng = np.random.default_rng(seed)
    items = []
    for w in WORDS:
        lab = targets.index(w) + 1 if w in targets else 0
        files = sorted(os.listdir(os.path.join(ROOT, w)))
        files = [f for f in files if f.endswith(".wav")]
        if lab == 0 and n_neg_per_word is not None and len(files) > n_neg_per_word:
            files = list(rng.choice(files, n_neg_per_word, replace=False))
        for f in files:
            rel = f"{w}/{f}"
            items.append((os.path.join(ROOT, rel), lab, _which_set(rel)))
    return items


def load_audio(items, jitter: float = 0.0, seed: int = 0) -> np.ndarray:
    """(N, CLIP_LEN) float32. `jitter` shifts each clip by up to +-jitter seconds."""
    rng = np.random.default_rng(seed)
    out = np.zeros((len(items), CLIP_LEN), dtype=np.float32)
    for i, (path, _, _) in enumerate(items):
        x = read_wav(path)
        if jitter:
            s = int(rng.integers(-jitter * AUDIO_HZ, jitter * AUDIO_HZ + 1))
            x = np.roll(x, s)
        out[i] = x
    return out


def background_noise() -> list[np.ndarray]:
    d = os.path.join(ROOT, "_background_noise_")
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".wav"):
            with wave.open(os.path.join(d, f), "rb") as w:
                raw = w.readframes(w.getnframes())
            out.append(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)
    return out
