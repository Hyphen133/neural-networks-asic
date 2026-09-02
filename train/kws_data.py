"""Google Speech Commands v0.02 -> integer feature frames for the wake-word tile.

No torchaudio, no soundfile: the dataset is 16-bit mono 16 kHz WAV, which the
standard-library `wave` module reads fine. That keeps the golden model and the
cocotb testbench dependency-free (numpy only), matching train/hw.py.

    python3 train/kws_data.py --prepare          # build the fine feature cache
    python3 train/kws_data.py --verify-pdm       # exact sigma-delta vs ideal ADC
    python3 train/kws_data.py --verify-derive    # derived vs native features
    python3 train/kws_data.py --report

Two cost facts drive the structure:

  * The sigma-delta microphone simulation is ~90 % of extraction time, so it
    runs ONCE per batch and its 48 kHz output feeds every filterbank ladder
    being cached.
  * Everything downstream of the ladder -- slot depth, frame rate, feature
    width -- is derived exactly from a single fine extraction by
    `kws_hw.derive_features`, so sweeping those costs nothing.

Splits use the dataset's own speaker-stable hash, so a speaker never straddles
train/val/test and numbers are comparable with published Speech Commands work.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kws_hw import (  # noqa: E402
    FS, PDM_HZ, KWSConfig, front_end, quantize_pcm, upsample_linear,
    pdm_decimate, fine_config, derive_features, FINE_FRAME_LOG2, FINE_FEAT_W,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
GSC = os.path.join(DATA, "gsc")
CACHE = os.path.join(DATA, "cache")

SR = 16000
CLIP_S = 1.5                       # padded clip length; room to time-shift
CLIP_N = int(SR * CLIP_S)
BG_DIR = "_background_noise_"

DEFAULT_NBANKS = (5, 7)


# ---------------------------------------------------------------------------
# WAV reading and the official split
# ---------------------------------------------------------------------------
def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, path
        assert w.getframerate() == SR, f"{path}: {w.getframerate()} Hz"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def which_set(filename: str, val_pct: float = 10.0, test_pct: float = 10.0) -> str:
    """The dataset's speaker-stable hash split, reimplemented.

    Taken from the Speech Commands release code rather than the *_list.txt
    files so that the words we subsample for the background class get the same
    treatment, and so one speaker's recordings never land in two splits.
    """
    speaker = os.path.basename(filename).split("_nohash_")[0].encode()
    h = int(hashlib.sha1(speaker).hexdigest(), 16)
    pct = (h % (2 ** 27 - 1)) * (100.0 / (2 ** 27 - 1))
    if pct < val_pct:
        return "val"
    if pct < val_pct + test_pct:
        return "test"
    return "train"


def list_words(root: str = GSC):
    return sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)) and not d.startswith("_"))


def load_noise(root: str = GSC):
    """The six long ambience recordings, concatenated."""
    d = os.path.join(root, BG_DIR)
    if not os.path.isdir(d):
        return np.zeros(SR, dtype=np.float32)
    parts = [read_wav(os.path.join(d, f)) for f in sorted(os.listdir(d))
             if f.endswith(".wav")]
    return np.concatenate(parts) if parts else np.zeros(SR, dtype=np.float32)


# ---------------------------------------------------------------------------
# Clip assembly + augmentation
# ---------------------------------------------------------------------------
def place(word_audio: np.ndarray, rng, n: int = CLIP_N, jitter: bool = True):
    """Drop a ~1 s utterance into an n-sample window at a random offset."""
    out = np.zeros(n, dtype=np.float32)
    w = word_audio[:n]
    room = n - len(w)
    off = int(rng.integers(0, room + 1)) if (jitter and room > 0) else room // 2
    out[off:off + len(w)] = w
    return out


def augment(clip: np.ndarray, noise: np.ndarray, rng,
            gain_db=(-18.0, 6.0), snr_db=(3.0, 30.0), p_noise: float = 0.8):
    """Gain and additive-ambience augmentation.

    The tile has a fixed 7-bit ADC and no AGC, so level robustness is not a
    nicety: an untrained-for 18 dB drop moves every log feature down by three
    whole steps. Training across the range is what makes the demo work at
    arm's length rather than only with the microphone at your lips.
    """
    clip = clip.copy()
    if len(noise) > len(clip) and rng.random() < p_noise:
        off = int(rng.integers(0, len(noise) - len(clip)))
        nz = noise[off:off + len(clip)]
        sp = float(np.sqrt(np.mean(clip ** 2)) + 1e-9)
        npow = float(np.sqrt(np.mean(nz ** 2)) + 1e-9)
        clip = clip + nz * (sp / npow) * 10 ** (-rng.uniform(*snr_db) / 20.0)
    clip = clip * 10 ** (rng.uniform(*gain_db) / 20.0)
    return np.clip(clip, -1.0, 1.0)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def fine_tag(nbank: int, split: str, aug: int, keywords) -> str:
    kw = "-".join(keywords)
    return (f"fine_{kw}_nb{nbank}_fl{FINE_FRAME_LOG2}_fw{FINE_FEAT_W}"
            f"_{split}_a{aug}")


def _collect(split: str, cfg_labels, rng, root: str, aug: int,
             n_bg_words: int, n_silence: int):
    """Assemble the float clips and labels for one split."""
    kws = list(cfg_labels)
    others = [w for w in list_words(root) if w not in kws]
    noise = load_noise(root)
    nclass = len(kws) + 1

    def files_for(word):
        d = os.path.join(root, word)
        return [os.path.join(d, f) for f in sorted(os.listdir(d))
                if f.endswith(".wav") and which_set(f) == split]

    clips, labels = [], []
    for ci, kw in enumerate(kws):
        fl = files_for(kw)
        for p in fl:
            a = read_wav(p)
            for _ in range(aug):
                clips.append(augment(place(a, rng), noise, rng))
                labels.append(ci)
        print(f"  {split:5s} {kw:>8s} : {len(fl) * aug:5d} clips")

    pool = [p for w in others for p in files_for(w)]
    rng.shuffle(pool)
    scale = 1.0 if split == "train" else 0.25
    take = pool[:int(n_bg_words * scale)]
    for p in take:
        clips.append(augment(place(read_wav(p), rng), noise, rng))
        labels.append(nclass - 1)
    print(f"  {split:5s} {'unknown':>8s} : {len(take):5d} clips "
          f"from {len(others)} other words")

    nsil = max(1, int(n_silence * scale))
    for _ in range(nsil):
        off = int(rng.integers(0, max(1, len(noise) - CLIP_N)))
        seg = noise[off:off + CLIP_N].astype(np.float32)
        if len(seg) < CLIP_N:
            seg = np.pad(seg, (0, CLIP_N - len(seg)))
        clips.append(np.clip(seg * 10 ** (rng.uniform(-6, 12) / 20.0), -1, 1))
        labels.append(nclass - 1)
    print(f"  {split:5s} {'silence':>8s} : {nsil:5d} clips")

    clips = np.stack(clips).astype(np.float32)
    labels = np.asarray(labels, dtype=np.int64)
    perm = rng.permutation(len(clips))
    return clips[perm], labels[perm]


def build_fine(split: str, keywords=("marvin", "sheila"), nbanks=DEFAULT_NBANKS,
               aug: int = 2, seed: int = 0, n_bg_words: int = 3500,
               n_silence: int = 1000, batch: int = 1024, root: str = GSC,
               force: bool = False):
    """Extract and cache fine features for every ladder in `nbanks`.

    One microphone simulation per batch feeds all ladders, which is the whole
    reason this function takes a list instead of a single config.
    """
    os.makedirs(CACHE, exist_ok=True)
    paths = {nb: os.path.join(CACHE, fine_tag(nb, split, aug, keywords) + ".npz")
             for nb in nbanks}
    todo = [nb for nb, p in paths.items() if force or not os.path.exists(p)]
    if not todo:
        return paths

    rng = np.random.default_rng(seed + abs(hash(split)) % 10007)
    clips, labels = _collect(split, keywords, rng, root, aug,
                             n_bg_words, n_silence)
    cfgs = {nb: fine_config(nb) for nb in todo}
    acc = {nb: [] for nb in todo}

    t0 = time.time()
    for s in range(0, len(clips), batch):
        chunk = clips[s:s + batch]
        done = min(s + batch, len(clips))
        head = f"  {split:5s} {done}/{len(clips)}"
        x = pdm_decimate(
            chunk, sr_in=SR,
            progress=lambda a, b_: print(f"{head} mic {100 * a // b_:3d}%",
                                         end="\r", flush=True))
        x = x.astype(np.int64)
        for nb in todo:
            print(f"{head} ladder nbank={nb}   ", end="\r", flush=True)
            acc[nb].append(front_end(x, cfgs[nb]).astype(np.uint8))
    print(" " * 78, end="\r")

    for nb in todo:
        feats = np.concatenate(acc[nb], axis=0)
        np.savez_compressed(paths[nb], feats=feats, labels=labels,
                            nbank=nb, keywords=np.array(list(keywords)))
        print(f"  {split:5s} nbank={nb}: {feats.shape} "
              f"-> {os.path.basename(paths[nb])}")
    print(f"  {split:5s} done in {time.time() - t0:.0f} s")
    return paths


def load_fine(split: str, nbank: int, keywords=("marvin", "sheila"),
              aug: int = 2, **kw):
    paths = build_fine(split, keywords=keywords, nbanks=(nbank,), aug=aug, **kw)
    z = np.load(paths[nbank])
    return z["feats"], z["labels"]


def load_for(cfg: KWSConfig, split: str, aug: int = 2, **kw):
    """Features shaped for `cfg`, derived from the fine cache."""
    fine, labels = load_fine(split, cfg.nbank, tuple(cfg.keywords), aug, **kw)
    return derive_features(fine, cfg).astype(np.int64), labels


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _sample_clips(word: str, n: int, rng, root: str = GSC):
    d = os.path.join(root, word)
    noise = load_noise(root)
    fl = sorted(os.listdir(d))[:n]
    return np.stack([augment(place(read_wav(os.path.join(d, f)), rng), noise, rng)
                     for f in fl]).astype(np.float32)


def verify_pdm(n: int = 24, seed: int = 7, nbank: int = 5):
    """Quantify what the cheap ideal-ADC model gets wrong about the mic.

    Reported so that the choice to always simulate the modulator is defensible
    rather than assumed: a systematic shift here would mistune every band.
    """
    rng = np.random.default_rng(seed)
    clips = _sample_clips("marvin", n, rng)
    cfg = fine_config(nbank)
    print(f"  exact sigma-delta ({PDM_HZ / 1e6:.3f} MHz, {n} clips) ...")
    fe = front_end(pdm_decimate(clips, sr_in=SR).astype(np.int64), cfg)
    print("  ideal ADC model ...")
    fa = front_end(quantize_pcm(upsample_linear(clips, FS // SR)), cfg)
    d = fe.astype(np.int16) - fa.astype(np.int16)
    step_db = 6.02 if cfg.feat_w == 4 else 3.01
    print(f"  exact agreement   : {float(np.mean(d == 0)) * 100:.1f} %")
    print(f"  mean bias         : {d.mean():+.3f} steps "
          f"({step_db * d.mean():+.2f} dB)")
    print(f"  within 1 step     : {float(np.mean(abs(d) <= 1)) * 100:.1f} %")
    print(f"  max |error|       : {int(abs(d).max())} steps")
    return d


def verify_derive(n: int = 16, seed: int = 3, nbank: int = 5):
    """Prove `derive_features` equals a native run of the full front end."""
    rng = np.random.default_rng(seed)
    x = pdm_decimate(_sample_clips("marvin", n, rng), sr_in=SR).astype(np.int64)
    fine = front_end(x, fine_config(nbank))
    ok = True
    for nslot in (2, 4):
        for fl2 in (FINE_FRAME_LOG2, FINE_FRAME_LOG2 + 1, FINE_FRAME_LOG2 + 2):
            for fw in (4, 5):
                cfg = KWSConfig(nbank=nbank, nslot=nslot, frame_log2=fl2,
                                feat_w=fw)
                native = front_end(x, cfg)
                got = derive_features(fine, cfg)[:, :native.shape[1]]
                same = np.array_equal(native, got)
                ok &= same
                print(f"  nslot={nslot} frame_log2={fl2} feat_w={fw}: "
                      f"{'MATCH' if same else 'MISMATCH'}  {native.shape}")
    print(f"  -> {'all derivations are bit-exact' if ok else 'DERIVATION BROKEN'}")
    return ok


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--verify-pdm", action="store_true")
    ap.add_argument("--verify-derive", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--nbanks", default="5,7")
    ap.add_argument("--aug", type=int, default=2)
    ap.add_argument("--keywords", default="marvin,sheila")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    kws = tuple(a.keywords.split(","))
    nbanks = tuple(int(x) for x in a.nbanks.split(","))

    if a.verify_pdm:
        verify_pdm()
    if a.verify_derive:
        verify_derive()
    if a.prepare:
        for split, aug in (("train", a.aug), ("val", 1), ("test", 1)):
            build_fine(split, keywords=kws, nbanks=nbanks, aug=aug,
                       force=a.force)
    if a.report:
        for f in sorted(os.listdir(CACHE)) if os.path.isdir(CACHE) else []:
            z = np.load(os.path.join(CACHE, f))
            print(f"{f:56s} {str(z['feats'].shape):22s} "
                  f"labels={np.bincount(z['labels'])}")
    if not any((a.prepare, a.verify_pdm, a.verify_derive, a.report)):
        ap.print_help()
