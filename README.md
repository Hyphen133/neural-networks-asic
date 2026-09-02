# tinytapeout-mnist-nn-asic

A W4A4 MNIST classifier (`784 → 16 → 10`) that fits a **TinyTapeout IHP 1×1
tile**, plus the quantization-aware training that produces bit-exact weights
for it.

The RTL and the PyTorch forward pass implement *the same integer arithmetic*,
so there is no "96 % in PyTorch, 89 % in silicon" gap: the cocotb testbench
asserts that the RTL reproduces the Python model's hidden activations and
predicted class exactly, image by image.

```
                        ui_in[7:0]                     uo_out[3:0]
   host ───{pixel4, weight4}───► ┌──────────────┐ ──────► digit 0-9
             1 byte / clock      │  4x4 MAC     │         uo_out[4] valid
                                 │  20-bit acc  │
                                 │  >>s, clamp  │
                                 │  16 x 4b h[] │
                                 │  argmax      │
                                 └──────────────┘
```

## Results

Integer accuracy on the full 10 000-image MNIST test set, and post-synthesis
cell area against the real IHP `sg13g2` liberty (`./area_check.sh`). The 1×1
tile is 202.08 × 154.98 µm = 31 318 µm²; OpenLane's default placement density
in the ttihp template is 60 %.

| config                | test acc | cells | flops | area µm² | tile util |
|-----------------------|---------:|------:|------:|---------:|----------:|
| W4A4, HIDDEN=8        |  92.15 % |   829 |   104 |   12 430 |   **40 %** |
| **W4A4, HIDDEN=16** — default | **95.47 %** | 998 | 138 | 15 688 | **50 %** |
| W4A4, HIDDEN=32       |  97.13 % |  1246 |   204 |   21 246 |     68 % |
| Ternary(W2)A4, H=8    |  87.30 % |   770 |   104 |   11 904 |     38 % |
| Ternary(W2)A4, H=16   |  92.76 % |  1005 |   138 |   15 197 |     49 % |
| Ternary(W2)A4, H=32   |  95.55 % |  1255 |   204 |   20 803 |     66 % |
| W4A4, H=16, `SHIFT_FIXED=6` | 95.47 % | 893 | 134 | 14 596 |   47 % |

**HIDDEN=16 is the default**: 50 % tile utilisation leaves real headroom for
CTS, buffering and routing under the template's 60 % density target.
HIDDEN=32 buys +1.7 points of accuracy but needs the density raised to ~70 %,
which is where ttihp 1×1 routing starts to get unhappy — treat it as a stretch
goal, not the thing you tape out first.

### The ternary weights are not worth it here

The base plan assumed a ternary `{-1,0,+1}` datapath would be the big area win.
Measured, it saves **491 µm² — 3.1 %** (`MULT_TERNARY=1`), because the 4×4
multiplier was never the cost driver. The area is in flip-flops (6 760 µm²,
43 % of the total), the hidden-activation register-file muxing, and the
variable barrel shifter. Meanwhile ternary costs 2.7 accuracy points at the
same HIDDEN.

At equal area the comparison is decisive: W4A4 @ H=16 (95.47 %, 15 688 µm²)
beats ternary @ H=16 (92.76 %, 15 197 µm²) outright, and roughly ties ternary
@ H=32 (95.55 %, 20 803 µm²) while using 25 % less area. **Keep the
multiplier.**

The cheapest real saving is `SHIFT_FIXED=6`, which hard-wires the
requantisation shift and deletes the 20-bit barrel shifter: −1 092 µm² for zero
accuracy cost (verified: `SHIFT_FIXED=6 make` in `test/` passes the same
image-by-image comparison). The only thing you give up is being able to re-tune the shift
after tape-out, which matters if you plan to retrain.

## Why it fits

Two decisions do all the work.

**1. The chip stores no weights and no image.** 784×16 weights at 4 bits is
6 kB — hopeless on 1×1. The host streams one `{pixel, weight}` byte per clock;
8 bits in, one MAC out, perfectly matched bandwidth.

**2. Neuron-outer loop order.** Streaming pixel-outer would need 16 separate
accumulators (16 × 20 = 320 flops plus read/write muxing). Streaming
*neuron-outer* — all 784 pixels for hidden unit 0, then unit 1, … — needs
**one** 20-bit accumulator, reused again for the output layer. Same host
traffic, ~180 fewer flops.

Everything else follows: only the winning class and its score are kept (no
10-element score vector), biases are shifted in through the accumulator itself
so there is no separate bias load path, and ReLU is free because the clamp
already discards negatives.

Total on-chip state, 138 flops:

```
 20  accumulator (shared: layer 1, layer 2, bias shift-in)
 64  hidden[16] x 4-bit activations
 20  best_score
  4  best_class        4  class_idx        4  hidden index
  4  shift1            2  state            2  ret_state      2  bias_cnt
  1  result_valid      1  acc_overflow     1  ack
  5  debug readback + slack
```

## Layout

```
src/tt_um_mnist_nn.sv    the design (parameterised: HIDDEN, ACC_W,
                         MULT_TERNARY, SHIFT_FIXED)
docs/design-document.html  full design document: architecture, protocol,
                         training, measured area, scaling limits
info.yaml                TinyTapeout project metadata, tiles: "1x1"
docs/info.md             datasheet: protocol, opcode table, how to test
test/                    cocotb testbench (RTL and gate-level)
train/hw.py              bit-exact integer model + byte-stream protocol + a
                         Python model of the RTL FSM. No torch.
train/train.py           QAT with straight-through estimators
train/export.py          full-test-set eval, C header, playable byte stream
area_check.sh            yosys area sweep against the sg13g2 liberty
```

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install numpy torch cocotb

# train (MNIST downloads itself; ~1 s/epoch on CPU)
.venv/bin/python train/train.py --mode w4 --hidden 16 --epochs 60

# full test-set accuracy + C header + a playable byte stream
.venv/bin/python train/export.py --tag w4_h16 --stream

# RTL simulation against the golden model
cd test && NUM_IMAGES=5 make

# post-synthesis area vs. the 1x1 tile
./area_check.sh
```

## Porting to ttihp-verilog-template

1. `git clone https://github.com/TinyTapeout/ttihp-verilog-template`
2. Copy `src/tt_um_mnist_nn.sv` into `src/`, delete the template's
   `project.v`, and point `source_files:` at the `.sv` file.
3. Copy `info.yaml`, `docs/info.md`, and the `test/` directory over the
   template's, and fill in `author` / `discord`.
4. The template's `test.yaml` and `gds.yaml` workflows then run unmodified —
   `tiles: "1x1"` is already set.

The testbench needs `numpy` (for the golden model) on top of the template's
`cocotb`; add it to `test/requirements.txt`. `train/` and `artifacts/` are not
consumed by the GDS flow — keep them or strip them, either works.

## Interface

`uio[0] = in_valid`, `uio[1] = is_cmd`, one byte per clock on `ui_in`. The host
may stall arbitrarily; there is no backpressure to honour. Full opcode table
and the exact stream layout are in [`docs/info.md`](docs/info.md).

Throughput: 12 838 bytes/image = 257 µs at 50 MHz, ≈3 900 images/s.

## Verification

The RTL lints clean under `verilator -Wall` for every
`HIDDEN × MULT_TERNARY × SHIFT_FIXED` combination listed above.

`test/test.py` runs five cocotb tests: reset state, the 3-byte bias load path
and `clamp(acc >>> s, 0, 15)` including saturation, a hand-computed MAC and
argmax, real MNIST images end-to-end (every hidden activation and the class are
compared against `hw.infer_int`), and a stalled stream to prove idle cycles are
inert.

There is a third independent model: `hw.StreamModel` is a pure-Python
implementation of the RTL's FSM. `train/export.py --check N` replays the host
protocol through it and compares against `hw.infer_int`, which catches protocol
bugs that a pure-arithmetic reference would miss.

Parameter overrides work from the Makefile, and `test.py` follows them:

```bash
HIDDEN=8 make                         # the 8-hidden-unit build
MULT_TERNARY=1 WTAG=ternary_h16 make  # the ternary datapath
SHIFT_FIXED=6 make                  # hard-wired requantisation shift
GATES=yes make                      # post-synthesis netlist
```

All of the above pass.
