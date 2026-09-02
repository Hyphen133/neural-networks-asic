# From idea to tapeout: a chip-design tutorial, using this repo as the worked example

This is a step-by-step guide to designing a small digital ASIC for a
Tiny Tapeout IHP `sg13g2` tile. Every step is illustrated with what was actually
done in this repository: the W4A4 MNIST classifier that fits a 1×1 tile
(`src/tt_um_mnist_nn.sv`), and the two audio designs (`docs/kws.md`,
`wakeword/FINDINGS.md`) that show what happens when a step is skipped.

Each step ends with two short lists:

* **Key decisions** — the choices that shaped the result, and why.
* **Metrics** — the numbers you must have before moving on, and how to get them.

The flow, in one picture:

```
 1. budget ──► 2. task ──► 3. arithmetic ──► 4. golden model ──► 5. training
                                                    │                  │
                                                    ▼                  ▼
 9. tapeout ◄── 8. synth/area ◄── 7. verify ◄── 6. RTL ◄──────── weights
        ▲                             │
        └───── loop back on any red number ─────┘
```

The order matters. Steps 1–5 happen in Python before a line of RTL exists.
That is deliberate: RTL is the most expensive thing to change, so every
decision that *can* be settled in software is settled first.

---

## Step 1 — Measure the budget before you design anything

A design is a way of spending area. You cannot spend what you have not counted.

**What a 1×1 IHP tile gives you** (from `docs/design-space-1x1.md` §1):

| quantity | value |
|---|---|
| tile | 202.08 × 154.98 µm = **31 318 µm²** |
| usable cell area @ 60 % density (OpenLane default) | **18 800 µm²** |
| usable @ 80 % density (sometimes routes) | 25 000 µm² |
| one D flip-flop (`sg13g2_dfrbp_1`) | 52.6 µm² |
| one NAND2 | 7.3 µm² |
| one 2:1 mux | 18.1 µm² |

**The rule that actually predicts area.** Across nine synthesis runs of two
unrelated designs, total area tracked flip-flop count, not gate count:

```
area ≈ flops × 110 µm²     (±8 %)
→ a 1×1 tile holds ~170 flops at 60 % density, ~227 at 80 %
```

The factor of ~2 over the raw flop cell area is the muxing and decoding that
surrounds every register in a time-multiplexed datapath. **You run out of
registers, not arithmetic.** The first version of the design-space document
estimated logic by guessing a cell count and was wrong by 2× every time; the
flop-based heuristic replaced it after the KWS design came in at 100 % of the
tile instead of the predicted 51 %.

**Key decisions**
* Tile count. 1×1 here. More tiles buy area and, from 1×2 upward, SRAM macros
  (see `docs/cnn-with-sram.md`), but cost shuttle money and integration risk.
* Density target. Design to 60 %. Treat 80 % as a stretch that may not route.

**Metrics**
* Available cell area in µm² at your chosen density.
* Your **flop budget**: `usable_area / 110`. Write it down; everything in
  steps 2–6 is measured against it.
* How to get them: `./area_check.sh` prints cells, flops, area and tile
  utilisation for the current RTL against the real liberty file.

---

## Step 2 — Pick a task the budget can hold, and prove it in Python first

With ~170 bits of state and no SRAM, most "tiny ML" architectures are dead on
arrival. A single 784→16 layer has 12 544 weights; at one bit each that is
70× the tile's total storage. This is **the weight wall**, and every viable
design must choose a route around it:

| route | storage | consequence |
|---|---|---|
| **A. stream weights from the host** | 0 | arbitrary model size; chip is not self-contained (this repo) |
| B. generate weights from an LFSR | 0 | random projections only; weights cannot be chosen |
| C. weights as combinational constants | ~1 cell/bit | a few hundred bits; frozen at tapeout (the audio designs) |
| D. weights in flops | ~110 µm²/bit | ≤170 bits, so few they could be trained on-chip |

**Validate the task in software before RTL.** The two audio designs are the
cautionary tale. The wake-word spotter was pitched at "~85–92 %" accuracy and
51 % utilisation. Built and measured: **1 % recall at 5 % false alarms** and
**100 % utilisation**. A float MLP with 128 hidden units on the *same* features
did no better, which proved the ceiling was the 16-integer feature
representation, not the arithmetic. None of that needed RTL to discover; it
needed a Python model and a dataset, which is where it should have started.

**Key decisions**
* Route around the weight wall. This design chose A: the host streams one
  `{pixel, weight}` byte per clock, so the chip stores neither weights nor
  image. Consequence accepted: a microcontroller is always in the loop.
* Task framing. Because weights are streamed, the silicon is a general
  W4A4 784→H→10 MAC engine, not "an MNIST chip". Any 28×28 dataset runs on it.
* Accuracy floor. Decide the number below which you stop, *before* training.

**Metrics**
* Estimated flops for the architecture, vs. the budget from step 1.
  `design-space-1x1.md` §3 scores five candidates this way; three do not fit.
* Task accuracy on a held-out split, in **integer arithmetic**, from a
  software model. For classification use accuracy; for detection use recall at
  a fixed false-alarm rate (AUC alone hides the operating point).
* A quick ceiling check: train an over-sized float model on the same input
  representation. If *it* fails, the representation is the problem.

---

## Step 3 — Fix the arithmetic exactly

Decide every bit width and every rounding rule now, and never again. The RTL,
the golden model and the training forward pass must all implement this table
(`docs/info.md`):

| value | format | why |
|---|---|---|
| pixel | uint4, 0…15 | 8 input pins = one pixel nibble + one weight nibble per clock |
| weight | int4 two's complement, −8…+7 | 4×4 multiplier is small; ternary measured not worth it (step 8) |
| product | int8, −120…+105 | |
| accumulator | int20 | worst case 784 × 15 × 8 = 94 080 needs 18 bits; 20 leaves room for a loaded bias |
| hidden activation | `clamp(acc >>> s, 0, 15)` → uint4 | ReLU is free: the clamp discards negatives |
| output score | int20, compared on the fly | only the running best is stored |

Two details that are easy to get wrong and expensive to fix later:

* **Requantisation is a floor arithmetic shift**, not a rounding divide. The
  training script's `RequantSTE` implements exactly `floor(acc / 2**s)` so the
  gradient path sees the same function the hardware computes.
* **Overflow is detected, not prevented.** `acc_ovf` is a sticky flag on
  `uo_out[6]`. The Python batch evaluator asserts no overflow on the whole test
  set, so the flag should never fire with shipped weights, and if it does the
  host can see it.

**Key decisions**
* W4A4. Chosen because it maps onto 8 pins and one small multiplier. The
  ternary alternative was kept as a parameter (`MULT_TERNARY`) so it could be
  measured rather than argued about.
* Accumulator width 20 with a 3-byte bias load, rather than a separate bias
  path. The bias is shifted in *through the accumulator*, so no extra register.
* Programmable shift (`SET_SHIFT`) vs. hard-wired (`SHIFT_FIXED`). Programmable
  costs a 20-bit barrel shifter, ~1 100 µm². Kept programmable so weights can be
  retrained after tapeout; the fixed variant is one parameter away.

**Metrics**
* Worst-case magnitude of every accumulator, computed by hand from the widths.
* Bias range (`BIAS_MIN/MAX = ±2^18`) that keeps the accumulator inside 20 bits.
* Accuracy cost of each width choice, from step 5's integer evaluation.

---

## Step 4 — Write the bit-exact golden model before the RTL

`train/hw.py` is the specification. It is pure Python and NumPy, imports no
PyTorch, and uses **no floating point during inference**. Three things live in
it, and three consumers import it:

```
hw.infer_int         scalar reference: the arithmetic, one image, loops as the RTL loops
hw.infer_int_batch   vectorised twin, asserts equality with the scalar path, used for 10k-image sweeps
hw.StreamModel       a Python model of the RTL's FSM: consumes the byte protocol, not just the math
hw.build_stream      the host protocol encoder: produces the exact byte sequence a microcontroller would send

train.py ─┐
export.py ├─► hw.py ◄─ test/test.py (cocotb golden reference)
          ┘
```

Why a model of the *FSM* as well as the arithmetic: `export.py --check`
replays the real byte stream through `StreamModel` and compares against
`infer_int`. That catches protocol bugs (wrong opcode order, off-by-one in the
bias shift) that an arithmetic-only reference cannot see, and it does so
without a simulator.

**Key decisions**
* One source of truth for constants (`HIDDEN`, `ACC_W`, opcodes) that the RTL
  parameters must match. The testbench reads the same module.
* Two independent implementations of the same math (scalar loop, vectorised)
  asserted equal at the end of every training run.

**Metrics**
* `reference self-check ok` printed by `train.py`: scalar == vector on a
  sample image.
* `export.py --check N`: N images where `StreamModel` == `infer_int`, no
  overflow, `result_valid` reached.

---

## Step 5 — Train *to* the hardware, not near it

Quantisation-aware training (QAT) here means the PyTorch forward pass **is**
the integer model: rounded int4 weights, uint4 activations, integer bias units,
floor-shift requantisation. Only the backward pass is floating point, via
straight-through estimators (`train/train.py`):

| STE | forward | backward |
|---|---|---|
| `RoundClampSTE` | `clamp(round(x), lo, hi)` | pass gradient where x is in range |
| `TernarySTE` | sign threshold to {−1,0,+1} | pass where \|x\| ≤ 1.5 |
| `RequantSTE` | `clamp(floor(acc/2^s), 0, 15)` | derivative of the unquantised map, gated to [0, 15] |

Three tricks that cost nothing in silicon but matter for accuracy:

1. **Shift calibration.** Before training, run the random-init layer 1 on 4096
   images and pick `s` so that the 99th percentile of positive sums lands at
   ~12 of the 0–15 range. The shipped model uses `shift1 = 6`.
2. **A logit temperature** on the output, so cross-entropy is well conditioned.
   It scales all scores equally, so argmax is unchanged and it never reaches
   the chip.
3. **Latent weight clipping** after each step, so the float weights cannot
   drift far from their quantised values.

**Evaluate in integers every epoch.** The log for the shipped weights
(`artifacts/logs/w4_h16.log`) reports `int-test` accuracy from
`hw.infer_int_batch`, not from the PyTorch model. There is therefore no
"96 % in PyTorch, 89 % in silicon" gap: the number you read is the number the
chip gets.

**Key decisions**
* Keep the model tiny and train fast: ~1 s per epoch on CPU, 60 epochs.
* Augment with ±1 px shifts only. Worth ~0.5–1 point at these widths.
* Select the best epoch on integer test accuracy, export as `.npz` + `.json`.

**Metrics**
* **Integer test accuracy on the full 10 000-image set.** Shipped: **95.47 %**.
* Weight histogram: does the model use the whole int4 range? Shipped `w1` spans
  −8…+7 with mass concentrated in −3…+3, which says int4 is not wasted.
* Sweep results that justify the size choice (full table in step 8):

  | HIDDEN | int accuracy |
  |---:|---:|
  | 8 | 92.15 % |
  | **16** | **95.47 %** |
  | 32 | 97.13 % |

---

## Step 6 — Architecture and RTL: spend flops only where you must

The architecture is the loop order. Everything else follows from it.

**The decisive idea: neuron-outer streaming.** If pixels arrive pixel-outer,
every hidden neuron needs its own accumulator: 16 × 20 = 320 flops plus
read/write muxing. If the host sends all 784 pixels for neuron 0, then all 784
for neuron 1, and so on, **one** 20-bit accumulator suffices, and the same
accumulator serves the output layer and the bias load. Same host bandwidth,
~180 fewer flops.

The whole on-chip state, 138 flops (`README.md`):

```
 20  accumulator (shared: layer 1, layer 2, bias shift-in)
 64  hidden[16] × 4-bit activations
 20  best_score          4  best_class    4  class_idx     4  hidden index
  4  shift1              2  state         2  ret_state     2  bias_cnt
  1  result_valid        1  acc_overflow  1  ack           5  debug + slack
```

Notice what is *not* stored: no image, no weights, no 10-element score vector
(only the running argmax), no separate bias register.

**The host interface** is a raw parallel handshake, one byte per clock, no
backpressure: `uio[0] = in_valid`, `uio[1] = is_cmd`, byte on `ui_in`. A
command byte is `{opcode[3:0], imm[3:0]}` with eleven opcodes
(`RESET`, `SET_SHIFT`, `LOAD_ACC`, `NEURON_DONE`, `START_L2`, `CLASS_DONE`,
`FINISH`, two debug ops, `NOP`, `ZERO_ACC`). A UART or SPI receiver would have
cost more flops than the datapath; a parallel bus costs none.

**Parameterise from the first commit.** `HIDDEN`, `ACC_W`, `MULT_TERNARY` and
`SHIFT_FIXED` are module parameters, so step 8's area sweep and step 7's tests
run over the whole design space with `chparam` and environment variables
instead of code edits.

**Key decisions**
* One multiplier, one adder, one accumulator, time-shared four ways.
* Neuron-outer loop order driven by the host. The chip has no address
  counters for the image because it never sees the image as a whole.
* Running argmax with strict `>`, so ties go to the lowest class — and the
  golden model uses the same rule.
* Debug readback (`RD_HIDDEN`) of every hidden activation. It costs 5 flops
  and is what makes step 7's image-by-image comparison possible on real
  silicon, not only in simulation.

**Metrics**
* Flop count from the state table, vs. the budget from step 1. 138 of ~170.
* `verilator -Wall` clean for every parameter combination.
* Cycle count per inference: 12 838 bytes = 257 µs at 50 MHz ≈ 3 900 img/s.

---

## Step 7 — Verify against the golden model, then against the gates

The testbench (`test/test.py`, cocotb + Icarus) has five tests, ordered from
cheap directed checks to the load-bearing end-to-end one:

| test | what it proves |
|---|---|
| `test_reset` | idle state, `uio_oe` direction mask |
| `test_bias_load_and_requantize` | 3-byte bias path; `clamp(acc >>> s, 0, 15)` incl. saturation and negatives |
| `test_mac_and_argmax` | hand-computed signed MAC and argmax tie/ordering |
| `test_mnist_images` | real weights, real images: **every hidden activation and the class** equal `hw.infer_int` |
| `test_stalled_stream` | idle cycles with `in_valid` low change nothing |

The end-to-end test compares *intermediate* state, not just the answer. A
wrong class is one bit of information; sixteen wrong hidden values tell you
which neuron, and therefore which byte, went wrong.

**Bugs this process caught in the audio designs** (`docs/kws.md` §2), each of
which would have shipped silently without a bit-exact model:

* `(env >> D) | 1` is not `max(env >> D, 1)`.
* Zero-extending a signed sample before shifting.
* Zero-width replication `{{0{1'b0}}, x}` reshuffling an output concatenation.
* Trained biases that made an all-zero (quiet-room) input pick a keyword.
* **Icarus silently ignores `-P` parameter overrides** unless `-s` is also
  given. A build whose parameters never took effect compares the RTL against a
  differently configured model and *passes*. The KWS testbench moved to `-D`
  macros and added a test that reads the elaborated parameters back.
  `test/Makefile` for the MNIST design still uses `-P`; its `HIDDEN`,
  `MULT_TERNARY` and `SHIFT_FIXED` overrides should be assumed inert until
  that is fixed the same way.

**Gate-level simulation** (`GATES=yes make`) reruns the same tests on the
post-synthesis netlist with the `sg13g2` cell models. It is slow and catches a
different class of bug (synthesis/RTL mismatch, X-propagation from
uninitialised state), so run it once before tapeout rather than every commit.

**Key decisions**
* Golden model as the oracle, never expected values typed by hand, except in
  the two tiny directed tests that check the oracle itself.
* Compare internals via the debug path, not just outputs.
* Three independent models (arithmetic, FSM, RTL) cross-checked pairwise.

**Metrics**
* Tests passing: 5/5 RTL, 5/5 gate-level, for every parameter combination.
* Number of real images run through the RTL (`NUM_IMAGES=N make`). Sim time
  is the constraint: one image is 12 838 cycles, so tens, not thousands.
* Lint warnings: zero.

---

## Step 8 — Synthesise for area from the first commit, and let it kill ideas

`area_check.sh` runs yosys against the real `sg13g2` liberty and prints one
line per configuration:

```
read_verilog -sv src/tt_um_mnist_nn.sv
chparam -set HIDDEN 16 -set MULT_TERNARY 0 -set SHIFT_FIXED 16 tt_um_mnist_nn
synth -top tt_um_mnist_nn -flatten
dfflibmap -liberty sg13g2.lib     # map flops to real cells
abc -liberty sg13g2.lib           # map logic to real cells
stat -liberty sg13g2.lib          # report cells, flops, area
```

Results, with accuracy from step 5 alongside (`README.md`):

| config | test acc | cells | flops | area µm² | tile util |
|---|---:|---:|---:|---:|---:|
| W4A4, H=8 | 92.15 % | 829 | 104 | 12 430 | 40 % |
| **W4A4, H=16** | **95.47 %** | 998 | 138 | 15 688 | **50 %** |
| W4A4, H=32 | 97.13 % | 1246 | 204 | 21 246 | 68 % |
| Ternary, H=16 | 92.76 % | 1005 | 138 | 15 197 | 49 % |
| Ternary, H=32 | 95.55 % | 1255 | 204 | 20 803 | 66 % |
| W4A4, H=16, `SHIFT_FIXED=6` | 95.47 % | 893 | 134 | 14 596 | 47 % |

Three lessons this table taught that intuition got wrong:

1. **Ternary weights were supposed to be the big area win. They save 3.1 %.**
   The 4×4 multiplier was never the cost; flops (43 % of area), the hidden
   register-file muxing and the barrel shifter are. Ternary costs 2.7 accuracy
   points for that 3.1 %. At equal area W4A4 wins outright. *Keep the
   multiplier.*
2. **Hard-wiring the shift is the cheapest real saving:** −1 092 µm² for zero
   accuracy loss, at the cost of post-tapeout retuning.
3. **H=16 at 50 % is the tapeout choice, not H=32 at 68 %.** 68 % needs the
   placement density raised to ~70 %, where 1×1 routing starts failing. The
   accuracy gain (+1.7 pt) is not worth a design that may not close.

**Where the area really goes in time-multiplexed designs.** The KWS design
tightened every intermediate from 32 bits to exact width and moved total area
by 1 %, because yosys had already dead-coded the unused bits. The 18k µm² of
combinational logic was read muxes and write decoders on indexed register
arrays (`y[bi]`, `slot[s][b]`, a 48-entry ROM). The fix that worked in the
wake-word design: make the arrays **rotating shift registers** so the active
element is always at index 0. −1 000 µm² there, and the reason it shipped at
78.7 % instead of 127 %.

**Key decisions**
* Synthesis against the real liberty, not a gate-count guess. The guess was
  2× low, twice.
* Sweep every parameter axis and pick on the *joint* accuracy–area table.
* Accept 50 % utilisation as done. Headroom is for clock-tree synthesis,
  buffering and routing, which the yosys number does not include.

**Metrics**
* Cell area µm² and tile utilisation per configuration.
* Flop count and its share of area (43–46 % is typical for this design style).
* Area per flop (~110 µm²) as a sanity check that your design is not an outlier.
* Accuracy delta per µm² between adjacent configurations: the exchange rate
  that decides which knob to turn.

---

## Step 9 — Pins, clock, metadata, and handing it to the flow

Tiny Tapeout fixes the interface: 8 dedicated inputs, 8 dedicated outputs,
8 bidirectional pins, one clock, one reset. Design the pin map for
*bring-up*, not only for function (`info.yaml`):

| pins | use |
|---|---|
| `ui[7:0]` | the data/command byte |
| `uio[1:0]` | inputs: `in_valid`, `is_cmd` |
| `uo[3:0]` | class, or a hidden activation when debug readback is on |
| `uo[4]` | `result_valid` |
| `uo[5]` `uo[6]` `uo[7]` | in-layer-2, sticky overflow, registered `in_valid` echo |
| `uio[7:2]` | outputs: hidden index and FSM state |

The `ack` echo on `uo[7]` and the FSM state on `uio[7:6]` cost three flops and
tell you, on a logic analyser, whether the chip is even hearing the host. The
audio designs go further and echo the PDM data bit back out for the same reason.

**Clock.** 50 MHz here; the tile is far from timing-critical (one 4×4 multiply
and a 20-bit add per cycle). The KWS design chose 12.288 MHz instead because it
divides cleanly to a 3.072 MHz PDM bit clock — pick the clock the *interface*
needs, not the fastest one that closes.

**Porting to the template** (`README.md`): clone `ttihp-verilog-template`, drop
the `.sv` into `src/`, copy `info.yaml`, `docs/info.md` and `test/`, add
`numpy` to `test/requirements.txt`. The template's `test.yaml` and `gds.yaml`
workflows then run unchanged. If your utilisation is above 60 %, you must also
raise `FP_CORE_UTIL` in the OpenLane config and accept the routing risk noted
in step 8.

**Key decisions**
* Raw parallel handshake over a serial protocol: zero interface flops.
* Every spare output pin carries debug state.
* Datasheet (`docs/info.md`) written from the golden model's `build_stream`,
  so the documented protocol is the tested protocol.

**Metrics**
* Throughput at the chosen clock: 3 900 img/s, with the host pushing
  12.8 kB per image.
* Utilisation vs. `FP_CORE_UTIL`: 50 % vs. the 60 % default, no override needed.
* GDS workflow green on the template, gate-level tests green (step 7).

---

## Step 10 — Post-mortem: what two failed designs teach about the flow

Both audio designs were built to the same standard as the MNIST tile:
bit-exact golden model, cocotb tests passing, lint clean. Both still failed
their pitch, and the reasons are the reasons this tutorial is ordered the way
it is.

| | pitched | measured | root cause | step that would have caught it |
|---|---|---|---|---|
| KWS area | 15.9k µm², 51 % | 31.4k µm², 100 % | costed logic by guessed cell count | 1 (flop-based heuristic) |
| KWS accuracy | ~85–92 % | 1 % recall @ 5 % FA | 16 integers per 341 ms cannot separate 35 words | 2 (float ceiling probe on the same features) |
| wake-word accuracy v1 | usable | ~0 % recall @ 1 FA/h | classifier was linear; fp32 MLP on same features hit 92–97 % AUC | 2 (probe function class, not just bits) |
| wake-word AUC | 92.4 % | 50.6 % | training saturated the accumulator once at the end, RTL saturates every frame | 5 (train to the *exact* hardware) |

And the wake-word design recovered to a shippable 78.7 % utilisation / 89.3 %
AUC by applying the steps in order: rotating shift registers instead of
indexed arrays (step 8), one hidden layer of four units (step 2's function
class), a constant feature offset (step 3), and per-frame saturation in the
training loop (step 5).

**The checklist this leaves you with:**

1. Count flops first. `area ≈ flops × 110 µm²`; a 1×1 tile is ~170 flops.
2. Route around the weight wall explicitly (stream, LFSR, constants, or flops).
3. Prove the task in integer Python on a real dataset before RTL. Probe the
   ceiling with an over-sized float model on the same features.
4. Fix every bit width and rounding rule in one table; make three
   implementations of it (golden model, training forward pass, RTL).
5. Train to the hardware and report only integer accuracy.
6. Choose the loop order that minimises live state. Parameterise everything.
7. Verify internals against the oracle, image by image, then gate-level once.
8. Synthesise against the real liberty from the first commit; sweep; choose on
   the joint accuracy–area table; stop at ~50–60 % utilisation.
9. Spend spare pins on debug. Pick the clock the interface needs.
10. Write down what was estimated vs. measured, so the next design starts
    from the measured number.

---

## Appendix A — Commands, in flow order

```bash
python -m venv .venv && .venv/bin/pip install numpy torch cocotb

# step 5: train (MNIST downloads itself; ~1 s/epoch on CPU)
.venv/bin/python train/train.py --mode w4 --hidden 16 --epochs 60

# steps 4–5: full-test-set integer accuracy, FSM protocol replay, C header, byte stream
.venv/bin/python train/export.py --tag w4_h16 --check 10 --stream

# step 7: lint, RTL simulation, gate-level simulation
verilator -Wall --lint-only src/tt_um_mnist_nn.sv
cd test && NUM_IMAGES=5 make
cd test && GATES=yes make            # needs the hardened netlist + PDK_ROOT

# step 8: area sweep against the real sg13g2 liberty
./area_check.sh
```

## Appendix B — Metric cheat sheet

| metric | where it comes from | shipped value | what it gates |
|---|---|---|---|
| usable cell area | tile × density | 18 800 µm² @ 60 % | everything |
| flop budget | area / 110 | ~170 | architecture (step 6) |
| flops used | `area_check.sh`, state table | 138 | fit |
| cell area | `area_check.sh` | 15 688 µm², 50 % | tapeout config choice |
| integer test accuracy | `train.py` / `export.py`, 10 000 images | 95.47 % | model choice |
| RTL == golden | `test/test.py`, hidden + class per image | 5/5 tests | RTL correctness |
| FSM == arithmetic | `export.py --check` | 10/10 images | protocol correctness |
| overflow flag | `uo[6]`, `infer_int_batch` assert | never set | bit-width choice |
| lint | `verilator -Wall` | clean, all configs | RTL hygiene |
| throughput | bytes/image × clock | 3 900 img/s @ 50 MHz | host requirements |

## Appendix C — Where to read more in this repo

* `docs/design-document.html` — the full design document for the MNIST tile,
  including the scaling ceilings and the CIFAR-10 experiment.
* `docs/design-space-1x1.md` — the budget derivation and five candidate designs.
* `docs/cnn-with-sram.md` — what changes at 2–4 tiles when SRAM macros appear.
* `docs/kws.md` and `wakeword/FINDINGS.md` — the two audio designs, with every
  negative result kept.
* `docs/info.md` — the datasheet: protocol, opcodes, how to drive the chip.
