# tt_um_kws — host-free wake-word tile: implementation and findings

A complete, verified implementation of the "PDM mic in, LED out" keyword spotter
from `docs/design-space-1x1.md` §4.3, plus the measurements that say it does not
work at the quality that idea was pitched at.

**Bottom line up front.** The design is built, bit-exact against its golden
model, and trained on real Speech Commands data. Two independent measurements
say it should not be taped out as a wake-word spotter:

| | pitched in the design doc | measured here |
|---|---|---|
| Cell area (smallest useful build) | 15.9k µm², **51 %** tile util | **31.4k µm², 100.4 %** |
| Flip-flops | ~264 | **276** (estimate was good) |
| Accuracy | "plausible ~85–92 %" on a 2-keyword task | **1 % recall at 5 % false alarms** |

The flip-flop estimate was fine. The *area* estimate was wrong by 2× because it
assumed ~200 cells of combinational logic where the design needs ~1,700, and
the accuracy estimate was optimistic by a wide margin. Both corrections are
folded back into `docs/design-space-1x1.md`.

---

## 1. What was built

Everything below runs and is checked in.

| File | What it is |
|---|---|
| `src/tt_um_kws.sv` | The tile. 497 lines, parameterised, lints clean. |
| `src/kws_weights.svh` | Generated constants: int4 weights, biases, filterbank microcode. |
| `train/kws_hw.py` | Bit-exact golden model, from PDM bits to LED. |
| `train/kws_data.py` | Speech Commands v0.02 pipeline, official speaker-stable split. |
| `train/kws_train.py` | Quantisation-aware training, int4 weights, integer features. |
| `train/kws_export.py` | Weight header + arithmetic-safety proofs. |
| `test/test_kws.py`, `test/tb_kws.v`, `test/Makefile.kws` | cocotb testbench, 5 tests. |
| `area_check_kws.sh` | Synthesis sweep against the real `sg13g2` library. |

### Signal chain

```
PDM mic ──1 bit @ 3.072 MHz──▶ count ones over 64 bits ──▶ x, 7-bit signed @ 48 kHz
   ──▶ 5 cascaded one-pole leaky integrators (shift-add only, no multiplier)
   ──▶ 4 bands as differences of adjacent taps
   ──▶ leaky peak envelope per band
   ──▶ log magnitude by leading-one position (4 bits)
   ──▶ max-pool into 4 time slots (85 ms each, 341 ms context)
   ──▶ int4 linear classifier over 16 features
   ──▶ argmax over {marvin, sheila, background} ──▶ hold ──▶ LED
```

`clk` is 12.288 MHz — 256 × 48 kHz, the standard audio clock, dividing cleanly
to a 3.072 MHz PDM bit clock that is within the maximum of every common MEMS PDM
microphone. There is no multiplier in the front end: each integrator coefficient
is a sum of two powers of two applied as two sequential shift-add passes, so the
effective pole is `1-(1-2^-a)(1-2^-b)`, not `2^-a + 2^-b`. Both the model and
the RTL derive their corner frequencies from the same function, so they cannot
be tuned apart.

### Pinout

| Pin | Direction | Function |
|---|---|---|
| `ui[0]` | in | PDM data |
| `ui[4:1]` | in | sensitivity trim (raises the background bias) |
| `ui[7:5]` | in | debug band select |
| `uo[1:0]` | out | keyword 0 / keyword 1 detected |
| `uo[2]` | out | any keyword |
| `uo[3]` | out | frame tick |
| `uo[7:4]` | out | live level of the selected band |
| `uio[0]` | out | PDM bit clock (3.072 MHz) |
| `uio[1]` | out | frame strobe |
| `uio[3:2]` | out | argmax class |
| `uio[4]` | out | PDM data echo (bring-up aid) |
| `uio[6:5]` | out | sticky envelope / accumulator saturation |
| `uio[7]` | out | 48 kHz sample strobe |

---

## 2. Verification

`cd test && make -f Makefile.kws` — 5 tests, all passing.

The load-bearing one is `test_matches_golden_model`: it drives the RTL with the
same PDM bit stream a microphone would produce, from a real Speech Commands
recording through the same second-order sigma-delta modulator used in training,
and asserts that the RTL's **per-frame argmax and detection-pin sequence equals
`train/kws_hw`'s, frame for frame**. 67 frames match exactly. A discrepancy
anywhere in the chain — decimator, filterbank, envelope, logarithm, pooling,
MACs, argmax, hold — changes that sequence, so one assertion covers all of it.

Simulation time is the binding constraint (a 1.5 s clip is 18 M clock edges), so
the tests use short audio with a small `FRAME_LOG2`, which *raises* the number
of classifier invocations per simulated second rather than lowering it.

### Bugs the process caught

Worth recording, because each was silent:

1. **Zero-width replication.** `{{(2-CIDX_W){1'b0}}, best_cls}` is illegal
   Verilog when `CIDX_W == 2`, and it quietly reshuffled the whole `uio_out`
   concatenation. Replaced with width casts.
2. **`(env >> DECAY) | 1` is not `max(env >> DECAY, 1)`.** For `env>>DECAY == 2`
   it gives 3. The envelope decayed faster than the model's.
3. **Sign extension.** `u` zero-extended `x` before shifting, destroying the
   sign of every negative sample.
4. **`iverilog -P` is silently ignored** by Icarus 13.0 unless `-s` is also
   given, and errors out when it is. A build whose parameters never took effect
   would compare the RTL against a differently configured golden model and
   *pass*. Overrides now go through `-D` macros, and `test_reset_and_clocks`
   reads back the elaborated parameters and asserts they match the model.
   **This affects `test/Makefile` for the existing MNIST design too — its
   `HIDDEN`, `ACC_W`, `MULT_TERNARY` and `SHIFT_FIXED` overrides have never
   taken effect.**
5. **Trim step 64× too coarse.** `TRIM_SHIFT=6` adds 128 per trim step against
   trained score margins of ~13, so the chip went from trigger-happy at trim=1
   to permanently deaf at trim=2. Now 1, which spans 13 % → 0.2 % false alarms
   across the nibble.
6. **The chip announced "sheila" into a silent room.** Trained biases came out
   `[5, 6, -7]`, so an all-zero feature vector — what the tile sees once every
   envelope has decayed in a quiet room — picked a keyword. All-zero frames
   never occur in the dataset, even in its "silence" clips, so the biases were
   unconstrained there. Training now includes an explicit quiet-room anchor;
   biases came out `[-25, -22, +24]`. Caught by
   `test_silence_stays_background`.

---

## 3. Area, measured

`./area_check_kws.sh`, yosys against `ihp-sg13g2` typ 1.20 V 25 °C. Tile is
31,318 µm²; usable cell area is ~18,800 µm² at OpenLane's default 60 % density,
~25,000 µm² at 80 %.

| bands × slots | feat_w | cells | flops | area µm² | tile util |
|---|---|---|---|---|---|
| **4 × 4** | 4 | ~1,980 | **276** | **31,438** | **100.4 %** |
| 4 × 4 | 5 | | 292 | 33,184 | 106.0 % |
| 4 × 6 | 4 | | 309 | 34,546 | 110.3 % |
| 6 × 4 | 4 | | 361 | 38,868 | 124.1 % |
| 6 × 6 | 4 | | 410 | 43,648 | 139.4 % |
| 7 × 6 | 4 | | 444 | 45,669 | 145.8 % |

**Even the smallest useful configuration is at 100 % tile utilisation** — 1.7×
over the 60 % budget and 1.25× over the optimistic 80 % one.

Flip-flops account for only 46 % of it (276 × 48.99 µm² = 13.5k). The other
18k µm² is combinational, and it is *not* wasted arithmetic — tightening every
intermediate from a lazy 32 bits to its exact width moved the total by 1 %
(31,803 → 31,438 µm²), because yosys had already dead-coded the upper bits. It
is the **multiplexing into and out of the indexed register arrays**: a
time-multiplexed datapath that reads `y[bi]`, `env[bi]`, `slot[s][b]` and a
48-entry weight ROM by variable index pays for a read mux and a write decoder on
every array.

The known fix is to make `y`, `env` and the slot register **rotating shift
registers** instead of indexed arrays, so the active element is always at a
fixed position. That should recover ~5–6k µm² (to ~81 % util) and is the obvious
next step if this design is continued — but see §4 before spending it.

---

## 4. Accuracy, measured

Google Speech Commands v0.02, the dataset's own speaker-stable hash split
(11,124 train / 1,511 val / 1,549 test clips), keywords `marvin` and `sheila`,
background drawn from all 33 other words plus ambience-only clips. Features come
from the **exact** microphone model — second-order sigma-delta at 3.072 MHz —
not an idealised ADC; the cheap ADC stand-in sits 3.7 dB high with errors up to
3 log steps, enough to mistune every band, so it is used only to quantify that
difference (`kws_data.py --verify-pdm`).

### The open-set task fails

"Say the keyword, the LED lights, and it stays dark for anything else" needs the
tile to reject 33 other spoken words. Test-set operating curve for the
4 bands × 4 slots build (the one that comes closest to fitting):

| clip false-alarm rate | recall, marvin / sheila |
|---|---|
| 1 % | 0.5 % / 0.5 % |
| 5 % | 1.0 % / 5.1 % |
| 10 % | 1.4 % / 13.6 % |
| 20 % | 1.9 % / 29.0 % |

At 5 % false alarms the LED lights for about one keyword in fifty. That is not a
demo.

### It is not the quantisation, and not the classifier

This was the first hypothesis and it is wrong. Replacing the int4 linear
classifier with a **float MLP of 64 or 128 hidden units** — which would never
fit — does not rescue it:

| model on the same features | 3-class accuracy | background recall |
|---|---|---|
| int4 linear | 33 % | 21 % |
| float MLP, 32 hidden | 50 % | 41 % |
| float MLP, 128 hidden | 56 % | 47 % |

Best open-set recall at 5 % false alarms, across every configuration and model
tried including ones far too large to fit: **35.6 %**. The ceiling is the
representation, not the arithmetic.

Two further hypotheses, both tested and both negative:

* **Level normalisation.** Background clips are *louder* than the keywords
  (mean log feature 10.6 vs 10.0) and all features sit in a narrow band around
  9–14, so the model looked like it was reading loudness rather than spectral
  shape. Subtracting the per-frame loudest band — free in the log domain —
  changed 3-class accuracy from 55.7 % to 55.4 %.
* **Persistence.** Requiring 2 consecutive keyword frames before firing, the
  standard false-alarm fix, drops recall to **zero**: a keyword clip wins
  exactly one frame, because at 85 ms max-pooled frames the template matches at
  a single alignment.

### Why: the information rate

4–6 octave-wide bands, 6 dB log resolution, 85 ms max-pooled frames — about 16
integers describing 341 ms of speech. A working keyword spotter uses ~40 mel
bands, 25 ms windows, 10 ms hop and 8-bit resolution: roughly 100× more feature
volume. Max-pooling a 10 ms peak-follower over 85 ms discards nearly all
temporal structure, which is exactly what distinguishes one word from another.

### What the front end *can* do

The two keywords are separable **from each other**; what fails is open-set
rejection. Given that a clip contains one of the two words, the int4 linear
classifier picks the right one:

| build | closed-set 2-word accuracy | fits? |
|---|---|---|
| 4 bands × 4 slots (16 feats) | 77.5 % | no — 100 % util |
| 6 bands × 4 slots (24 feats) | 78.8 % | no — 124 % util |
| 6 bands × 6 slots (36 feats) | **85.2 %** | no — 139 % util |

So a "which of two words did I just hear" tile is a real result — but the
version that reaches 85 % is 1.4× too big, and the one nearest to fitting is
still at 100 % utilisation.

---

## 5. Where that leaves it

The concept needs one of these, and the choice is a scope decision rather than
an engineering detail:

1. **Change the task to sound-event classification.** Clap / whistle / speech /
   quiet are separated by exactly the gross spectral energy this front end
   measures well, unlike two English words. Reuses the entire chain — RTL,
   golden model, testbench, training — with a different dataset, and is the
   change most likely to produce a working, genuinely fun demo on 1x1.
2. **Spend a 2x1 tile.** 62,636 µm² buys the ~40 k µm² the 6 × 6 build needs
   (85 % closed-set, and a real shot at open-set with a hidden layer). The area
   optimisation in §3 plus one more tile is the honest cost of the original
   pitch.
3. **Do the shift-register rewrite and ship the closed-set demo on 1x1** at
   ~77 %, ~81 % utilisation. Fits, works, undersells the pitch.
4. **Stop here** and pick a different idea from `docs/design-space-1x1.md` —
   noting that its area estimates are now corrected, and only the two
   lowest-ranked designs fit.

## Appendix: reproducing

```bash
python3 train/kws_data.py --prepare          # ~15 min, downloads 2.4 GB
python3 train/kws_data.py --verify-pdm       # mic model vs ideal ADC
python3 train/kws_data.py --verify-derive    # feature derivation is bit-exact
python3 train/kws_train.py --sweep           # configuration sweep
python3 train/kws_train.py --config 5,4,12,4 --epochs 150 --tag fit
python3 train/kws_export.py --tag fit        # writes src/kws_weights.svh
./area_check_kws.sh                          # synthesis sweep
cd test && make -f Makefile.kws              # 5 tests
```
