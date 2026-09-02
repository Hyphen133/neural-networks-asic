"""Bit-exact software twin of src/tt_um_kws.sv -- the host-free wake-word tile.

Pure Python + NumPy, no PyTorch. Imported by:
  * kws_data.py   -- to turn WAV files into integer feature frames
  * kws_train.py  -- so the QAT forward pass matches silicon
  * kws_export.py -- to sanity-check exported weights
  * test/test_kws.py -- as the cocotb golden reference

Signal chain (everything integer, no floats after the microphone):

    PDM mic  --1 bit @ 3.072 MHz-->  ones-count over 64 bits  --> x, 7-bit
    signed @ 48 kHz  -->  cascade of NBANK one-pole leaky integrators
    (shift-add only, no multiplier)  -->  NBAND = NBANK-1 band signals as
    differences of adjacent taps  -->  leaky peak envelope per band  -->
    log via leading-zero count (FEAT_W bits)  -->  max-pool into NSLOT
    time slots  -->  int4 linear classifier over NBAND*NSLOT features
    -->  argmax over NCLASS  -->  LED.

The two design facts that shape all of it:
  * a 1x1 IHP tile holds ~300 flip-flops, so every register width here was
    chosen against that budget (see docs/design-space-1x1.md);
  * there is no multiplier in the front end at all -- each integrator
    coefficient is a sum of two powers of two, applied as two sequential
    shift-add passes, which is why `alpha_effective` below is not simply
    2**-a + 2**-b.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field

import numpy as np

# ---------------------------------------------------------------------------
# Rates. clk is the TinyTapeout user clock; everything else divides down.
# ---------------------------------------------------------------------------
# 12.288 MHz is 256 x 48 kHz, the standard audio clock, and divides cleanly to
# a 3.072 MHz PDM bit clock -- at or below the maximum of every common MEMS PDM
# microphone (SPH0641 4.8 MHz, ICS-41350 3.6 MHz, MP34DT05 3.25 MHz).
#
# DECIM = 64 rather than 32 is worth a flip-flop: the ones-count then spans
# [-32, +32] instead of [-16, +16], one extra ADC bit. That matters because
# ordinary speech only drives the modulator to ~1.5 LSB rms, so the front end
# is resolution-starved at the bottom of its range, not the top.
CLK_HZ = 12_288_000         # nominal user clock
PDM_DIV = 4                 # clk / 4  -> PDM bit clock
PDM_HZ = CLK_HZ // PDM_DIV  # 3.072 MHz
DECIM = 64                  # ones-count window, PDM bits per PCM sample
FS = PDM_HZ // DECIM        # 48 kHz -- the rate the filterbank runs at

SKIP = 0xF                  # microcode shift value meaning "this pass is a no-op"


# ---------------------------------------------------------------------------
# Integrator coefficient fitting
# ---------------------------------------------------------------------------
def alpha_effective(a: int, b: int | None) -> float:
    """Effective one-pole coefficient of the two-pass sequential update.

    The RTL does, per band, up to two shift-add passes over the *same*
    register, recomputing the difference in between:

        d = prev - y ; y += d >> a
        d = prev - y ; y += d >> b

    which is a cascade of two one-poles sharing a target, i.e.

        alpha = 1 - (1 - 2**-a) * (1 - 2**-b)

    not 2**-a + 2**-b. Getting this wrong detunes every band, so the model
    and the RTL both go through this function.
    """
    alpha = 1.0 - 2.0 ** -a
    if b is not None and b != SKIP:
        alpha *= 1.0 - 2.0 ** -b
    return 1.0 - alpha


def corner_hz(a: int, b: int | None, fs: int = FS) -> float:
    """-3 dB corner of a one-pole with the given effective coefficient."""
    return alpha_effective(a, b) * fs / (2.0 * np.pi)


def fit_corner(target_hz: float, fs: int = FS, max_shift: int = 12):
    """Best (a, b) shift pair for a desired corner frequency.

    Searches single-shift and two-shift forms and returns the pair whose
    `corner_hz` is closest in log-frequency, which is the error measure that
    matters for a log-spaced filterbank.
    """
    best = None
    for a in range(1, max_shift + 1):
        cands = [(a, None)] + [(a, b) for b in range(a, max_shift + 1)]
        for a_, b_ in cands:
            fc = corner_hz(a_, b_, fs)
            if fc <= 0:
                continue
            err = abs(np.log(fc / target_hz))
            if best is None or err < best[0]:
                best = (err, a_, b_)
    _, a, b = best
    return a, b


def fit_bank(targets_hz, fs: int = FS):
    """Fit a whole ladder of corners; returns [(a, b), ...] descending in fc."""
    return [fit_corner(f, fs) for f in targets_hz]


# ---------------------------------------------------------------------------
# Filterbank ladders.
#
# Corners descend from just under Nyquist-of-speech down to the F0 region.
# Band i is the difference of taps i and i+1, so NBANK taps give NBANK-1
# bandpass channels; the top tap doubles as the anti-alias low-pass that
# keeps PDM shaped noise out of band 0.
#
# The 4-band ladder is roughly 1.1 octaves per channel -- the resolution of
# a 4-channel vocoder, which the cochlear-implant literature shows is enough
# for high word recognition in quiet. That is the accuracy story for the
# default build; wider ladders trade flip-flops for spectral detail.
# ---------------------------------------------------------------------------
BANK_TARGETS = {
    5: [3600, 1800, 800, 360, 160],                          # 4 bands
    6: [3700, 2000, 1000, 500, 250, 140],                    # 5 bands
    7: [3800, 2200, 1250, 700, 400, 230, 130],               # 6 bands
    8: [3900, 2350, 1450, 880, 530, 320, 195, 120],          # 7 bands
}

BANK_SHIFTS = {n: fit_bank(t) for n, t in BANK_TARGETS.items()}


def bank_table(nbank: int):
    """Microcode for the filterbank sweep: [(shift_a, shift_b), ...]."""
    if nbank not in BANK_SHIFTS:
        raise ValueError(f"no coefficient ladder for NBANK={nbank}")
    return [(a, SKIP if b is None else b) for a, b in BANK_SHIFTS[nbank]]


def bank_report(nbank: int, fs: int = FS) -> str:
    rows = ["  tap  shifts      fc(Hz)   band            passband(Hz)"]
    tbl = bank_table(nbank)
    for i, (a, b) in enumerate(tbl):
        fc = corner_hz(a, SKIP if b == SKIP else b, fs)
        bstr = f"{a}" + ("" if b == SKIP else f"+{b}")
        if i == 0:
            band = "(anti-alias)"
            span = f"< {fc:7.0f}"
        else:
            fhi = corner_hz(*(tbl[i - 1][0], tbl[i - 1][1]), fs=fs)
            band = f"band{i-1}"
            span = f"{fc:7.0f} .. {fhi:7.0f}"
        rows.append(f"  {i:3d}  {bstr:<10s} {fc:8.0f}   {band:<14s} {span}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Configuration -- one object shared by the model, the trainer and the export
# ---------------------------------------------------------------------------
@dataclass
class KWSConfig:
    # --- front end ---
    nbank: int = 5              # integrator taps; bands = nbank - 1
    state_w: int = 14           # integrator register width (signed)
    in_shift: int = 7           # x << in_shift before entering the cascade
    #   in_shift 7 with state_w 14 is the largest gain that cannot clip:
    #   x is 7-bit signed so u = x<<7 spans +-4096 inside a +-8191 register,
    #   and a one-pole has unity DC gain and no overshoot, so |y| <= |u|.
    env_w: int = 12             # envelope register width (unsigned)
    env_shift: int = 2          # |band| >> env_shift before the envelope
    decay: int = 9              # envelope decay: env -= max(1, env >> decay)
    feat_w: int = 4             # 4 = exponent only, 5 = exponent + 1 mantissa bit
    # --- temporal ---
    nslot: int = 4              # pooled time slots held on chip
    frame_log2: int = 12        # frame = 2**frame_log2 samples at FS
    # --- classifier ---
    nclass: int = 3             # keyword0, keyword1, background
    score_w: int = 16           # classifier accumulator width (signed)
    trim_shift: int = 1         # ui trim nibble << trim_shift added to bg bias
    hold_frames: int = 6        # how long a detection latches the LED
    # --- labels, carried through so exports are self-describing ---
    keywords: list = field(default_factory=lambda: ["marvin", "sheila"])

    # ---- derived ----
    @property
    def nband(self) -> int:
        return self.nbank - 1

    @property
    def nfeat(self) -> int:
        return self.nband * self.nslot

    @property
    def frame_len(self) -> int:
        return 1 << self.frame_log2

    @property
    def frame_hz(self) -> float:
        return FS / self.frame_len

    @property
    def context_ms(self) -> float:
        return 1000.0 * self.nslot * self.frame_len / FS

    @property
    def feat_max(self) -> int:
        return (1 << self.feat_w) - 1

    @property
    def env_max(self) -> int:
        return (1 << self.env_w) - 1

    def flop_estimate(self) -> dict:
        """Register budget, itemised. Compare against ~300 for a 1x1 tile."""
        items = {
            "pdm divider + ones counter + x reg": 2 + 7 + 7,
            "filterbank taps": self.nbank * self.state_w,
            "band envelopes": self.nband * self.env_w,
            "feature slots": self.nslot * self.nband * self.feat_w,
            "frame counter": self.frame_log2,
            "classifier acc + best score/class": self.score_w + self.score_w + 2,
            "sequencer indices": 3 + 2 + 2 + 3,
            "outputs, hold counter, sticky flags": 2 + 4 + 2 + self.feat_w,
        }
        items["TOTAL"] = sum(items.values())
        return items

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        known = {f for f in KWSConfig.__dataclass_fields__}
        return KWSConfig(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Small integer helpers. Every one of these has a direct gate-level twin.
# ---------------------------------------------------------------------------
def sat(v, width: int):
    """Saturate to a signed `width`-bit range (the RTL clamps, never wraps)."""
    lo, hi = -(1 << (width - 1)), (1 << (width - 1)) - 1
    return np.clip(v, lo, hi)


def asr(v, sh: int):
    """Arithmetic shift right that rounds toward -inf, like Verilog >>>."""
    return v >> sh                      # numpy ints are two's complement


def bit_length(v):
    """Position of the most significant set bit, 0 for v == 0.

    This is the leading-zero-count "logarithm" the tile uses; np.frexp gives
    exactly `bit_length` for non-negative integers below 2**53.
    """
    v = np.asarray(v)
    e = np.frexp(v.astype(np.float64))[1].astype(np.int64)
    return np.where(v > 0, e, 0)


def log_feature(env, cfg: KWSConfig):
    """Envelope -> FEAT_W-bit log magnitude.

    feat_w == 4: the exponent alone, i.e. 6 dB per step, free in gates.
    feat_w == 5: exponent plus the bit below the MSB, i.e. 3 dB per step,
                 one extra mux and NBAND*NSLOT more flip-flops.
    """
    e = bit_length(env)
    if cfg.feat_w == 4:
        return np.minimum(e, cfg.feat_max)
    if cfg.feat_w == 5:
        sh = np.maximum(e - 2, 0)
        mant = np.where(e >= 2, (env >> sh) & 1, 0)
        return np.minimum(2 * e + mant, cfg.feat_max)
    raise ValueError("feat_w must be 4 or 5")


# ---------------------------------------------------------------------------
# PDM front end
# ---------------------------------------------------------------------------
def sigma_delta_pdm(audio, osr: int = 1):
    """Second-order sigma-delta modulator: float audio in [-1,1] -> 0/1 stream.

    `audio` is (B, N) already at the PDM rate. Returns (B, N) uint8. Kept for
    inspecting short signals; feature extraction uses `pdm_decimate` instead,
    which fuses this with interpolation and the ones-count so that a whole
    training batch fits in memory.
    """
    audio = np.asarray(audio, dtype=np.float64)
    b, n = audio.shape
    i1, i2 = np.zeros(b), np.zeros(b)
    out = np.empty((b, n), dtype=np.uint8)
    for t in range(n):
        y = np.where(i2 >= 0.0, 1.0, -1.0)
        i1 += audio[:, t] - y
        i2 += i1 - y
        np.clip(i1, -4.0, 4.0, out=i1)      # integrator limiting, as in silicon
        np.clip(i2, -8.0, 8.0, out=i2)
        out[:, t] = (y > 0).astype(np.uint8)
    return out


def decimate_ones(pdm_bits, decim: int = DECIM):
    """Exact model of the on-chip stage: count ones per window, centre it.

    pdm_bits (B, N) of 0/1 -> (B, N//decim) signed in [-decim/2, +decim/2].
    """
    bits = np.asarray(pdm_bits, dtype=np.int64)
    b, n = bits.shape
    n -= n % decim
    return bits[:, :n].reshape(b, n // decim, decim).sum(axis=2) - decim // 2


def pdm_decimate(audio, sr_in: int = 16000, decim: int = DECIM,
                 pdm_hz: int = PDM_HZ, chunk: int = 256, progress=None):
    """Microphone model: (B, N) audio at sr_in -> (B, N*fs/sr_in) x at FS.

    Interpolation to the PDM rate, second-order sigma-delta modulation and the
    on-chip ones-counting decimator, fused into one pass so that only the
    64 kHz result is ever materialised. Without the fusion a batch of 1024
    one-and-a-half-second clips would need 12 GB just to hold the bit stream.

    This is the path the trainer uses. The cheaper `quantize_pcm` below sits
    3.7 dB low with errors up to 3 log steps -- see `kws_data.verify_pdm` --
    which is enough to mistune every band, so it is not used for training.
    """
    audio = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    if audio.ndim == 1:
        audio = audio[None, :]
    b, n_in = audio.shape
    up = pdm_hz // sr_in                        # 128
    n_pdm = n_in * up
    n_out = n_pdm // decim
    out = np.empty((b, n_out), dtype=np.int16)

    i1 = np.zeros(b, dtype=np.float32)
    i2 = np.zeros(b, dtype=np.float32)
    cnt = np.zeros(b, dtype=np.int16)
    half = np.int16(decim // 2)
    oi = 0
    phase = 0

    # Walk the input in chunks of source samples, interpolating each chunk to
    # the PDM rate up front so the inner loop is pure recursion.
    for s0 in range(0, n_in, chunk):
        s1 = min(s0 + chunk, n_in)
        seg = audio[:, s0:s1]
        nxt = audio[:, s0 + 1:s1 + 1]
        if nxt.shape[1] < seg.shape[1]:         # hold the last sample
            nxt = np.concatenate([nxt, seg[:, -1:]], axis=1)
        # (B, chunk, up) linear ramp between consecutive source samples
        frac = (np.arange(up, dtype=np.float32) / up)[None, None, :]
        fine = (seg[:, :, None] + (nxt - seg)[:, :, None] * frac)
        fine = fine.reshape(b, -1)

        for t in range(fine.shape[1]):
            y = np.where(i2 >= 0.0, np.float32(1.0), np.float32(-1.0))
            i1 += fine[:, t] - y
            i2 += i1 - y
            np.clip(i1, -4.0, 4.0, out=i1)
            np.clip(i2, -8.0, 8.0, out=i2)
            cnt += (y > 0)
            phase += 1
            if phase == decim:
                out[:, oi] = cnt - half
                cnt[:] = 0
                phase = 0
                oi += 1
        if progress:
            progress(s1, n_in)
    return out[:, :oi]


def quantize_pcm(audio_f, decim: int = DECIM):
    """Ideal log2(decim)+1-bit ADC -- a fast but *inexact* stand-in for the mic.

    Same full-scale range as the real chain ([-16, +16] for decim=32) but with
    flat instead of shaped quantisation noise. Retained only so that
    `kws_data.verify_pdm` can quantify the difference; do not train on it.
    """
    a = np.clip(np.asarray(audio_f, dtype=np.float64), -1.0, 1.0)
    half = decim // 2
    return np.clip(np.rint(a * half), -half, half).astype(np.int64)


def upsample_linear(audio, factor: int):
    """Band-limited-enough 4x interpolation from 16 kHz to FS."""
    audio = np.asarray(audio, dtype=np.float64)
    n = audio.shape[-1]
    src = np.arange(n)
    dst = np.arange(n * factor) / factor
    if audio.ndim == 1:
        return np.interp(dst, src, audio)
    return np.stack([np.interp(dst, src, row) for row in audio])


# ---------------------------------------------------------------------------
# The front end proper -- bit-exact, batched over the first axis
# ---------------------------------------------------------------------------
def front_end(x_pcm, cfg: KWSConfig, want_bands: bool = False):
    """Integer feature extraction, identical to the RTL cycle for cycle.

    x_pcm : (B, N) int, the centred ones-count stream at FS.
    Returns feats (B, NFRAME, NSLOT, NBAND) uint -- the slot register contents
    as sampled at every frame boundary, newest slot first.

    Time ordering matters and is fixed here: at a frame boundary the
    classifier sees the *just-completed* frame in slot 0, and only then do the
    slots shift. The RTL follows the same order.
    """
    x = np.asarray(x_pcm, dtype=np.int64)
    b, n = x.shape
    nb, nband = cfg.nbank, cfg.nband
    tbl = bank_table(nb)

    y = np.zeros((nb, b), dtype=np.int64)
    env = np.zeros((nband, b), dtype=np.int64)
    slots = np.zeros((cfg.nslot, nband, b), dtype=np.int64)

    nframe = n // cfg.frame_len
    out = np.zeros((b, nframe, cfg.nslot, nband), dtype=np.int64)

    state_lo = -(1 << (cfg.state_w - 1))
    state_hi = (1 << (cfg.state_w - 1)) - 1
    frame_mask = cfg.frame_len - 1
    fi = 0

    for t in range(n):
        u = sat(x[:, t] << cfg.in_shift, cfg.state_w)

        # --- filterbank: ascending sweep, up to two shift-add passes per tap
        for i in range(nb):
            prev = u if i == 0 else y[i - 1]
            for sh in tbl[i]:
                if sh == SKIP:
                    continue
                d = prev - y[i]
                y[i] = np.clip(y[i] + asr(d, sh), state_lo, state_hi)

        # --- envelopes and log features
        for i in range(nband):
            band = y[i] - y[i + 1]
            a = np.minimum(np.abs(band) >> cfg.env_shift, cfg.env_max)
            dec = np.maximum(env[i] >> cfg.decay, (env[i] > 0).astype(np.int64))
            env[i] = np.maximum(a, env[i] - dec)
            f = log_feature(env[i], cfg)
            slots[0, i] = np.maximum(slots[0, i], f)

        # --- frame boundary: sample, then shift and clear
        if (t & frame_mask) == frame_mask:
            if fi < nframe:
                out[:, fi] = slots.transpose(2, 0, 1)
                fi += 1
            slots[1:] = slots[:-1]
            slots[0] = 0

    if want_bands:
        return out, y, env
    return out


def front_end_from_audio(audio16k, cfg: KWSConfig, exact_pdm: bool = True):
    """WAV samples (B, N) float in [-1,1] -> integer feature frames."""
    a = np.asarray(audio16k)
    if a.ndim == 1:
        a = a[None, :]
    x = pdm_decimate(a) if exact_pdm else quantize_pcm(upsample_linear(a, FS // 16000))
    return front_end(x, cfg)


# ---------------------------------------------------------------------------
# Classifier -- int4 weights, uint FEAT_W features, saturating accumulator
# ---------------------------------------------------------------------------
W_MIN, W_MAX = -8, 7


def classify_int(feats, w, bias, cfg: KWSConfig, trim: int = 0):
    """One frame -> (best_class, scores).

    feats : (..., NSLOT, NBAND) uint    w : (NCLASS, NSLOT, NBAND) int4
    bias  : (NCLASS,) int
    trim  : 0..15 from ui[4:1]; raises the background bias, so a bigger trim
            means a less trigger-happy chip.
    """
    f = np.asarray(feats, dtype=np.int64).reshape(*np.shape(feats)[:-2], -1)
    wf = np.asarray(w, dtype=np.int64).reshape(cfg.nclass, -1)
    bias = np.asarray(bias, dtype=np.int64).copy()
    bias[-1] = bias[-1] + (int(trim) << cfg.trim_shift)

    scores = sat(f @ wf.T + bias, cfg.score_w)
    return scores.argmax(axis=-1), scores


def detect_sequence(feats_seq, w, bias, cfg: KWSConfig, trim: int = 0):
    """Run the per-frame classifier over a whole clip and latch detections.

    Mirrors the RTL output logic: a non-background argmax lights that
    keyword's pin and holds it for `hold_frames` frames.
    """
    cls, scores = classify_int(feats_seq, w, bias, cfg, trim)
    cls = np.atleast_1d(cls)
    det = np.zeros((cls.shape[0], cfg.nclass - 1), dtype=bool)
    hold = np.zeros(cfg.nclass - 1, dtype=np.int64)
    for t in range(cls.shape[0]):
        hold = np.maximum(hold - 1, 0)
        c = int(cls[t])
        if c < cfg.nclass - 1:
            hold[c] = cfg.hold_frames
        det[t] = hold > 0
    return det, cls, scores


def clip_decision_batch(feats, w, bias, cfg: KWSConfig, trim: int = 0):
    """Whole-clip labels for a batch: (N, T, NSLOT, NBAND) -> (N,).

    Vectorised twin of `clip_decision`; identical results, but it evaluates a
    1500-clip validation split in one pass instead of 1500, which is the
    difference between a usable and an unusable training loop.
    """
    cls, _ = classify_int(feats, w, bias, cfg, trim)          # (N, T)
    cls = np.atleast_2d(cls)
    counts = np.stack([(cls == c).sum(axis=1) for c in range(cfg.nclass)], axis=1)
    kw = counts[:, : cfg.nclass - 1]
    return np.where(kw.max(axis=1) > 0, kw.argmax(axis=1), cfg.nclass - 1)


def clip_decision(feats_seq, w, bias, cfg: KWSConfig, trim: int = 0):
    """Whole-clip label: the keyword with the most winning frames, else bg.

    This is the number a demo actually cares about -- "did the LED light for
    the right word while I was talking" -- as opposed to per-frame accuracy.
    """
    cls, _ = classify_int(feats_seq, w, bias, cfg, trim)
    cls = np.atleast_1d(cls)
    counts = np.bincount(cls, minlength=cfg.nclass)
    kw = counts[: cfg.nclass - 1]
    return int(kw.argmax()) if kw.max() > 0 else cfg.nclass - 1


# ---------------------------------------------------------------------------
# Artifact I/O
# ---------------------------------------------------------------------------
ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")


def save_weights(tag: str, w, bias, cfg: KWSConfig, extra: dict | None = None):
    os.makedirs(ART_DIR, exist_ok=True)
    blob = {
        "w": np.asarray(w, dtype=np.int64).tolist(),
        "bias": np.asarray(bias, dtype=np.int64).tolist(),
        "cfg": cfg.to_dict(),
    }
    if extra:
        blob.update(extra)
    path = os.path.join(ART_DIR, f"kws_{tag}.json")
    with open(path, "w") as f:
        json.dump(blob, f, indent=1)
    return path


def load_weights(tag: str = "default"):
    with open(os.path.join(ART_DIR, f"kws_{tag}.json")) as f:
        blob = json.load(f)
    cfg = KWSConfig.from_dict(blob["cfg"])
    w = np.asarray(blob["w"], dtype=np.int64)
    bias = np.asarray(blob["bias"], dtype=np.int64)
    return w, bias, cfg, blob


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    for nb in sorted(BANK_TARGETS):
        cfg = KWSConfig(nbank=nb)
        print(f"\n=== NBANK={nb}  ({cfg.nband} bands) ===")
        print(bank_report(nb))
        est = cfg.flop_estimate()
        print(f"  flops: " + ", ".join(f"{k}={v}" for k, v in est.items()))
        print(f"  frame {1000/cfg.frame_hz:.0f} ms, context {cfg.context_ms:.0f} ms, "
              f"{cfg.nfeat} features, {cfg.nclass*cfg.nfeat} int4 weights")


# ---------------------------------------------------------------------------
# Deriving coarser configs from one fine extraction
#
# The sigma-delta simulation dominates feature-extraction cost, so it is run
# once per filterbank ladder at the finest useful frame rate with feat_w = 5,
# and every coarser (nslot, frame_log2, feat_w) combination is derived from
# that. The derivation is *exact*, not an approximation:
#   * max-pooling is associative, so max over 2**k fine frames equals max over
#     the 2**(11+k) samples a coarse frame spans;
#   * feat_w 4 is the exponent alone and feat_w 5 is the exponent with one
#     mantissa bit appended, so f4 == f5 >> 1 below the clamp.
# `test/test_kws.py` asserts both against a native run.
# ---------------------------------------------------------------------------
FINE_FRAME_LOG2 = 11
FINE_FEAT_W = 5


def fine_config(nbank: int, **kw) -> KWSConfig:
    """The extraction config: one slot, finest frames, full feature width."""
    return KWSConfig(nbank=nbank, nslot=1, frame_log2=FINE_FRAME_LOG2,
                     feat_w=FINE_FEAT_W, **kw)


def derive_features(fine, cfg: KWSConfig):
    """(B, Tf, 1, NBAND) fine features -> (B, Tc, NSLOT, NBAND) for cfg.

    Tf frames of 2**FINE_FRAME_LOG2 samples in, cfg.nslot-deep slot registers
    sampled at cfg.frame_len out. Slot 0 is the newest frame, as on chip.
    """
    f = np.asarray(fine, dtype=np.int64)
    if f.ndim == 4:
        f = f[:, :, 0, :]
    group = cfg.frame_len >> FINE_FRAME_LOG2
    if group < 1:
        raise ValueError(f"frame_log2={cfg.frame_log2} finer than the extraction")
    b, tf, nband = f.shape
    assert nband == cfg.nband, f"extraction has {nband} bands, cfg wants {cfg.nband}"

    tc = tf // group
    coarse = f[:, :tc * group].reshape(b, tc, group, nband).max(axis=2)

    if cfg.feat_w == 4:
        coarse = coarse >> 1
    elif cfg.feat_w != 5:
        raise ValueError("feat_w must be 4 or 5")
    coarse = np.minimum(coarse, cfg.feat_max)

    out = np.zeros((b, tc, cfg.nslot, nband), dtype=np.int64)
    for s in range(cfg.nslot):
        out[:, s:, s, :] = coarse[:, :tc - s if s else tc, :]
    return out
