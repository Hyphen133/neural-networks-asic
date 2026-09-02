"""cocotb testbench: RTL vs. the bit-exact integer golden model in train/hw.py.

  make                     # 3 real MNIST images + directed tests
  NUM_IMAGES=20 make       # more images
  GATES=yes make           # gate-level (post-synthesis) run

If artifacts/weights_w4_h16.json is missing the test falls back to
deterministic pseudo-random weights, so it runs in a bare CI checkout.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train"))
import numpy as np  # noqa: E402
import hw  # noqa: E402

CLK_NS = 20  # 50 MHz, matches info.yaml clock_hz


# ---------------------------------------------------------------------------
# Low-level driver
# ---------------------------------------------------------------------------
class Driver:
    """Feeds the (byte, is_cmd) protocol -- one byte per clock, no backpressure."""

    def __init__(self, dut):
        self.dut = dut

    def _idle(self):
        self.dut.uio_in.value = 0
        self.dut.ui_in.value = 0

    async def reset(self):
        self.dut.ena.value = 1
        self._idle()
        self.dut.rst_n.value = 0
        await ClockCycles(self.dut.clk, 5)
        self.dut.rst_n.value = 1
        await ClockCycles(self.dut.clk, 2)

    async def send(self, byte: int, is_cmd: int):
        self.dut.ui_in.value = byte & 0xFF
        self.dut.uio_in.value = 0b01 | (0b10 if is_cmd else 0)
        await RisingEdge(self.dut.clk)
        self._idle()

    async def send_stream(self, stream, gap: int = 0):
        for byte, is_cmd in stream:
            await self.send(byte, is_cmd)
            if gap:
                await ClockCycles(self.dut.clk, gap)

    async def cmd(self, op: int, imm: int = 0):
        await self.send(((op & 0xF) << 4) | (imm & 0xF), 1)

    async def read_hidden(self, idx: int) -> int:
        await self.cmd(hw.OP_RD_HIDDEN, idx)
        await ClockCycles(self.dut.clk, 1)      # dbg_out is registered
        v = int(self.dut.uo_out.value) & 0xF
        await self.cmd(hw.OP_DBG_OFF)
        return v

    def result(self):
        uo = int(self.dut.uo_out.value)
        return dict(cls=uo & 0xF, valid=(uo >> 4) & 1,
                    in_l2=(uo >> 5) & 1, ovf=(uo >> 6) & 1)


def get_weights(hidden: int = 16):
    """Load artifacts/weights_$WTAG.json, else deterministic random weights."""
    tag = os.environ.get("WTAG", f"w4_h{hidden}")
    try:
        m = hw.load_weights(tag)
        assert m["hidden"] == hidden
        return m
    except Exception:
        rng = np.random.default_rng(7)
        return dict(
            w1=rng.integers(hw.W_MIN, hw.W_MAX + 1, (hidden, hw.N_IN)).astype(np.int64),
            b1=rng.integers(-2000, 2000, hidden).astype(np.int64),
            w2=rng.integers(hw.W_MIN, hw.W_MAX + 1, (hw.N_CLASS, hidden)).astype(np.int64),
            b2=rng.integers(-500, 500, hw.N_CLASS).astype(np.int64),
            shift1=6, hidden=hidden, mode="random",
        )


def get_images(n: int):
    try:
        (_, _), (xte, yte) = hw.load_mnist_u4(hw.DEFAULT_DATA_DIR)
        return xte[:n], yte[:n]
    except Exception:
        rng = np.random.default_rng(11)
        return rng.integers(0, 16, (n, hw.N_IN)).astype(np.int32), np.full(n, -1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@cocotb.test()
async def test_reset(dut):
    """After reset the chip is idle, in layer 1, with no valid result."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    drv = Driver(dut)
    await drv.reset()
    r = drv.result()
    assert r["valid"] == 0, "result_valid must be low after reset"
    assert r["in_l2"] == 0, "must start in layer 1"
    assert r["ovf"] == 0, "overflow flag must be clear"
    assert int(dut.uio_oe.value) == 0b11111100, "uio[1:0] must be inputs"


@cocotb.test()
async def test_bias_load_and_requantize(dut):
    """LOAD_ACC + NEURON_DONE exercises the 3-byte bias path and clamp(0,15)."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    drv = Driver(dut)
    await drv.reset()

    # shift1 = 0 makes hidden == clamp(acc, 0, 15), i.e. the bias itself.
    cases = [(7, 7), (15, 15), (100, 15), (0, 0), (-1, 0), (-4096, 0)]
    await drv.cmd(hw.OP_RESET)
    await drv.cmd(hw.OP_SET_SHIFT, 0)
    for i, (bias, _) in enumerate(cases):
        await drv.cmd(hw.OP_LOAD_ACC)
        for b in hw.bias_bytes(bias):
            await drv.send(b, 0)
        await drv.cmd(hw.OP_NEURON_DONE)

    for i, (bias, want) in enumerate(cases):
        got = await drv.read_hidden(i)
        assert got == want, f"bias {bias}: hidden[{i}] = {got}, expected {want}"

    # And with a shift: 1000 >> 4 = 62 -> saturates to 15; 200 >> 4 = 12.
    await drv.cmd(hw.OP_RESET)
    await drv.cmd(hw.OP_SET_SHIFT, 4)
    for bias in (200, 1000):
        await drv.cmd(hw.OP_LOAD_ACC)
        for b in hw.bias_bytes(bias):
            await drv.send(b, 0)
        await drv.cmd(hw.OP_NEURON_DONE)
    assert await drv.read_hidden(0) == 12
    assert await drv.read_hidden(1) == 15


@cocotb.test()
async def test_mac_and_argmax(dut):
    """Small hand-computed network: checks the signed 4x4 MAC and the argmax."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    drv = Driver(dut)
    await drv.reset()

    await drv.cmd(hw.OP_RESET)
    await drv.cmd(hw.OP_SET_SHIFT, 0)

    # hidden[0] = 3*2 + 5*(-1) = 1     hidden[1] = 15*7 = 105 -> clamp 15
    await drv.cmd(hw.OP_LOAD_ACC)
    for b in hw.bias_bytes(0):
        await drv.send(b, 0)
    await drv.send((3 << 4) | (2 & 0xF), 0)
    await drv.send((5 << 4) | (-1 & 0xF), 0)
    await drv.cmd(hw.OP_NEURON_DONE)

    await drv.cmd(hw.OP_LOAD_ACC)
    for b in hw.bias_bytes(0):
        await drv.send(b, 0)
    await drv.send((15 << 4) | (7 & 0xF), 0)
    await drv.cmd(hw.OP_NEURON_DONE)

    assert await drv.read_hidden(0) == 1
    assert await drv.read_hidden(1) == 15

    # Layer 2: only the first two hidden units are non-zero, so
    #   class 0: 1*1 + 15*1        = 16
    #   class 1: 1*7 + 15*2        = 37   <- winner
    #   class 2: bias 30, no weights = 30
    #   classes 3..9: bias -100
    await drv.cmd(hw.OP_START_L2)
    await ClockCycles(dut.clk, 1)
    assert drv.result()["in_l2"] == 1

    async def do_class(bias, w):
        await drv.cmd(hw.OP_LOAD_ACC)
        for b in hw.bias_bytes(bias):
            await drv.send(b, 0)
        for i in range(16):
            await drv.send((w[i] if i < len(w) else 0) & 0xF, 0)
        await drv.cmd(hw.OP_CLASS_DONE)

    await do_class(0, [1, 1])
    await do_class(0, [7, 2])
    await do_class(30, [])
    for _ in range(7):
        await do_class(-100, [])

    await drv.cmd(hw.OP_FINISH)
    await ClockCycles(dut.clk, 2)
    r = drv.result()
    assert r["valid"] == 1, "result_valid must be set by FINISH"
    assert r["cls"] == 1, f"argmax should be class 1, got {r['cls']}"
    assert r["ovf"] == 0, "no overflow expected"


@cocotb.test()
async def test_mnist_images(dut):
    """End-to-end: real weights + real MNIST images, RTL vs golden model."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    drv = Driver(dut)
    await drv.reset()

    hidden = int(os.environ.get("HIDDEN", "16"))
    n_img = int(os.environ.get("NUM_IMAGES", "3"))
    m = get_weights(hidden)
    xs, ys = get_images(n_img)

    n_correct = 0
    for i in range(n_img):
        x = xs[i]
        stream = hw.build_stream(m["w1"], m["b1"], m["w2"], m["b2"], m["shift1"], x)
        await drv.send_stream(stream)
        await ClockCycles(dut.clk, 2)

        exp_cls, exp_hidden, exp_scores = hw.infer_int(
            m["w1"], m["b1"], m["w2"], m["b2"], m["shift1"], x)

        r = drv.result()
        assert r["valid"] == 1, f"image {i}: result_valid not set"
        assert r["ovf"] == 0, f"image {i}: accumulator overflowed"

        for h in range(hidden):
            got = await drv.read_hidden(h)
            assert got == int(exp_hidden[h]), (
                f"image {i}: hidden[{h}] RTL={got} golden={int(exp_hidden[h])}")

        assert r["cls"] == exp_cls, (
            f"image {i}: RTL class {r['cls']} != golden {exp_cls} "
            f"(scores {exp_scores.tolist()})")
        n_correct += int(exp_cls == ys[i])
        dut._log.info(
            f"image {i}: label={ys[i]} pred={exp_cls} (RTL matches golden), "
            f"{len(stream)} bytes")

    dut._log.info(f"{n_correct}/{n_img} images classified correctly")


@cocotb.test()
async def test_stalled_stream(dut):
    """The host may pause arbitrarily: idle cycles must not change state."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, units="ns").start())
    drv = Driver(dut)
    await drv.reset()

    m = get_weights(16)
    xs, _ = get_images(1)
    stream = hw.build_stream(m["w1"], m["b1"], m["w2"], m["b2"], m["shift1"], xs[0])

    # Only stall inside the first neuron; a full gapped run would be slow.
    await drv.send_stream(stream[:200], gap=2)
    await drv.send_stream(stream[200:])
    await ClockCycles(dut.clk, 2)

    exp_cls, _, _ = hw.infer_int(m["w1"], m["b1"], m["w2"], m["b2"], m["shift1"], xs[0])
    r = drv.result()
    assert r["valid"] == 1 and r["cls"] == exp_cls, (
        f"stalled stream gave class {r['cls']}, expected {exp_cls}")
