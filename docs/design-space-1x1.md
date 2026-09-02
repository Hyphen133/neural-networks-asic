# Neural network design space for a Tiny Tapeout IHP 1x1 tile

What is actually buildable in 202.08 × 154.98 µm of `ihp-sg13g2`, and five
candidate designs. Written as a follow-on to the streaming MNIST tile in `src/`.

> **Revised after measurement — see `docs/kws.md`.** One of the five designs
> below, the wake-word spotter in §4.3, has since been built and synthesised. It
> came out **2× larger than this document first estimated**, which exposed a bad
> rule of thumb: the original §1 costed combinational logic by guessing a cell
> count, and guessed low every time. §1 now carries a heuristic validated
> against nine synthesis runs across two independent designs, and §3 is
> recomputed with it. **Three of the five designs do not fit.** The two that do
> are the two originally ranked lowest.

---

## 1. The budget, measured

Everything below is derived from the real standard-cell library, not from rules of thumb.

| Quantity | Value | Source |
|---|---|---|
| Tile area | 202.08 × 154.98 µm = **31,318 µm²** | ttihp template |
| Usable cell area @ 60% density (OpenLane default) | **18,791 µm²** | 0.60 × tile |
| Usable cell area @ 80% density (sometimes achievable) | 25,054 µm² | 0.80 × tile |
| `sg13g2_dfrbp_1` (D flip-flop) | **52.62 µm²** | liberty |
| `sg13g2_dlhq_1` (latch) | 30.84 µm² | liberty |
| `sg13g2_nand2_1` | 7.26 µm² | liberty |
| `sg13g2_inv_1` | 5.44 µm² | liberty |
| `sg13g2_mux2_1` | 18.14 µm² | liberty |

Reproduce with `./area_check.sh`. The current tile, for calibration:

| config | cells | flops | area/µm² | tile util |
|---|---|---|---|---|
| H=8 | 829 | 104 | 12,430 | 39.7% |
| **H=16 (taped out)** | **998** | **138** | **15,688** | **50.1%** |
| H=32 | 1246 | 204 | 21,246 | 67.8% |

### The one number that matters

From the H=16 row: 138 flops × 52.62 = 7,261 µm², i.e. **46% of the whole design
is flip-flops**, and the remaining 860 combinational cells average 9.8 µm² each.

The temptation is to cost a new design as `flops × 52.6 + guessed_cells × 9.8`.
Do not: guessing the cell count is where the original version of this document
went wrong, by 2×. What actually holds steady is the *ratio*. Both designs
measured so far spend ~6.2 combinational cells per flip-flop, because both are
time-multiplexed datapaths whose logic is dominated by the muxes and decoders
that read and write their register arrays — an inherent cost of the
one-accumulator style, not a sign of sloppy arithmetic.

So flip-flops alone predict total area, across a 4× range and two unrelated
designs:

| design | flops | area µm² | µm² per flop |
|---|---|---|---|
| MNIST H=8 | 104 | 12,430 | 119.5 |
| MNIST H=16 | 138 | 15,688 | 113.7 |
| MNIST H=32 | 204 | 21,246 | 104.1 |
| KWS 4 bands × 4 slots | 276 | 31,438 | 113.9 |
| KWS 4 × 4, feat_w=5 | 292 | 33,184 | 113.6 |
| KWS 4 × 6 | 309 | 34,546 | 111.8 |
| KWS 6 × 4 | 361 | 38,868 | 107.7 |
| KWS 6 × 6 | 410 | 43,648 | 106.5 |
| KWS 7 × 6 | 444 | 45,669 | 102.9 |

```
area ≈ flops × 110 µm²        (±8% across all nine points)
budget = 18,800 µm² @60% density,  25,000 @80%
```

and therefore:

> **A 1x1 IHP tile holds about 170 flip-flops at OpenLane's default 60 %
> density, or 227 if 80 % can be made to route. That is ~170 bits of on-chip
> state, total — not the ~300 this document first claimed.**

A flip-flop plus its share of surrounding logic costs ~15 NAND2 gates.
Registers, not arithmetic, are what you run out of — but each one is three times
more expensive than its own cell area suggests.

---

## 2. The weight wall

There is no SRAM macro available at 1x1. Combined with the ~170 bits of register
state §1 turns out to allow, this eliminates most published tiny-NN
architectures outright: a 784→16 layer needs 12,544 weights, and even at 1 bit
each that is 70× the entire tile's storage.

Every viable design must therefore take one of three routes:

| Route | Storage cost | Consequence |
|---|---|---|
| **A. Stream weights from the host** | 0 bits | What `tt_um_mnist_nn` does. Arbitrary model size, but the chip is not self-contained. |
| **B. Generate weights from an LFSR** | 0 bits | Random projections, reservoirs, hyperdimensional item memories. Free, reproducible from a seed, but the weights cannot be *chosen*. |
| **C. Hold weights as combinational constants** | ~0.5–1 cell/bit | A few hundred bits before it costs more than flops would. Frozen at tapeout. |
| **D. Hold weights in flops** | ~110 µm²/bit all-in | ~170 bits maximum — so few that they may as well be **trainable on-chip**. |

The interesting corner of the design space is where **B and D combine**: let an LFSR
supply the large random layer for free, and spend the entire flop budget on a small
*learned* readout. That yields a chip that trains itself, which is qualitatively
different from every inference-only tile.

---

## 3. Scorecard

Recomputed with the validated `flops × 110` heuristic. The budget is 18,800 µm²
at 60 % density, 25,000 µm² at 80 %.

| # | Design | Flops | Area µm² | Util | Fits? | Novelty | Risk |
|---|---|---|---|---|---|---|---|
| 1 | HDC one-shot learner | ~292 | ~32.1k | 103 % | **no** — needs ~1.7× shrink | very high | medium |
| 2 | Reservoir + on-chip readout | ~296 | ~32.6k | 104 % | **no** — needs ~1.7× shrink | very high | medium |
| 3 | Wake-word spotter | **276** | **31.4k** | **100 %** | **no** — measured, and accuracy fails too | high | realised |
| 4 | Line-follower / steering regressor | ~130 | ~14.3k | 46 % | **yes**, comfortably | medium | low |
| 5 | Character-level RNN | ~130 | ~14.3k | 46 % | **yes**, but ROM-heavy logic may break the ratio | medium | low |

Row 3 is measured, not estimated. The other four are `flops × 110`, good to
about ±10 % on this evidence, and each still needs its own synthesis pass — but
the two 290-flop designs are over budget by enough that no amount of tuning
inside the current architecture rescues them. Both would need their state cut to
~170 flops: for #1 that means D × C ≤ 128 (a 64-bit hypervector with two
classes, or 32-bit with four), and for #2 a reservoir of ~16 neurons.

---

## 4. The designs

### 1. One-shot on-chip learning — hyperdimensional classifier

Hyperdimensional computing is the single best-fit ML paradigm for this area budget, and
is essentially unexplored on Tiny Tapeout. Everything is binary vectors, XOR and
popcount — no multipliers at all.

* **Encode.** Each input feature index is bound to a random D-bit hypervector produced
  on demand by an LFSR (route B — zero storage). Bind = XOR, bundle = bitwise majority.
* **Store.** The only persistent state is C class prototypes of D bits. **D × C ≤ 256.**
  Useful points: D=64/C=4, D=128/C=2, D=32/C=8.
* **Classify.** XOR query against each prototype, popcount, take the minimum Hamming
  distance. Time-multiplex the popcount 8 bits per cycle (popcount-8 ≈ 25 cells) rather
  than building a 64-bit tree; 32 cycles per classification at 50 MHz is 0.64 µs.
* **Learn.** Bundle the query into the prototype of the asserted label. **One example
  per class is enough** — no gradients, no epochs.

Budget: 256 prototype flops + 16-bit LFSR + ~20 FSM flops = 292 flops (15.4k µm²), plus
~280 logic cells (2.7k µm²) → **~18.1k µm², 58% util**.

Pins: 8 feature bits in, 3 label bits + 1 learn-strobe on `uio`, 2-3 class bits + valid out.

Demo: hold a label button, present the pattern once, the chip has learned it. Re-teach a
different mapping live. Risk: D=64 is a small hypervector — expect ~80–90% on cleanly
separated 4-class data, and characterise this in simulation before committing.

### 2. Reservoir computer with on-chip readout learning

Handles **time**, which no image-classifier tile does, and dodges the weight wall twice.

* **Reservoir.** N=24 neurons, 6-bit state (144 flops). Sparse recurrent connectivity —
  4 taps per neuron — with ternary weights drawn from a fixed-seed LFSR. Zero storage,
  bit-exactly reproducible in the Python model.
* **Readout.** 24 × 4 binary weights = 96 flops, the only trained parameters.
* **Learn.** Delta rule / sign-SGD, one update per input frame. Label supplied on `uio`.
* **Schedule.** 24 neurons × 4 taps = 96 MAC cycles per timestep. At 50 MHz against a
  1 kHz input frame rate there are 50,000 cycles available — ~500× headroom.

Budget: 144 + 96 + 24 (LFSR) + 12 (accumulator) + ~20 (FSM) = 296 flops (15.6k µm²),
plus ~300 logic cells (2.9k µm²) → **~18.5k µm², 59% util**.

Tasks: spoken-digit classification (FSDD), accelerometer gestures, next-value prediction
on a chaotic series. The last is the cleanest demo — the chip's prediction tracks a
Mackey-Glass or Lorenz signal on a scope after a few seconds of exposure.

Risk: reservoir dynamics are sensitive to spectral radius, which here is set by the LFSR
seed and tap count. The seed must be swept in the Python model and hard-coded once a good
one is found.

### 3. Wake-word spotter with no host at all — BUILT, AND IT DOES NOT WORK

> **This one has been implemented and measured; see `docs/kws.md` for the full
> report.** The estimate below said 15.9k µm² and 51 % utilisation; it
> synthesises to **31.4k µm², 100.4 %**. The accuracy guess of "~85–92 %" was
> also wrong: on Speech Commands v0.02 the tile reaches **1 % recall at a 5 %
> false-alarm rate**, and a float MLP with 128 hidden units on the same features
> does no better than the int4 linear one — so the ceiling is the 4-band, 6 dB,
> 85 ms representation, not the arithmetic. The front end *can* pick which of
> two words was spoken (77–85 %); what it cannot do is reject the other 33.
> The original estimate follows unedited, as a record of how it was wrong.

The highest-impact demo, and the tightest fit. Notable property: a PDM microphone is a
**one-bit stream on a single pin**, so the sensor interface is free.

* **Front end.** 6-band filterbank, second-order IIR per band, one datapath
  time-multiplexed across bands. 6 × 2 × 10-bit state = 120 flops.
* **Compression.** Envelope per band, then log via leading-zero count — free in gates.
  Decimate to ~32 frames/s at 4 bits per band.
* **History.** 6 bands × 4 bits × 6 frames = 144 flops. This is the expensive part and
  the reason the frame count is so low.
* **Classifier.** Binary-weight linear model over the 36-value window, weights held as
  combinational constants (route C, ~0 flops), scored with a popcount-style MAC, then
  max-over-time and a hysteresis threshold. 2 keywords.

Budget: 264 flops (13.9k µm²) + ~200 logic cells (2.0k µm²) → **~15.9k µm², 51% util**.

Pins: 1 PDM data + 1 PDM clock out, 2 detection outputs, band-energy debug readback.

Demo: say the word, an LED lights. Self-contained speech recognition in 0.031 mm².

Risk (**highest of the five**): the filterbank state dominates the budget, and a binary
linear model over 36 features is a weak classifier. Validate accuracy on a
Speech-Commands 2-keyword subset in Python *before* writing RTL — if it lands below ~85%
this design should be dropped rather than rescued.

### 4. Line-follower / steering regressor

The easiest to build and the only one that closes a physical control loop.

An 8×8 photodiode or cropped-camera frame is streamed in column by column, so no frame
buffer is needed: 8 column accumulators (10 bit, 80 flops) feed a 1D convolution with 8
ternary 5-tap kernels held as constants, then a linear head producing a 6-bit steering
value that drives a servo PWM directly from the tile.

Budget: ~130 flops (6.8k µm²) + ~400 logic cells (3.9k µm²) → **~10.7k µm², 34% util** —
by far the most headroom, so the kernel count or input width can grow.

Demo: the chip drives a robot along a line, with no microcontroller in the inference path.

### 5. Character-level RNN

The smallest interesting language model that fits. 27 symbols (a–z plus space), hidden
state of 8 units at 6 bits (48 flops). Because inputs are one-hot, the input weight
matrix is a *selected column*, not a multiply: 27×8 + 8×8 + 8×27 = 496 binary weights,
held as a constant mux-tree ROM (~250–500 cells).

Budget: ~130 flops + ~700 logic cells → **~13.7k µm², 44% util**.

Demo: type over UART, the chip predicts the next character. It will reliably learn
`q`→`u` and English letter statistics. Output quality is poor in absolute terms; the
point is the headline, and the honest framing is "a language model in 0.031 mm²".

---

## 5. Reframing the tile that already exists

`tt_um_mnist_nn` streams every weight from the host (route A), which means the silicon is
not an MNIST chip — it is a **general W4A4 784→H→10 streaming MAC engine**, and MNIST is
one configuration of it. The host can load Fashion-MNIST, KMNIST, EMNIST-letters, or any
custom 28×28 model with no RTL change and no re-tapeout.

That is already true today and is undersold in `info.yaml` and `docs/info.md`. It is the
cheapest available increase in perceived scope: documentation only.

---

## 6. Recommendation

Build **#1 (HDC one-shot learner)** as the flagship and **#3 (wake-word)** as the
crowd-pleaser, in that order of confidence. #1 has the better novelty-to-risk ratio: the
architecture is arithmetically trivial, the area estimate is dominated by a single
well-understood term (D × C prototype flops), and on-chip learning is the one capability
that separates a tile from the field. #3 is the better demo but carries real
accuracy risk that must be settled in Python first.

Suggested order of work for either:

1. Python model with bit-exact fixed-point semantics, matching the eventual RTL (as
   `train/hw.py` does today).
2. Accuracy characterisation across the D × C (or band × frame) trade, to pick the
   configuration *before* writing RTL.
3. RTL, with the same `chparam` sweep harness as `area_check.sh` so area is tracked from
   the first commit rather than discovered at the end.
4. Cocotb testbench cross-checking RTL against the Python model on the same vectors.

---

## Appendix: estimation caveats

* Only the H=8/16/32 rows in §1 are measured. Every other area figure is an estimate from
  `flops × 52.6 + cells × 9.8`, calibrated on one design, and should be read as ±25%.
* The conversion ignores routing congestion. A design that synthesises to 60% cell
  utilisation can still fail to route at 1x1, particularly with wide muxes or a high
  fan-out clock enable.
* Constant-weight ROM cost (route C) varies enormously with how well the constants
  minimise. Budget 1 cell/bit, and treat anything better as a bonus.
* Flop counts above exclude any UART/SPI interface logic, which the current design does
  not need because it uses a raw parallel handshake. Keep that handshake.
