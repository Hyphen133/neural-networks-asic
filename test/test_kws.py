"""cocotb testbench for tt_um_kws -- the host-free wake-word tile.

    cd test
    make -f Makefile.kws                          # RTL
    FRAME_LOG2=6 make -f Makefile.kws             # faster frames, more of them
    GATES=yes make -f Makefile.kws                # gate level

What is actually being proven: the RTL's per-frame decision sequence equals
train/kws_hw's, driven by the *same PDM bit stream* that a microphone would
produce. That single check exercises the whole chain -- ones-counting
decimator, shift-add filterbank, envelopes, leading-one logarithm, slot
pooling, int4 MACs, argmax and the hold logic -- because a discrepancy
anywhere changes the sequence.

Simulation time is the binding constraint: a 1.5 s clip is 18 M clock edges,
which Icarus will not do quickly. So the tests use short audio with a small
FRAME_LOG2 override, which raises the number of classifier invocations per
simulated second rather than lowering it. The golden model is parameterised
identically, so bit-exactness is still what gets checked.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "train"))
import kws_hw as H                                                # noqa: E402

CLK_NS = 81  # 12.288 MHz is 81.38 ns; the design is fully synchronous so the
             # exact period only matters for waveform readability.


# ---------------------------------------------------------------------------
# Configuration mirrored from the Makefile's parameter overrides
# ---------------------------------------------------------------------------
def cfg_from_env() -> H.KWSConfig:
    def iv(name, default):
        v = os.environ.get(name, "")
        return int(v) if v else default

    return H.KWSConfig(
        nbank=iv("NBANK", 5),
        nslot=iv("NSLOT", 4),
        frame_log2=iv("FRAME_LOG2", 6),
        feat_w=iv("FEAT_W", 4),
        state_w=iv("STATE_W", 14),
        env_w=iv("ENV_W", 12),
        env_shift=iv("ENV_SHIFT", 2),
        in_shift=iv("IN_SHIFT", 7),
        decay=iv("DECAY", 9),
        score_w=iv("SCORE_W", 16),
        hold_frames=iv("HOLD_FRAMES", 6),
    )


def weights_from_header(cfg: H.KWSConfig):
    """Read back the constants the RTL was compiled with.

    Parsing the generated header rather than the training artifact means the
    test compares against exactly what is in the gates, even if someone
    regenerates one without the other.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "src", "kws_weights.svh")
    defs = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("`define KWS_"):
                parts = line.split(None, 2)
                if len(parts) == 3:
                    defs[parts[1]] = parts[2].strip()

    def unpack(lit, count, bits, signed):
        _, _, hexpart = lit.partition("'h")
        acc = int(hexpart, 16)
        mask = (1 << bits) - 1
        out = []
        for i in range(count):
            v = (acc >> (i * bits)) & mask
            if signed and v & (1 << (bits - 1)):
                v -= 1 << bits
            out.append(v)
        return np.asarray(out, dtype=np.int64)

    nfeat = cfg.nfeat
    w = unpack(defs["KWS_W_FLAT"], cfg.nclass * nfeat, 4, True)
    b = unpack(defs["KWS_B_FLAT"], cfg.nclass, cfg.score_w, True)
    return w.reshape(cfg.nclass, cfg.nslot, cfg.nband), b, defs


# ---------------------------------------------------------------------------
# Stimulus
# ---------------------------------------------------------------------------
def load_audio(seconds: float, seed: int = 4):
    """Real speech when the dataset is present, a synthetic stand-in otherwise.

    The synthetic fallback is deliberately broadband and amplitude-modulated so
    that every band and the envelope decay all get exercised.
    """
    n = int(H.FS * seconds * (16000 / H.FS))       # samples at 16 kHz
    n = int(16000 * seconds)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "train"))
        import kws_data as D
        if os.path.isdir(D.GSC):
            d = os.path.join(D.GSC, "marvin")
            f = sorted(os.listdir(d))[0]
            a = D.read_wav(os.path.join(d, f))
            # take the loudest window so a short excerpt contains the word
            if len(a) > n:
                e = np.convolve(a.astype(np.float64) ** 2, np.ones(n), "valid")
                s = int(e.argmax())
                a = a[s:s + n]
            else:
                a = np.pad(a, (0, max(0, n - len(a))))
            return a[:n].astype(np.float32), "speech"
    except Exception:
        pass

    rng = np.random.default_rng(seed)
    t = np.arange(n) / 16000.0
    sig = sum(np.sin(2 * np.pi * f * t) / (i + 1)
              for i, f in enumerate((220.0, 700.0, 1500.0, 3000.0)))
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 6.0 * t)
    sig = sig * env + 0.02 * rng.standard_normal(n)
    return (0.4 * sig / np.abs(sig).max()).astype(np.float32), "synthetic"


def make_pdm(audio16k):
    """16 kHz audio -> PDM bits at H.PDM_HZ, via the same modulator as training."""
    up = H.upsample_linear(audio16k[None, :], H.PDM_HZ // 16000)
    return H.sigma_delta_pdm(up)[0].astype(np.uint8)


async def reset(dut):
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await Timer(1, "ns")


# ---------------------------------------------------------------------------
async def drive_pdm(dut, bits, pdm_div: int = 4, trim: int = 0,
                    dbg_band: int = 0, on_frame=None):
    """Feed PDM bits, one per pdm_div clocks, collecting per-frame outputs.

    The RTL samples the mic when its internal divider is zero, which is the
    first active clock after reset and every pdm_div-th clock after that, so
    holding bit k across clocks [k*pdm_div, (k+1)*pdm_div) lines up exactly.
    """
    frames = []
    ui_static = (dbg_band << 5) | ((trim & 0xF) << 1)
    for k, bit in enumerate(bits):
        dut.ui_in.value = ui_static | int(bit)
        for _ in range(pdm_div):
            await RisingEdge(dut.clk)
            if dut.uio_out.value.is_resolvable and (int(dut.uio_out.value) & 0x02):
                best = (int(dut.uio_out.value) >> 2) & 0x3
                det = int(dut.uo_out.value) & 0x3
                frames.append((best, det, int(dut.uo_out.value) >> 4))
                if on_frame:
                    on_frame(frames[-1])
    return frames


# ---------------------------------------------------------------------------
@cocotb.test()
async def test_reset_and_clocks(dut):
    """uio directions, PDM clock division and the 48 kHz sample strobe."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    await reset(dut)

    assert int(dut.uio_oe.value) == 0xFF, "every uio pin is an output"

    # The golden model is configured from the same environment variables the
    # Makefile turns into -P overrides. If elaboration ignored one of them,
    # every later comparison would be against the wrong model, so check the
    # elaborated values directly rather than trusting the build.
    cfg = cfg_from_env()
    for name, want in (("NBANK", cfg.nbank), ("NSLOT", cfg.nslot),
                       ("FRAME_LOG2", cfg.frame_log2), ("FEAT_W", cfg.feat_w),
                       ("NCLASS", cfg.nclass), ("ENV_W", cfg.env_w),
                       ("STATE_W", cfg.state_w), ("DECAY", cfg.decay),
                       ("IN_SHIFT", cfg.in_shift), ("ENV_SHIFT", cfg.env_shift)):
        got = int(getattr(dut.user_project, name).value)
        assert got == want, (f"RTL elaborated {name}={got} but the golden model "
                             f"was built with {want}")
    dut._log.info(f"parameters agree: {cfg.nband} bands x {cfg.nslot} slots, "
                  f"frame = {cfg.frame_len} samples")

    # pdm_clk on uio[0] must be clk/PDM_DIV with a 50 % duty cycle
    seen = []
    for _ in range(64):
        await RisingEdge(dut.clk)
        seen.append(int(dut.uio_out.value) & 1)
    edges = [i for i in range(1, len(seen)) if seen[i] != seen[i - 1]]
    periods = {edges[i + 1] - edges[i] for i in range(len(edges) - 1)}
    assert periods == {2}, f"pdm_clk half-period should be 2 clocks, got {periods}"

    # one sample strobe every PDM_DIV*DECIM = 256 clocks
    hits = 0
    for _ in range(256 * 3):
        await RisingEdge(dut.clk)
        if (int(dut.uio_out.value) >> 7) & 1:
            hits += 1
    assert hits == 3, f"expected 3 sample strobes in 768 clocks, saw {hits}"
    dut._log.info("clocking OK: pdm_clk = clk/4, sample rate = clk/256")


@cocotb.test()
async def test_silence_stays_background(dut):
    """An all-silence PDM stream must never light a keyword pin."""
    cfg = cfg_from_env()
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    await reset(dut)

    # A modulator fed silence emits an alternating pattern averaging half.
    bits = np.tile([1, 0], 3000).astype(np.uint8)
    frames = await drive_pdm(dut, bits)
    assert frames, "no frames completed -- FRAME_LOG2 too large for this stimulus"
    lit = [f for f in frames if f[1] != 0]
    assert not lit, f"{len(lit)} of {len(frames)} frames triggered on silence"
    dut._log.info(f"{len(frames)} frames of silence, no false triggers")


@cocotb.test()
async def test_matches_golden_model(dut):
    """Per-frame argmax and detection pins equal train/kws_hw, bit for bit."""
    cfg = cfg_from_env()
    w, bias, defs = weights_from_header(cfg)

    for key, val in (("KWS_NBANK", cfg.nbank), ("KWS_NSLOT", cfg.nslot),
                     ("KWS_NCLASS", cfg.nclass), ("KWS_FEAT_W", cfg.feat_w)):
        assert int(defs[key]) == val, (
            f"{key} in kws_weights.svh is {defs[key]} but the test config says "
            f"{val}; regenerate with train/kws_export.py")

    audio, kind = load_audio(0.09)
    bits = make_pdm(audio)
    dut._log.info(f"{kind} stimulus: {len(audio)} samples at 16 kHz "
                  f"-> {len(bits)} PDM bits, frame = {cfg.frame_len} samples")

    # --- golden: same bits, same decimator, same everything
    x = H.decimate_ones(bits[None, :])
    feats = H.front_end(x, cfg)[0]
    exp_cls, _ = H.classify_int(feats, w, bias, cfg)
    exp_det, _, _ = H.detect_sequence(feats, w, bias, cfg)

    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    await reset(dut)
    got = await drive_pdm(dut, bits)

    n = min(len(got), len(exp_cls))
    assert n >= 8, f"only {n} frames compared; lower FRAME_LOG2 or lengthen audio"

    bad = [(i, got[i][0], int(exp_cls[i])) for i in range(n)
           if got[i][0] != int(exp_cls[i])]
    assert not bad, (f"{len(bad)}/{n} frames disagree on argmax, first 5: "
                     f"{bad[:5]} (frame, rtl, model)")

    bad_det = [(i, got[i][1], int(exp_det[i][0] | (exp_det[i][1] << 1)))
               for i in range(n)
               if got[i][1] != int(exp_det[i][0]) | (int(exp_det[i][1]) << 1)]
    assert not bad_det, (f"{len(bad_det)}/{n} frames disagree on the detection "
                         f"pins, first 5: {bad_det[:5]}")

    dut._log.info(f"{n} frames match the golden model exactly "
                  f"(classes seen: {sorted(set(c for c, _, _ in got[:n]))})")


@cocotb.test()
async def test_trim_reduces_sensitivity(dut):
    """The ui trim nibble must only ever make the chip less trigger-happy."""
    cfg = cfg_from_env()
    w, bias, _ = weights_from_header(cfg)
    audio, _ = load_audio(0.09)
    bits = make_pdm(audio)

    counts = {}
    for trim in (0, 15):
        cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
        await reset(dut)
        frames = await drive_pdm(dut, bits, trim=trim)
        counts[trim] = sum(1 for f in frames if f[0] != cfg.nclass - 1)

        x = H.decimate_ones(bits[None, :])
        feats = H.front_end(x, cfg)[0]
        exp, _ = H.classify_int(feats, w, bias, cfg, trim=trim)
        n = min(len(frames), len(exp))
        bad = [i for i in range(n) if frames[i][0] != int(exp[i])]
        assert not bad, f"trim={trim}: {len(bad)}/{n} frames disagree"

    assert counts[15] <= counts[0], (
        f"trim raised keyword frames from {counts[0]} to {counts[15]}; the trim "
        f"nibble is supposed to add to the background bias only")
    dut._log.info(f"keyword frames: trim=0 -> {counts[0]}, trim=15 -> {counts[15]}")


@cocotb.test()
async def test_no_saturation_flags(dut):
    """env and score saturation are sticky; neither should fire on real audio."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    await reset(dut)
    audio, _ = load_audio(0.09)
    await drive_pdm(dut, make_pdm(audio))
    flags = int(dut.uio_out.value)
    assert not (flags >> 5) & 1, "envelope register saturated (uio[5] sticky)"
    assert not (flags >> 6) & 1, "classifier accumulator saturated (uio[6] sticky)"
    dut._log.info("no saturation on either register")
