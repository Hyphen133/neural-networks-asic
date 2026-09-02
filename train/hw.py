"""Bit-exact software twin of src/tt_um_mnist_nn.sv.

Pure Python + NumPy, no PyTorch. Imported by:
  * train.py     -- to make the QAT forward pass match silicon
  * export.py    -- to sanity-check exported weights
  * test/test.py -- as the cocotb golden reference

Nothing in here may use floating point during inference.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import os

import numpy as np

# ---------------------------------------------------------------------------
# Hardware constants -- must match the RTL parameters
# ---------------------------------------------------------------------------
HIDDEN = 16          # parameter HIDDEN
ACC_W = 20           # parameter ACC_W
N_IN = 784           # 28*28
N_CLASS = 10

W_MIN, W_MAX = -8, 7        # int4 two's complement weight
A_MIN, A_MAX = 0, 15        # uint4 activation
ACC_MIN = -(1 << (ACC_W - 1))
ACC_MAX = (1 << (ACC_W - 1)) - 1
BIAS_MIN, BIAS_MAX = -(1 << 18), (1 << 18) - 1   # keeps acc inside ACC_W

# ---------------------------------------------------------------------------
# Command opcodes (command byte = opcode<<4 | imm)
# ---------------------------------------------------------------------------
OP_NOP = 0x0
OP_RESET = 0x1
OP_SET_SHIFT = 0x2
OP_LOAD_ACC = 0x3
OP_ZERO_ACC = 0x4
OP_NEURON_DONE = 0x5
OP_START_L2 = 0x6
OP_CLASS_DONE = 0x7
OP_FINISH = 0x8
OP_RD_HIDDEN = 0x9
OP_DBG_OFF = 0xA


@dataclass
class HWConfig:
    hidden: int = HIDDEN
    acc_w: int = ACC_W
    n_in: int = N_IN
    n_class: int = N_CLASS
    shift1: int = 5

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Pixel front end (runs on the host, not on the chip)
# ---------------------------------------------------------------------------
def quantize_pixels(img_u8: np.ndarray) -> np.ndarray:
    """uint8 0..255 -> uint4 0..15, round-to-nearest. Host-side."""
    img_u8 = np.asarray(img_u8)
    return np.clip(np.rint(img_u8.astype(np.int32) * 15 / 255), 0, 15).astype(np.int32)


# ---------------------------------------------------------------------------
# Integer inference -- exactly what the RTL computes
# ---------------------------------------------------------------------------
def _wrap(v: int, width: int) -> int:
    """Two's-complement wrap, mirroring an ACC_W-bit register."""
    m = 1 << width
    v &= m - 1
    return v - m if v & (m >> 1) else v


def requantize(acc: int, shift1: int) -> int:
    """hidden = clamp(acc >>> shift1, 0, 15) with ReLU folded in."""
    if acc < 0:
        return 0
    return min(A_MAX, acc >> shift1)


def infer_int(w1, b1, w2, b2, shift1: int, x4, acc_w: int = ACC_W):
    """Single-image integer forward pass.

    w1: (H, 784) int in [-8, 7]      b1: (H,)  int
    w2: (10, H) int in [-8, 7]       b2: (10,) int
    x4: (784,)  int in [0, 15]

    Returns (pred_class, hidden[H], scores[10]).
    """
    w1 = np.asarray(w1, dtype=np.int64)
    w2 = np.asarray(w2, dtype=np.int64)
    b1 = np.asarray(b1, dtype=np.int64)
    b2 = np.asarray(b2, dtype=np.int64)
    x4 = np.asarray(x4, dtype=np.int64).reshape(-1)

    hidden = np.zeros(w1.shape[0], dtype=np.int64)
    for n in range(w1.shape[0]):
        acc = _wrap(int(b1[n]), acc_w)
        for p in range(x4.size):
            acc = _wrap(acc + int(x4[p]) * int(w1[n, p]), acc_w)
        hidden[n] = requantize(acc, shift1)

    scores = np.zeros(w2.shape[0], dtype=np.int64)
    best_score, best_class = ACC_MIN, 0
    for c in range(w2.shape[0]):
        acc = _wrap(int(b2[c]), acc_w)
        for h in range(w2.shape[1]):
            acc = _wrap(acc + int(hidden[h]) * int(w2[c, h]), acc_w)
        scores[c] = acc
        if acc > best_score:            # strict '>', so ties go to lowest class
            best_score, best_class = acc, c

    return best_class, hidden, scores


def infer_int_batch(w1, b1, w2, b2, shift1: int, x4_batch, acc_w: int = ACC_W):
    """Vectorised equivalent of infer_int for accuracy sweeps.

    Identical results as long as no accumulator wraps (asserted).
    """
    w1 = np.asarray(w1, dtype=np.int64)
    w2 = np.asarray(w2, dtype=np.int64)
    b1 = np.asarray(b1, dtype=np.int64)
    b2 = np.asarray(b2, dtype=np.int64)
    x = np.asarray(x4_batch, dtype=np.int64).reshape(len(x4_batch), -1)

    acc1 = x @ w1.T + b1                                # (N, H)
    assert acc1.min() >= ACC_MIN and acc1.max() <= ACC_MAX, "layer-1 acc overflow"
    hidden = np.clip(acc1 >> shift1, 0, A_MAX)
    hidden[acc1 < 0] = 0

    acc2 = hidden @ w2.T + b2                           # (N, 10)
    assert acc2.min() >= ACC_MIN and acc2.max() <= ACC_MAX, "layer-2 acc overflow"
    return acc2.argmax(axis=1), hidden, acc2


# ---------------------------------------------------------------------------
# Byte-stream builder (host protocol)
# ---------------------------------------------------------------------------
def _cmd(op: int, imm: int = 0) -> tuple[int, int]:
    return (((op & 0xF) << 4) | (imm & 0xF), 1)


def _data(byte: int) -> tuple[int, int]:
    return (byte & 0xFF, 0)


def bias_bytes(value: int, acc_w: int = ACC_W) -> list[int]:
    """Encode a signed bias as the 3 data bytes LOAD_ACC shifts in (MSB first).

    Only the low acc_w bits survive the shift register, so mask accordingly.
    """
    assert BIAS_MIN <= value <= BIAS_MAX, f"bias {value} out of range"
    v = value & ((1 << acc_w) - 1)
    return [(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]


def _nibble(v: int) -> int:
    return v & 0xF


def build_stream(w1, b1, w2, b2, shift1: int, x4, acc_w: int = ACC_W):
    """Full (byte, is_cmd) sequence to classify one image.

    Length = 2 + H*(1+3+784+1) + 1 + 10*(1+3+H+1) + 1 bytes,
    i.e. 12_838 bytes for H=16 -> 257 us / 3_900 images per second at 50 MHz.
    """
    w1 = np.asarray(w1, dtype=np.int64)
    w2 = np.asarray(w2, dtype=np.int64)
    x4 = np.asarray(x4, dtype=np.int64).reshape(-1)
    H = w1.shape[0]

    s: list[tuple[int, int]] = []
    s.append(_cmd(OP_RESET))
    s.append(_cmd(OP_SET_SHIFT, shift1))

    # ---- layer 1: one hidden neuron at a time, single accumulator ----
    for n in range(H):
        s.append(_cmd(OP_LOAD_ACC))
        s += [_data(b) for b in bias_bytes(int(b1[n]), acc_w)]
        for p in range(x4.size):
            s.append(_data((_nibble(int(x4[p])) << 4) | _nibble(int(w1[n, p]))))
        s.append(_cmd(OP_NEURON_DONE))

    # ---- layer 2: streaming argmax ----
    s.append(_cmd(OP_START_L2))
    for c in range(w2.shape[0]):
        s.append(_cmd(OP_LOAD_ACC))
        s += [_data(b) for b in bias_bytes(int(b2[c]), acc_w)]
        for h in range(H):
            s.append(_data(_nibble(int(w2[c, h]))))
        s.append(_cmd(OP_CLASS_DONE))

    s.append(_cmd(OP_FINISH))
    return s


def stream_length(hidden: int = HIDDEN, n_in: int = N_IN, n_class: int = N_CLASS) -> int:
    return 2 + hidden * (1 + 3 + n_in + 1) + 1 + n_class * (1 + 3 + hidden + 1) + 1


# ---------------------------------------------------------------------------
# Dataset + artifact helpers (no torch -- shared with the cocotb testbench)
# ---------------------------------------------------------------------------
MNIST_FILES = {
    "train_x": "train-images-idx3-ubyte.gz",
    "train_y": "train-labels-idx1-ubyte.gz",
    "test_x": "t10k-images-idx3-ubyte.gz",
    "test_y": "t10k-labels-idx1-ubyte.gz",
}
MNIST_URL = "https://ossci-datasets.s3.amazonaws.com/mnist/"


def _fetch(root: str, name: str) -> str:
    """Download once into root/. Falls back to curl (a stock macOS python
    often has no CA bundle configured, which breaks urllib but not curl)."""
    path = os.path.join(root, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(root, exist_ok=True)
    url = MNIST_URL + name
    try:
        import urllib.request
        urllib.request.urlretrieve(url, path)
    except Exception:
        import subprocess
        subprocess.run(["curl", "-sSfL", "-o", path, url], check=True)
    return path


def _read_idx(path: str) -> np.ndarray:
    import gzip
    import struct
    with gzip.open(path, "rb") as f:
        buf = f.read()
    ndim = struct.unpack(">HBB", buf[:4])[2]
    dims = struct.unpack(">" + "I" * ndim, buf[4:4 + 4 * ndim])
    return np.frombuffer(buf[4 + 4 * ndim:], dtype=np.uint8).reshape(dims)


def load_mnist_u4(root: str):
    """MNIST as uint4 numpy arrays: ((xtr, ytr), (xte, yte)).

    xtr/xte are (N, 784) int32 in [0, 15]; ytr/yte are (N,) int64.
    No torch, no torchvision -- the testbench imports this too.
    """
    raw = {k: _read_idx(_fetch(root, v)) for k, v in MNIST_FILES.items()}

    def prep(x, y):
        xq = quantize_pixels(x).reshape(len(x), -1).astype(np.int32)
        return xq, y.astype(np.int64)

    return (prep(raw["train_x"], raw["train_y"]),
            prep(raw["test_x"], raw["test_y"]))


DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "artifacts", "data", "MNIST", "raw")
ARTIFACT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")


def load_weights(tag: str = "w4_h16", art_dir: str = None):
    """Load an exported weight set: dict with w1,b1,w2,b2,shift1,hidden,mode."""
    import json
    art_dir = art_dir or ARTIFACT_DIR
    with open(os.path.join(art_dir, f"weights_{tag}.json")) as f:
        m = json.load(f)
    for k in ("w1", "b1", "w2", "b2"):
        m[k] = np.asarray(m[k], dtype=np.int64)
    return m


# ---------------------------------------------------------------------------
# Executable spec of the RTL FSM -- validates the *protocol*, not just the math
# ---------------------------------------------------------------------------
class StreamModel:
    """Cycle-accurate-enough Python model of tt_um_mnist_nn's byte interface.

    One `step()` == one clock with in_valid high. Used to prove that
    build_stream() drives the FSM correctly, independently of the RTL.
    """

    S_L1, S_L2, S_BIAS = 0, 1, 2

    def __init__(self, hidden: int = HIDDEN, acc_w: int = ACC_W):
        self.h = hidden
        self.acc_w = acc_w
        self.reset()

    def reset(self):
        self.state = self.S_L1
        self.ret_state = self.S_L1
        self.bias_cnt = 0
        self.acc = 0
        self.best_score = -(1 << (self.acc_w - 1))
        self.best_class = 0
        self.class_idx = 0
        self.h_idx = 0
        self.shift1 = 5
        self.hidden = [0] * self.h
        self.result_valid = False
        self.acc_ovf = False
        self.dbg_en = False
        self.dbg_out = 0

    def step(self, byte: int, is_cmd: int):
        byte &= 0xFF
        if is_cmd:
            op, imm = byte >> 4, byte & 0xF
            if op == OP_RESET:
                self.reset()
            elif op == OP_SET_SHIFT:
                self.shift1 = imm
            elif op == OP_LOAD_ACC:
                if self.state != self.S_BIAS:
                    self.ret_state = self.state
                self.state, self.bias_cnt, self.acc = self.S_BIAS, 0, 0
            elif op == OP_ZERO_ACC:
                self.acc = 0
            elif op == OP_NEURON_DONE:
                self.hidden[self.h_idx] = requantize(self.acc, self.shift1)
                self.h_idx = (self.h_idx + 1) % self.h
                self.acc = 0
            elif op == OP_START_L2:
                self.state = self.ret_state = self.S_L2
                self.h_idx = self.class_idx = self.acc = 0
            elif op == OP_CLASS_DONE:
                if self.acc > self.best_score:
                    self.best_score, self.best_class = self.acc, self.class_idx
                self.class_idx = (self.class_idx + 1) & 0xF
                self.h_idx = self.acc = 0
            elif op == OP_FINISH:
                self.result_valid = True
            elif op == OP_RD_HIDDEN:
                self.dbg_out, self.dbg_en = self.hidden[imm % self.h], True
            elif op == OP_DBG_OFF:
                self.dbg_en = False
        elif self.state == self.S_BIAS:
            self.acc = _wrap((self.acc << 8) | byte, self.acc_w)
            if self.bias_cnt == 2:
                self.state, self.bias_cnt = self.ret_state, 0
            else:
                self.bias_cnt += 1
        else:
            act = self.hidden[self.h_idx] if self.state == self.S_L2 else (byte >> 4)
            w = (byte & 0xF) - 16 if byte & 0x8 else (byte & 0xF)
            nxt = _wrap(self.acc + act * w, self.acc_w)
            if (self.acc >= 0) == (act * w >= 0) and (nxt >= 0) != (self.acc >= 0):
                self.acc_ovf = True
            self.acc = nxt
            if self.state == self.S_L2:
                self.h_idx = (self.h_idx + 1) % self.h

    def run(self, stream):
        for byte, is_cmd in stream:
            self.step(byte, is_cmd)
        return self
