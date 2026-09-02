# Sheila wake-word spotter — design reference

TinyTapeout IHP sg13g2, 1×1 tile, commit `42f7cd3` on `tapeout-sheila`.
This is the complete description of what is being manufactured: the
signal processing, the hardware architecture, the training that produced the
weights, the physical implementation, the verification, and the limits.
`FINDINGS.md` is the chronological measurement trail; `docs/info.md` is the
short datasheet TinyTapeout renders. This document is the reference.

---

## 1. What it is

A single-purpose chip that listens to a PDM microphone and raises an output
pin when it hears the word **"sheila"**. There is no host interface, no
memory, no firmware and nothing to load: the classifier weights are constants
in the logic. Power, clock, one microphone data line in, one LED line out.

| | |
|---|---|
| keyword | "sheila", from Google Speech Commands v0.02 (CC BY 4.0) |
| process | IHP SG13G2, 130 nm BiCMOS, open PDK |
| die area | 202.08 × 154.98 µm (TinyTapeout 1×1 tile), core 28 941 µm² |
| cells | 1 246 standard cells after synthesis, 205 flip-flops, 1 714 after hold repair |
| clock | 50 MHz system clock, 1.5625 MHz microphone clock generated on chip |
| classifier | 4 hidden units, ternary weights {−1, 0, +1}, 6-bit saturating accumulators |
| accuracy | 89.1 % test AUC, speaker-disjoint split, tie-aware |
| latency | decision every 335 ms, worst case ~335 ms after the word ends |
| power | ~0.76 mW at 1.2 V (flow estimate, dominated by clock and flops) |
| verification | RTL and gate-level netlist bit-exact against the Python model |

---

## 2. Interface

### 2.1 Pins

| pin | direction | function |
|---|---|---|
| `clk` | in | 50 MHz system clock (TinyTapeout supplies it) |
| `rst_n` | in | active-low reset, asynchronous inside the design |
| `ui_in[0]` | in | PDM microphone data |
| `ui_in[7:1]` | in | 7-bit threshold trim, 64 = neutral |
| `uo_out[0]` | out | PDM microphone clock, 1.5625 MHz |
| `uo_out[1]`, `uo_out[2]`, `uo_out[3]` | out | detect (three copies; `uo[3]` is the intended LED) |
| `uo_out[7:4]` | out | debug: current band-0 frame maximum, 4 bits |
| `uio_out[3:0]` | out | debug: frame index within the window |
| `uio_out[4]` | out | debug: detect |
| `uio_out[6:5]` | out | debug: FSM state (0 idle, 1 cascade, 2 classify, 3 roll) |
| `uio_out[7]` | out | debug: microphone tick |
| `uio_oe` | out | all ones (bidirectionals are outputs) |
| `ena`, `uio_in` | in | unused |

The debug outputs cost nothing measurable in area and are what makes the
gate-level simulation observable through ports (section 8.4).

### 2.2 Microphone timing

The chip divides `clk` by 32 with a free-running 5-bit counter `div`.
`uo_out[0]` is `div[4]`, so the mic clock is high for `div` = 16…31. The data
bit is sampled when `div` = 24, mid-way through the high phase, which is where
every common PDM MEMS microphone holds its data valid for the "left" channel
select. One internal *tick* is asserted at `div` = 1, and everything else in
the chip is paced by ticks.

### 2.3 Threshold trim

`trim = (ui_in[7:1] − 64) × 4`, added to the trained threshold of 13. All
switches off except the MSB (`1000000`) is neutral. Each step moves the
threshold by 4 score units; the score itself spans roughly −60…+60, so the
switch range covers everything from "fires constantly" to "never fires". This
is the only field-adjustable parameter and it exists because recall at a fixed
false-alarm rate was not measured before tape-out (section 10).

---

## 3. Signal chain

Everything is integer. There is no multiplier anywhere in the design.

```
PDM bit ─► ±128 ─► 9-stage dyadic 1-pole cascade ─► 5 octave band-pass
        ─► |x| ─► priority encoder (log2) + 1 mantissa bit ─► 4-bit level
        ─► max over 65 536 ticks (41.9 ms frame)
        ─► 4 ternary 16×5 templates, accumulated frame by frame, 6-bit saturating
        ─► clamp(acc >> 1, 0, 15) ─► ±1 output layer ─► > threshold ─► hold 16 frames
```

### 3.1 Input

A PDM bit of 1 becomes +128, a 0 becomes −128 (`IN_AMP = 2^(STATE_W−3)`).
This is the line that the netlist bug rectified (section 9.2).

### 3.2 Dyadic cascade

Nine one-pole low-pass stages. Stage *b* updates only on ticks where the low
*b* bits of the tick counter are zero, i.e. every 2^b ticks, so its cut-off
sits one octave below stage *b*−1:

```
state[b] += (in[b] − state[b]) >>> 2        (K_SHIFT = 2)
in[0]    = ±128
in[b]    = state[b−1]   for b ≥ 1
```

Each stage is a 10-bit signed register (`STATE_W = 10`). The corner frequency
of stage *b* is roughly f_pdm / (2^b · 2π · 4).

### 3.3 Band-pass and magnitude

The difference between the input and output of a stage is a one-octave
band-pass: `band[b] = in[b] − state_new[b]`. Five of the nine stages are used
as bands, `TAP0 = 4` to 8:

| band | stage | passband |
|---|---|---|
| 0 | 4 | 3 886 – 7 771 Hz |
| 1 | 5 | 1 943 – 3 886 Hz |
| 2 | 6 | 971 – 1 943 Hz |
| 3 | 7 | 486 – 971 Hz |
| 4 | 8 | 243 – 486 Hz |

Stages 0–3 exist only to decimate; stages above 8 were dropped from the
original 11-stage design because they carried no information for this word.

The magnitude is two's-complement absolute value, 10 bits.

### 3.4 Log level

A priority encoder finds the index of the most significant set bit of the
magnitude (`bexp`, 0 if zero). With `MANT = 1`, the bit below the leading one
is appended as a mantissa bit, so the level is `2·(bexp − 1) + mantissa`,
clamped to 0…15. This is a log2 in 3 dB steps, and the "computation" is just
the encoder's output wiring. The log is what makes the templates robust to
absolute volume: a 6 dB louder input adds 2 to every band level.

### 3.5 Framing

Each frame is 2^16 = 65 536 ticks = 41.94 ms. For each band the chip keeps
the maximum level seen during the frame (`fmax[b]`). At the frame boundary the
five maxima go to the classifier and are cleared. Max-over-frame, not
mean-over-frame, because it is free (one compare per update) and because
speech energy is bursty.

### 3.6 Classifier

Four hidden units. Each holds a 16-frame × 5-band template of ternary
weights. At every frame boundary, for each unit, the chip computes

```
dot = Σ_b  w[unit][frame][b] · (fmax[b] − FEAT_OFF)      FEAT_OFF = 6
acc[unit] = saturate_6bit(acc[unit] + dot)
```

The accumulator is reset to a per-unit constant (the trained bias, −4…+4) at
frame 0 of the window and saturated to −32…+31 **after every frame**, not only
at the end. Training models this per-frame saturation exactly; modelling it
only at the end inflated AUC by 25 points in an early version and was one of
the first corrections in the project.

At frame 15 the hidden activation is `h = clamp(acc >> 1, 0, 15)` (a ReLU and
a requantiser in one shift), the output layer is `score = h0 − h1 + h2 − h3`,
and `score > threshold + trim` raises the detect line.

### 3.7 Windows and hold

Two windows run staggered by 8 frames (`NPHASE = 2`, hop = 335 ms), so a word
that straddles one window boundary is centred in the other. Each window has
its own set of four accumulators. A detection sets a 16-frame hold counter,
so the LED stays on for 671 ms and repeated triggers within that time are
absorbed.

---

## 4. The learned pattern

From `src/ww_weights.svh`, rows are bands from high to low, columns are the
16 frames (42 ms each), `+` = +1, `−` = −1, `.` = 0:

```
unit 0   bias +1   output +1
   3.9–7.8 kHz  . - - - + + + + + + + + + . . -
   1.9–3.9 kHz  - - - . + + + + + + + + + + + +
  0.97–1.9 kHz  - . . . + + + . - - - - - - - .
   486–971 Hz   + + + . . - - - - - - - . - . +
   243–486 Hz   + + . - - - - - + + + + + + + +

unit 1   bias  0   output −1
   3.9–7.8 kHz  - - - - - - - - - . - - - + + +
   1.9–3.9 kHz  - . - - - - - . + + + . + . - .
  0.97–1.9 kHz  - - - . + + + + + + + + . - - -
   486–971 Hz   . + + + + + + + + + + . . - - .
   243–486 Hz   + + . . . - - . + . . . . + + +

unit 2   bias +1   output +1
   3.9–7.8 kHz  + + + + + + + + + . - - - - . -
   1.9–3.9 kHz  + + + + + + + + + . + - - . . .
  0.97–1.9 kHz  + + + + . . - - - - - + . . + +
   486–971 Hz   - - - - - - . . + + + . . + . +
   243–486 Hz   - - . . + + + + + + + . . + - -

unit 3   bias +4   output −1
   3.9–7.8 kHz  - - . . . + + . . . . . + . - .
   1.9–3.9 kHz  - . . . + + + + - - - - - - - -
  0.97–1.9 kHz  + + + + + + + + . - - - . . . -
   486–971 Hz   + + + + + + + + + . - - + . . +
   243–486 Hz   + + . . - . . - . - + . . . . .

threshold 13; 237 of 320 weights non-zero
```

Unit 2 is the shape of the word: about 350 ms of broadband energy above
2 kHz with a quiet bottom band (the `/ʃ/`), then the high bands fall silent
and 243–971 Hz comes up (the vowel and the `/l/`). Unit 0 wants the same
high-band burst later in the window, which is how the detector tolerates the
word starting at different moments. Units 1 and 3 enter negatively: they
fire on ordinary voiced speech with mid-band energy and no sibilant onset,
and suppress it. A single linear template cannot express "energy here **and
not** there"; this is exactly why the purely linear version of the design
capped at 84 % AUC and the hidden layer was added.

Why this word wins: of the 35 words in the corpus, "sheila" produces the
longest sustained energy above 2 kHz with the bottom band quiet. `six` and
`visual` come next for the same reason. Short voiced monosyllables (`down`,
`go`, `on`) are one low-band blob and are indistinguishable at 5 bands and
42 ms.

---

## 5. Hardware architecture

### 5.1 Time-multiplexing

The whole chip has one 10-bit subtract-shift-add for the cascade and one
five-term ternary adder tree for the classifier. Both are shared over time:

| state | when | clocks | what |
|---|---|---|---|
| `S_IDLE` | between ticks | ~22 | wait for the next tick |
| `S_CASC` | every tick | 9 | rotate the cascade ring once per stage, update due stages, update band maxima |
| `S_CLASS` | once per frame | 8 | one accumulator per clock: 2 windows × 4 units |
| `S_ROLL` | once per frame | 1 | clear band maxima, decrement hold, advance the tick counter |

A tick is 32 clocks, so the design is busy for 9 of them; the frame-end work
adds 9 clocks every 65 536 ticks. The datapath is idle ~70 % of the time.

### 5.2 Rotating rings instead of addressed arrays

Three state arrays are stored as shift registers that rotate past a single
compute position, so no read mux and no write decoder exist:

- **Cascade ring**, 9 × 10 bits. Each `S_CASC` clock rotates one position;
  the head is the stage being computed, the tail receives its new value. The
  previous stage's output is whatever the last rotation wrote to the tail,
  which removed a separate register that the first version carried.
- **Band maxima ring**, 5 × 4 bits. The five tap stages visit bands 0…4 in
  order every tick; the band being updated is always the head, the new
  maximum goes to the tail, and after five rotations the ring is back in band
  order for the classifier's parallel read.
- **Accumulator ring**, 8 × 6 bits. `S_CLASS` visits the eight (window, unit)
  slots in a fixed order every frame; the current slot is always the head.

The rotating cascade alone saved ~1 000 µm² over an addressed array in the
first version; the other two rings were added while fitting the tile.

### 5.3 Weight ROM

The 320 ternary weights are a 640-bit constant (`WW_ROW`), 2 bits per weight
(`01` = +1, `11` = −1, `00` = 0), indexed by `unit × 16 + frame`. Synthesis
turns the index into a 6-input logic function per output bit; zeros
simply drop their term from the adder tree, so sparsity is free area. The
four biases (`WW_HBIAS`, 6 bits each), the output weights (`WW_W2`) and the
threshold (`WW_THRESH_PK`, 10 bits) are constants in the same header.

### 5.4 Registers

| register | bits | purpose |
|---|---|---|
| `ring[9]` | 90 | cascade states |
| `hacc[8]` | 48 | accumulators, 2 windows × 4 units × 6 bits |
| `cnt` | 20 | tick counter: 16 bits of frame position + 4 bits of frame index |
| `fmax[5]` | 20 | band maxima |
| `osum` | 7 | output-layer running sum |
| `hold`, `div`, `stg`, `slot`, `st`, `pdm_bit` | 5+5+4+3+2+1 | sequencing |
| total | 205 | |

### 5.5 Reset

Asynchronous. sg13g2's only flip-flop type carries an async reset pin and
there is no reset-less variant, so a synchronous reset costs a tie-high cell on
every reset pin plus a mux in front of every D input: about 3 000 µm² here.
TinyTapeout deasserts `rst_n` synchronously to `clk`, so the async form is
safe.

### 5.6 Parameters

All of the above is parameterised in `tt_um_wakeword.sv`. Tape-out values:

| parameter | value | meaning |
|---|---|---|
| `PDM_DIV_LOG2` | 5 | mic clock = clk / 32 |
| `NSTAGE`, `K_SHIFT`, `STATE_W` | 9, 2, 10 | cascade depth, coefficient shift, state width |
| `TAP0`, `NBAND` | 4, 5 | first band stage, number of bands |
| `MANT`, `FEAT_W` | 1, 4 | log mantissa bits, level width |
| `FRAME_LOG2`, `NFRAME` | 16, 16 | ticks per frame (log2), frames per window |
| `NPHASE` | 2 | staggered windows |
| `NHID`, `HACC_W`, `HSHIFT`, `FEAT_OFF` | 4, 6, 1, 6 | hidden units, accumulator width, requantise shift, feature offset |
| `SCORE_W`, `HOLD_FRAMES` | 10, 16 | threshold width, LED hold |
| `DEBUG_PINS` | 1 | debug outputs enabled |

---

## 6. Training

### 6.1 Data

Google Speech Commands v0.02: 105 829 one-second clips of 35 words. The
official speaker-disjoint split is used, derived by hashing the speaker id
exactly as the dataset's `validation_list.txt` / `testing_list.txt` do.
Positives are every "sheila" clip (2 022) plus three time-jittered copies
(±60, 120, 180 ms) to teach alignment tolerance; negatives are 150 random
clips of each of the other 34 words. Clips are peak-normalised to 0.7 before
PDM encoding.

| split | clips | positives |
|---|---|---|
| train | 10 658 | 6 552 |
| val | 1 258 | 768 |
| test | 1 272 | 768 |

### 6.2 Bit-exact front end

`train/wwhw.py` implements the sigma-delta microphone model, the integer
cascade, the priority-encoder log and the frame maxima exactly as the RTL
does, and the cocotb testbench asserts that the RTL reproduces it frame for
frame. The front end is fixed silicon and learns nothing, so it is run once
over the dataset (`train/extract.py`, ~5 min) and the 24-frame × 5-band
feature maps are cached. Training then touches only the classifier.

### 6.3 Quantisation-aware training (`train/train_sheila.py`)

- **Model**: exactly the hardware classifier. Weights are real-valued during
  training and rounded to {−1, 0, +1} in the forward pass with a
  straight-through estimator. The accumulator is saturated to the 6-bit range
  **after every frame** with a straight-through gradient. The requantiser
  `clamp(acc >> 1, 0, 15)` uses a leaky STE (10 % gradient in the dead region)
  so units that die can recover.
- **Feature centring**: the training-set mean feature (6) is subtracted from
  every band level. In hardware this is the constant `FEAT_OFF`; it costs one
  constant subtract on a 4-bit value and it is what makes the hidden layer
  trainable at all (66 % AUC without it).
- **Bias initialisation**: each unit's bias starts at minus the median of its
  pre-activation over 4 096 training windows, so roughly half the units are
  alive from the first step.
- **Multiple-instance pooling**: a clip's label applies to the clip, not to a
  particular window, so the loss is on a pooled score over the window
  positions. Training warms up with mean pooling for 40 % of the epochs, then
  switches to log-sum-exp (soft max) pooling, which is what the chip's
  max-over-windows behaviour approximates.
- **Loss**: binary cross-entropy on the pooled score divided by a learned
  temperature. Adam, cosine schedule, 250 epochs, batch 1 024.
- **Selection**: the checkpoint with the best validation AUC within a run,
  and the seed with the best validation AUC across 6 seeds (seed 3). AUC is
  tie-aware (Mann-Whitney), because scores are small integers and a strict
  `>` comparison understates performance badly.
- **Export**: `--emit` writes `src/ww_weights.svh`. The bias folds into the
  accumulator's frame-0 reset value; the output-layer bias folds into the
  threshold. Both are exact, not approximations.

Command:

```
python train/train_sheila.py --tag sheila_hw --arch mlp --H 4 --WL 1 --shift 1 \
    --nphase 2 --accw 6 --epochs 250 --seed 3 --emit
```

### 6.4 Results

| model | test AUC |
|---|---|
| purely linear ternary template (first version) | 83.6 % |
| H=4, 7-bit accumulator | 89.3 % |
| **H=4, 6-bit accumulator (shipped)** | **89.1 %** |
| H=8, 8-bit accumulator (does not fit 1×1) | 91.6 % |
| fp32 MLP-32 on the same features (ceiling) | 94.1 % |

Seed spread for the shipped configuration: 87.9–90.6 % test AUC over 6 seeds.

### 6.5 Keyword choice

The front end is keyword-agnostic, so its output was extracted once for all
35 words and the exact hardware classifier trained for each. "sheila" wins
outright (89.1 %), then `six` (85.5 %), `visual` (83.9 %), `yes` (81.9 %);
`down` is last at 60.6 %. A confirmation run from a different seed base
reproduces the ordering. Full table in `artifacts/ww_sweep_exact.json`.

---

## 7. Physical implementation

### 7.1 Flow

TinyTapeout's flow, LibreLane 3.0.6 via `tt-gds-action@ttihp26b`, run locally
with `./harden_local.sh` in the `ghcr.io/librelane/librelane:3.0.6` image.
Configuration is the template's `src/config.json` with one change,
`PL_TARGET_DENSITY_PCT` raised from 60 to 85, plus the tile geometry
`tt_tool.py` generates (`src/user_config.json`).

### 7.2 Area

The yosys cell-area estimate used during design exploration said 78 % of the
tile. The flow adds cells the estimate never sees, and the first real run of
the design as originally specified reached **105.7 % utilisation** at global
placement. What it took to fit, all measured (`FINDINGS.md` §7):

| change | yosys µm² | flops | note |
|---|---:|---:|---|
| original RTL, 7-bit acc | 25 600 | 237 | 106 % in the flow |
| asynchronous reset, `prev_v` removed | 23 242 | 215 | −245 tie cells, −reset muxes |
| accumulator and band-maxima rings, narrower output sum | 21 927 | 212 | bit-exact |
| 6-bit accumulator (retrained) | **21 251** | **205** | −0.2 AUC points |

Final flow figures for the shipped RTL (`runs/wokwi6`):

| stage | µm² | of core |
|---|---:|---:|
| synthesis, 1 246 cells | 21 372 | 73.8 % |
| after fanout repair and clock tree | 22 976 | 79.4 % |
| after hold repair, 292 buffers | 27 744 | **95.9 %** |

The hold buffers are TinyTapeout's 0.1 ns hold margin plus the generic SDC's
0.25 ns clock uncertainty acting on every flop's Q→mux→D self-loop. They add
~17 µm² per flop and are not negotiable; budget ~73 µm² per flip-flop on this
flow, not the library's 49.

### 7.3 Signoff

| check | result |
|---|---|
| routing DRC (OpenROAD) | 0 |
| magic DRC | 0 |
| LVS | 0 |
| antenna | 0 violating nets |
| setup, slow corner 1.08 V 125 °C | 6.9 ns slack at 20 ns period |
| hold, fast corner 1.32 V −40 °C | +0.15 ns |
| lint (Verilator, in flow) | 0 warnings |
| max slew / max cap | 0 |
| max fanout | 14 informational warnings |
| power (flow estimate) | 0.76 mW |

There is no headroom left. Any change that adds flip-flops needs the
one-window fallback (`NPHASE=1`, 86 % AUC, ~2 000 µm² smaller).

---

## 8. Verification

### 8.1 Three independent models

1. `train/wwhw.py`: Python, integer, the reference. Used for training and
   as the golden model in every test.
2. `src/tt_um_wakeword.sv`: the RTL.
3. `runs/wokwi6/final/nl/tt_um_wakeword.nl.v`: the gate-level netlist the
   GDS was made from.

### 8.2 cocotb tests (`test/test.py`)

| test | what it asserts |
|---|---|
| `test_reset` | outputs clear after reset, mic clock toggles at the right rate |
| `test_frontend_bit_exact` | every frame's five band maxima equal the Python front end, exactly, on a synthetic multi-tone stimulus with the RTL's one-sample input latency modelled |
| `test_detector_matches_model` | the LED trace equals the Python detector fed the golden features, frame by frame, with the shipped weights parsed from `ww_weights.svh` so RTL and model share one source of truth |

Run at three levels, all passing on the shipped RTL:

| level | frames | wall time |
|---|---|---|
| RTL, `FRAME_LOG2=8` (256 ticks/frame) | 40 | 20 s |
| RTL, `FRAME_LOG2=16` (tape-out frame length) | 8 (40 also run) | 10 min (40 frames: 90 min) |
| gate-level netlist, `FRAME_LOG2=16` | 5 | 24 min |

### 8.3 RTL versus netlist

A side-by-side simulation drives the RTL and yosys's generic netlist of it
with the same PDM stream and compares all outputs every clock. 0 mismatches
over 40 frames (10 240 ticks). This is the test that found the bug in
section 9.2.

### 8.4 Gate-level specifics

- The IHP flow emits an unpowered netlist; the testbench instantiates it
  without `VPWR`/`VGND`.
- Internal signals do not survive synthesis, so in gate-level mode the tests
  read the FSM state from `uio_out[6:5]` and band 0 from `uo_out[7:4]`. The
  debug pins are what make this possible.
- Stock iverilog cannot compile `sg13g2_stdcell.v` (`ifnone` on edge-sensitive
  paths). TinyTapeout's CI uses its own patched build. Locally, a copy with
  the `specify` blocks stripped and the `delayed_*` nets tied through is
  passed as `GL_CELLS=`.
- No waveform dump at gate level: it wrote 87 GB before being stopped.

### 8.5 Continuous integration

`.github/workflows/test.yaml` runs the RTL suite at both frame lengths;
`gds.yaml` hardens, runs TinyTapeout's precheck, the gate-level test on the
produced netlist, and publishes a layout viewer.

---

## 9. Bugs and lessons

### 9.1 What the area estimate missed

Three effects, ~6 000 µm² together, none visible to yosys `stat`: tie cells
and reset muxes from a synchronous reset (section 5.5), hold buffers on every
flop (section 7.2), and ~125 fanout-repair buffers on the reset and FSM nets.
Raising `MAX_FANOUT_CONSTRAINT` in the config had no effect; the SDC wins.

### 9.2 The rectified microphone

The first gate-level simulation failed the front-end check while RTL passed.
Side-by-side simulation put the divergence at tick 0: for a microphone bit
of 0, RTL injected −128 and the netlist +128. The line was

```
x_in = pdm_bit ? STATE_W'(IN_AMP) : -STATE_W'(IN_AMP);
```

iverilog and the Python model read `-STATE_W'(IN_AMP)` as "negate the cast";
yosys reads it as a cast with a negated width and produces `+IN_AMP`. The
chip would have seen a rectified microphone, the filterbank fed DC, nothing
detected. **This was in the original design and would have shipped.** RTL
simulation, lint, the Python model and every area number agreed with each
other. Fixed by writing the two constants as signed localparams with no cast;
every other widening cast of a signed value was replaced with explicit sign
extension as a precaution. Lesson: run the gate-level test, and compare the
netlist against the RTL directly.

### 9.3 Earlier corrections (from FINDINGS.md)

- AUC computed with strict `>` understated performance on integer scores;
  now tie-aware.
- Saturating the accumulator only at window end instead of every frame
  inflated AUC by ~25 points at narrow widths.
- A purely linear template cannot pass ~84 % on these features; the hidden
  layer is not optional.
- `STATE_W=9` looked like a free 800 µm² and costs 2 AUC points alone, 7 with
  the 6-bit accumulator, because halving the input amplitude shifts every log
  level down by one and the accumulator then saturates. Rejected after
  measurement.

---

## 10. Limits and what is not established

- **Recall at a fixed false-alarm rate is unmeasured.** The test split has
  ~500 isolated-word negatives, which puts the floor at tens of false alarms
  per hour. Nobody has yet run hours of continuous speech, music and room
  noise through the model. The threshold trim exists for exactly this reason.
  The measurement can be done entirely in software, since the Python model is
  bit-exact with the netlist.
- **No automatic gain control.** The log levels give 3 dB resolution and the
  templates are differences, so moderate level changes cancel, but training
  data was peak-normalised. Distant or very quiet speech will sit below the
  levels the templates expect.
- **Single-word corpus.** A multi-syllable phrase with a sibilant onset
  ("hey sheila") would very likely beat every single word, but there is no
  data for it.
- **Fixed forever.** Weights, threshold base, frame length and band edges are
  silicon. Only the 7-bit trim is adjustable.
- **95.9 % utilisation.** It routed clean, but a future revision that adds
  logic starts from the one-window variant.

---

## 11. Reproducing

```bash
# dataset (2.3 GB, CC BY 4.0) into artifacts/data/speech_commands/
curl -O http://download.tensorflow.org/data/speech_commands_v0.02.tar.gz

# features (bit-exact front end, ~5 min) and training (~2 min per seed)
python train/extract.py --targets sheila --aug 4 --neg-per-word 150 \
    --nstage 9 --nband 5 --tap0 4 --state-w 10 --mant 1 --feat-w 4 --tag sheila_hw
python train/train_sheila.py --tag sheila_hw --arch mlp --H 4 --WL 1 --shift 1 \
    --nphase 2 --accw 6 --epochs 250 --seed 3 --emit     # -> src/ww_weights.svh

# verification
cd test && make                     # RTL, short frames, 20 s
FRAME_LOG2=16 make                  # RTL, tape-out frame length
GATES=yes make                      # gate-level; needs test/gate_level_netlist.v and PDK_ROOT

# area and physical flow
./area_check.sh                     # yosys estimate; add ~30 % for the flow
./harden_local.sh                   # LibreLane 3 in Docker -> runs/wokwi/final/{gds,nl,...}
```

## 12. File map

```
src/tt_um_wakeword.sv    the design
src/ww_weights.svh       generated weights, biases, output layer, threshold
src/config.json          LibreLane config (template + PL_TARGET_DENSITY_PCT 85)
info.yaml                TinyTapeout metadata and pinout
docs/info.md             datasheet rendered by TinyTapeout
docs/DESIGN.md           this document
docs/DRONE.md            the same RTL retargeted to drone detection (DADS); build with WEIGHTS=drone
test/                    cocotb testbench, Makefile (RTL and GL), tb.v
train/wwhw.py            bit-exact Python twin
train/wwdata.py          Speech Commands loader, official split
train/extract.py         feature extraction
train/train_sheila.py    QAT and header export
train/extract_all.py, sweep_words.py   35-word keyword sweep
area_check.sh            yosys area estimate
harden_local.sh          the TinyTapeout flow, locally
FINDINGS.md              the measurement trail, sections 1–7
TAPEOUT_PLAN.md          decisions taken on 2026-09-02 and why
```
