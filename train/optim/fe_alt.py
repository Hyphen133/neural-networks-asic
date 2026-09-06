"""Front ends the chip does *not* have, to bound what the one it has is costing.

Every experiment so far moved parameters inside the dyadic cascade: how many
bands, which octaves, how wide the log step, how long the window. The cascade
itself was fixed, and it forces one property none of those parameters can
touch -- **the bands are exactly one octave wide**, because stage b runs at
half the rate of stage b-1 and the band is the difference between neighbouring
stages. An infant cry has a 300-600 Hz fundamental with harmonics above it; an
octave-wide bin at 243-486 Hz cannot separate the fundamental from its
neighbours, and no choice of TAP0 or K_SHIFT changes that.

So this asks the questions the parameter sweeps could not:

    mel<N>   an N-band log-mel spectrogram. Not buildable on this tile at any
             N -- it needs an FFT or N biquads -- but it is the bound: if a
             40-band mel cannot beat the 6-band cascade by much, the limit is
             the task, not the filterbank, and no front-end work is worth
             doing.
    oct1     the dyadic cascade's own band, s[b-1] - s[b], recomputed here so
             the alternatives have a like-for-like control.
    oct2     s[b-1] - 2*s[b] + s[b+1]. A second-order difference of the *same*
             states: 12 dB/octave skirts instead of 6, so a sharper band for
             one extra subtract and a shift, and no new state at all.
    half     two cascades at K and K+1 interleaved -> bands every half octave.
             Doubles the cascade state, which is why it needs measuring before
             it is designed.

Features come out in the same ww_feats_<tag>.npz layout probe.py already
reads, so every alternative is scored by the same ladder and the same
validation-selected protocol as everything else.

    python train/optim/fe_alt.py --task babycry --mode mel40
    python train/optim/fe_alt.py --task babycry --mode oct2 --quant

``--quant`` applies the chip's readout -- per-frame maximum of a 4-bit log
magnitude -- so a filterbank can be compared with the cascade at matched
precision rather than being flattered by float.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "train"))
import wwhw  # noqa: E402

ART = os.path.join(ROOT, "artifacts")
CACHE = os.path.join(ART, "data")
SR = 16_000
NFRAME_EXT = 24
FRAME = SR // NFRAME_EXT          # 666 samples ~ 41.6 ms, the chip's frame


def mel_filters(n_mels: int, n_fft: int, fmin=60.0, fmax=7800.0) -> np.ndarray:
    """(n_mels, n_fft//2+1) triangular filterbank on a mel scale."""
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    edges = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    freqs = np.linspace(0, SR / 2, n_fft // 2 + 1)
    fb = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        left = (freqs - lo) / max(mid - lo, 1e-9)
        right = (hi - freqs) / max(hi - mid, 1e-9)
        fb[i] = np.clip(np.minimum(left, right), 0, None)
    return fb


def mel_features(x: np.ndarray, n_mels: int) -> np.ndarray:
    """(B, L) float -> (B, NFRAME_EXT, n_mels) log energy."""
    n_fft = 1024
    fb = mel_filters(n_mels, n_fft)
    out = np.zeros((x.shape[0], NFRAME_EXT, n_mels), dtype=np.float32)
    win = np.hanning(n_fft).astype(np.float32)
    for f in range(NFRAME_EXT):
        lo = f * FRAME + FRAME // 2 - n_fft // 2
        seg = np.zeros((x.shape[0], n_fft), dtype=np.float32)
        a, b = max(0, lo), min(x.shape[1], lo + n_fft)
        seg[:, a - lo:b - lo] = x[:, a:b]
        spec = np.abs(np.fft.rfft(seg * win, axis=1)) ** 2
        # Halved to log10(amplitude), the same unit dyadic_features returns, so
        # quantise()'s 0.15-decade step is 3 dB for both. Leaving this as
        # log10(power) gives mel a 1.5 dB step and therefore half the dynamic
        # range of the cascade it is being compared against.
        out[:, f, :] = 0.5 * np.log10(spec @ fb.T + 1e-12)
    return out


def cascade_states(x: np.ndarray, nstage: int, k: int, state_w: int = 0,
                   in_amp: int = 0) -> list[np.ndarray]:
    """The dyadic 1-pole cascade on PCM: stage b updates every 2^b samples.

    Each stage is held between its updates and returned at the input length, so
    differences between stages line up sample for sample.

    ``state_w`` switches from float to the RTL's integer arithmetic: the input
    is scaled to +-``in_amp`` and every state is a truncating right shift held
    in ``state_w`` signed bits, exactly as ``state[b] += (in - state[b]) >> K``.

    The RTL ties the two together as ``IN_AMP = 1 << (STATE_W - 3)``, so the
    state always has three bits of headroom above the input and *no fractional
    bits below it*. Widening STATE_W under that rule scales the signal with the
    LSB and buys no resolution at all -- which is why they are separate
    arguments here. Holding in_amp fixed while state_w grows is the change the
    RTL cannot currently express.
    """
    B, L = x.shape
    if state_w:
        lim = 1 << (state_w - 1)
        prev = np.clip(np.round(x * in_amp), -lim, lim - 1).astype(np.int64)
    else:
        prev = x
    states = []
    for b in range(nstage):
        s = np.zeros(prev.shape, dtype=prev.dtype)
        acc = np.zeros(B, dtype=prev.dtype)
        step = 1 << b
        for i in range(0, L, step):
            if state_w:
                # >> on a negative int64 floors, matching Verilog's >>> on a
                # signed value; the RTL truncates the same way.
                acc = np.clip(acc + ((prev[:, i] - acc) >> k), -lim, lim - 1)
            else:
                acc = acc + (prev[:, i] - acc) / (1 << k)
            s[:, i:i + step] = acc[:, None]
        states.append(s)
        prev = s
    return [st.astype(np.float32) for st in states]


def dyadic_features(x: np.ndarray, mode: str, nband: int, tap0: int,
                    k: int, state_w: int = 0, in_amp: int = 0) -> np.ndarray:
    """(B, L) -> (B, NFRAME_EXT, nbands) per-frame max of the log |band|."""
    if mode == "half":
        st_a = cascade_states(x, tap0 + nband + 1, k, state_w, in_amp)
        st_b = cascade_states(x, tap0 + nband + 1, k + 1, state_w, in_amp)
        bands = []
        for i in range(nband):
            b = tap0 + i
            bands.append(st_a[b - 1] - st_a[b])
            bands.append(st_b[b - 1] - st_b[b])
    else:
        st = cascade_states(x, tap0 + nband + 1, k, state_w, in_amp)
        bands = []
        for i in range(nband):
            b = tap0 + i
            if mode == "oct2":
                bands.append(st[b - 1] - 2.0 * st[b] + st[b + 1])
            else:
                bands.append(st[b - 1] - st[b])
    out = np.zeros((x.shape[0], NFRAME_EXT, len(bands)), dtype=np.float32)
    for j, bd in enumerate(bands):
        mag = np.abs(bd)
        for f in range(NFRAME_EXT):
            out[:, f, j] = mag[:, f * FRAME:(f + 1) * FRAME].max(1)
    return np.log10(out + 1e-10)


def quantise(f: np.ndarray, feat_w: int = 4) -> np.ndarray:
    """The chip's readout: a log magnitude on a ~3 dB grid, feat_w bits wide."""
    steps = (f - f.max()) / 0.15          # 0.15 decades ~ 3 dB
    q = np.clip(np.round(steps) + (1 << feat_w) - 1, 0, (1 << feat_w) - 1)
    return q.astype(np.uint8)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="babycry")
    ap.add_argument("--mode", required=True, help="mel<N> | oct1 | oct2 | half")
    ap.add_argument("--tag", default="")
    ap.add_argument("--nband", type=int, default=6)
    ap.add_argument("--tap0", type=int, default=3)
    ap.add_argument("--k-shift", dest="k", type=int, default=2)
    ap.add_argument("--quant", action="store_true",
                    help="apply the chip's log readout")
    ap.add_argument("--feat-w", dest="feat_w", type=int, default=4,
                    help="bits in the quantised log feature. The chip has 4, "
                         "giving 16 steps of 3 dB = 48 dB of range; a finer "
                         "filterbank resolves more spectral contrast and so "
                         "needs more range to represent it.")
    ap.add_argument("--state-w", dest="state_w", type=int, default=0,
                    help="0 = float cascade. Otherwise the RTL's integer state, "
                         "this many signed bits.")
    ap.add_argument("--in-amp", dest="in_amp", type=int, default=0,
                    help="input scale for the integer cascade. The RTL forces "
                         "1<<(STATE_W-3); passing a smaller value adds "
                         "fractional bits below the input, which is the thing "
                         "the RTL cannot currently express. 0 = the RTL rule.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=512)
    args = ap.parse_args()
    if args.state_w and not args.in_amp:
        args.in_amp = 1 << (args.state_w - 3)

    prefix = os.path.join(CACHE, f"cache_{args.task}")
    meta = np.load(prefix + "_meta.npz")
    clips = np.load(prefix + "_clips.npy", mmap_mode="r")
    n = len(meta["labels"]) if not args.limit else min(args.limit, len(meta["labels"]))

    chunks = []
    for lo in range(0, n, args.batch):
        hi = min(lo + args.batch, n)
        x = np.asarray(clips[lo:hi], dtype=np.float32) / 32768.0
        if args.mode.startswith("mel"):
            f = mel_features(x, int(args.mode[3:]))
        else:
            f = dyadic_features(x, args.mode, args.nband, args.tap0, args.k,
                                args.state_w, args.in_amp)
        chunks.append(quantise(f, args.feat_w) if args.quant
                      else f.astype(np.float32))
        if (lo // args.batch) % 5 == 0:
            print(f"  {hi}/{n}", flush=True)
    feats = np.concatenate(chunks, 0)

    tag = args.tag or (f"{args.task}_{args.mode}"
                       f"{f's{args.state_w}a{args.in_amp}' if args.state_w else ''}"
                       f"{f'q{args.feat_w}' if args.quant else ''}")
    cfg = wwhw.HWConfig(nband=feats.shape[2], tap0=args.tap0, nphase=2,
                        frame_log2=16, feat_w=4 if args.quant else 8)
    out = os.path.join(ART, f"ww_feats_{tag}.npz")
    np.savez_compressed(out, feats=feats, labels=meta["labels"][:n],
                        splits=meta["splits"][:n], index=meta["index"][:n],
                        gains=meta["gains"][:n], targets=np.array([args.task]),
                        cfg=json.dumps(cfg.to_dict()))
    print(f"wrote {os.path.relpath(out, ROOT)}  {feats.shape}  "
          f"range {feats.min():.2f}..{feats.max():.2f}")


if __name__ == "__main__":
    main()
