# "Sheila" wake-word spotter on a 1×1 tile

**Shipped: 24 643 µm² (78.7 % tile utilisation), 89.3 % test AUC, verified
bit-exact against the golden model.** PDM microphone in, LED out, no host, no
memory, no weights to load.

| | |
|---|---|
| keyword | "sheila" (Google Speech Commands v0.02, CC BY 4.0) |
| test AUC | **89.3 %** (tie-aware, speaker-disjoint split) |
| area | 24 643 µm² of 31 318 µm² = **78.7 %** |
| flip-flops | 225 |
| latency | one decision every 336 ms (2 staggered 671 ms windows) |
| verification | 3 cocotb tests pass; front end bit-exact frame-for-frame |

`tiles: "1x1"` — but 78.7 % is above OpenLane's 60 % default density, so
`FP_CORE_UTIL` has to be raised. The ttihp template notes ~80 % has routed.
**That is the one real risk in this design.** Dropping to `STATE_W=9` gives
73.1 %; the linear-template variant gives 62.2 % at 83.6 % AUC.

## How it got there

The first attempt — the architecture as originally specified — neither fit
(127 % of the tile) nor worked (~0 % recall). Sections 1–5 below are the
measurement trail that produced the shipped design; they are kept because the
negative results are the reason the final configuration looks the way it does.
The four changes that mattered:

1. **A rotating-ring cascade** instead of an addressed state array. Removes the
   N:1 read mux and the 1:N write demux. −1 000 µm².
2. **One hidden layer.** A purely linear ternary template caps at ~84 % AUC on
   these features no matter how the weights are quantised. Four hidden units
   reach ~90 %. Hidden pre-activations accumulate frame by frame just like the
   linear score, so the staggered-accumulator hardware is unchanged, just wider.
3. **A constant feature offset (`FEAT_OFF`).** Subtracting a constant from each
   band feature before the adder tree is nearly free in silicon, and it is what
   makes the hidden layer trainable at all — without it accuracy sits at 66 %.
4. **Training that matches the RTL exactly**, including *incremental*
   accumulator saturation. Saturating only the final sum — which is what the
   first version did — inflated measured AUC by ~25 points at narrow widths.

### Corrections made along the way

Two of my own earlier numbers in this file were wrong and are superseded:

- AUC was computed with a strict `>`, so ties between small integer scores all
  counted as losses. Now tie-aware (Mann-Whitney).
- Accumulator saturation was modelled once at the end rather than per frame.
  With the correct model, "H=8, 6-bit accumulator, 92.4 % AUC" collapses to
  50.6 %; the honest figures are in the table below.

### Accuracy vs. area, exact hardware semantics

| configuration | test AUC | area µm² | util |
|---|---:|---:|---:|
| linear ternary template | 83.6 % | 19 469 | 62.2 % |
| **H=4, 7-bit accumulator** ← shipped | **89.3 %** | **24 643** | **78.7 %** |
| H=4, 7-bit acc, `STATE_W=9` | (untested) | 22 888 | 73.1 % |
| H=8, 8-bit accumulator | 91.6 % | 29 928 | 95.6 % |

The reduced front end costs almost nothing: `STATE_W` 15→10, `NSTAGE` 11→9,
`NBAND` 8→5, `FEAT_W` 5→4 gives 92.34 % vs 92.78 % for the rich one at H=8.

### What is still weak

Recall at a low false-alarm rate is **not** established. The test split has only
504 negatives, so the smallest measurable false-alarm rate is ~42/hour; every
figure at 1 FA/hour sits below that floor. Measuring a real operating point
needs hours of continuous negative audio (the `_background_noise_` files sliced
up), which has not been done. AUC 89.3 % is a genuine number; "it will light up
when you say sheila and rarely otherwise" is not yet evidence-backed.

---

---

## 1. It does not fit

Synthesised against the real IHP `sg13g2` liberty. The 1×1 tile is 31 318 µm²;
OpenLane's default placement density is 60 %, so the working budget is
~18 800 µm².

| configuration | cells | flops | area µm² | tile util |
|---|---:|---:|---:|---:|
| **as specified** (8 band, 2 keyword, 4 phase) | 2683 | 349 | 39 863 | **127 %** |
| 1 keyword | 2303 | 296 | 34 661 | 111 % |
| 2 phases | 2524 | 300 | 36 230 | 116 % |
| 1 keyword, 2 phases | 2281 | 271 | 33 253 | 106 % |
| 6 bands, 1 keyword, 2 phases | 2171 | 261 | 31 403 | 100 % |
| 12-bit state, 1 keyword, 2 phases | 2038 | 238 | 29 461 | 94 % |

Stripping every parameter to the point of uselessness still lands at 94 % of
the whole tile — about **1.6× over the density budget**. This is not a tuning
problem.

Where it goes (default config): 349 flops = 17 097 µm² (43 %), combinational
22 766 µm².

```
165  11 cascade stages x 15-bit state     <- the dominant cost
 96  4 phases x 2 words x 12-bit accumulators
 40  8 band maxima x 5-bit log feature
 48  counters, FSM, LED hold
```

The combinational half is dominated by two variable-index reads into the
11×15 stage array (`stage[stg]` and `stage[stg-1]`), the 512-bit weight-row
mux, and the priority encoder.

The "~200 flops" estimate in the original plan was close (349 actual), but
200 flops was never going to be affordable either: at 48.99 µm² per
`sg13g2_dfrbpq_1`, 200 flops alone is 9 800 µm², over half the budget, before
a single gate of logic.

## 2. It does not detect

Google Speech Commands v0.02, speaker-disjoint official split, 18 306 clips.
Metric is what matters for a wake word — recall at a false-alarm rate — not
accuracy. The chip evaluates 21 458 windows/hour (one every 168 ms).

| variant | marvin AUC | sheila AUC | recall @ 1 FA/hour |
|---|---:|---:|---:|
| ternary template, hard labels | 69.1 % | 84.3 % | ~0 % |
| ternary template, KD from a log-mel CNN teacher | 70.3 % | 80.6 % | ~0 % |

Recall at every usable operating point is zero. As a wake-word detector this
does not work.

## 3. The cause: linearity, not bits and not the front end

This is the useful part. Linear probes on the **identical** cached hardware
features:

| model on the same 24×8 hardware features | marvin AUC | sheila AUC |
|---|---:|---:|
| ternary linear template (what the chip does) | 69.1 % | 84.3 % |
| **fp32 linear** on the same features | 76.6 % | 83.7 % |
| **fp32 MLP-64** on the same features | **91.9 %** | **97.4 %** |

The front end is good: the information needed for 92–97 % AUC is present in
the 8-band log-magnitude maps the silicon produces. Ternary quantisation costs
little — fp32 linear is only ~7 points above ternary linear. **The wall is that
the classifier is linear.**

Things tried, all of which stay linear, all of which plateau:

| change | marvin | sheila |
|---|---:|---:|
| raw features | 76.6 % | 83.7 % |
| per-frame band-mean removed (1 adder + shift) | 75.3 % | 80.3 % |
| + temporal delta | 76.7 % | 83.7 % |
| clip-mean removed (global AGC) | 76.0 % | 81.3 % |
| max over 2 ternary templates | 70.9 % | 84.5 % |
| max over 4 | 70.6 % | 85.3 % |
| max over 8 | 71.1 % | 84.9 % |
| max over 16 | 71.2 % | 85.0 % |

Max-over-templates is a *convex* piecewise-linear function, which is why it
buys almost nothing: it cannot express the "energy here **and not** there"
interactions the MLP uses.

**Distillation cannot fix this.** KD moved marvin +1.2 and sheila −3.7 points —
noise. Soft targets help a student that is short of *supervision*; this student
is short of *function class*. That is a clean negative result, not a failed
experiment.

## 4. What would actually work

One hidden layer. Hidden pre-activations decompose over frames exactly the way
the linear score does, so the staggered-accumulator hardware is unchanged —
just wider: `NPHASE × H` accumulators instead of `NPHASE × NWORD`.

The cost is the problem. H=8 at NPHASE=4 is 32 accumulators ≈ 384 flops of
accumulator alone, on top of a cascade that already does not fit. Fixing the
accuracy makes the area worse.

A quick two-layer ternary run trained poorly (AUC 47–79 %, unstable across
shifts). That is **not** evidence the approach fails — it needs the per-layer
shift calibration the MNIST design uses, which was not done here. It is an
open thread, not a conclusion.

### Recommendation

- **2×1 or 2×2 tiles.** Roughly 2.5× the area makes the specified design fit
  with room for the hidden layer that gets it to ~92 % AUC. This is the honest
  ask for self-contained speech on this process.
- **Or keep 1×1 and change the demo** to something whose decision function is
  genuinely linear — a clap/knock pattern, a whistle-pitch detector, a
  loudness-envelope trigger. Those work on this exact front end.
- **Or keep 1×1 and keep going:** trim the cascade (a CIC decimator in place of
  stages 0–2 saves ~37 flops), drop to one keyword, and spend the recovered
  budget on H=4 hidden units with properly calibrated shifts. My estimate is
  this still lands ~85–95 % of the tile — tight, and the accuracy is unproven.

## Reproducing

```bash
# dataset (2.3 GB, CC BY 4.0)
curl -O http://download.tensorflow.org/data/speech_commands_v0.02.tar.gz

python train/extract.py  --targets marvin sheila --aug 3   # ~8 min
python train/train_ww.py --tag marvin_sheila --kd 0.0      # baseline
python train/train_ww.py --tag marvin_sheila --kd 0.7      # distilled
cd test && make                                            # 3 tests
./area_check.sh
```

## What is in here regardless

- `src/tt_um_wakeword.sv` — complete, parameterised, lint-clean under
  `verilator -Wall`. PDM mic in, LED out, no host.
- `train/wwhw.py` — bit-exact software twin: sigma-delta mic model, integer
  dyadic cascade, priority-encoder log, ternary template, streaming detector.
- `train/wwdata.py` — Speech Commands loader honouring the official
  speaker-disjoint split.
- `train/extract.py`, `train/train_ww.py` — feature caching, MIL training,
  optional log-mel CNN teacher, wake-word metrics, weight-header emission.
- `test/` — 3 cocotb tests. The front end matches the golden model **frame-for-frame,
  bit-exact**, including the one-mic-tick input-register latency.


---

## 5. Option sweep (answering "test options" and "you can quantize weights")

### 5a. Area — a 1×1-fitting configuration exists

Budget is 18 791 µm² (60 % of the 31 318 µm² tile). Cumulative cuts:

| configuration | flops | area µm² | tile util |
|---|---:|---:|---:|
| as specified | 349 | 39 861 | 127 % |
| `NWORD=1` | 296 | 34 661 | 111 % |
| ` + NPHASE=2` | 271 | 33 253 | 106 % |
| ` + STATE_W=12` | 238 | 29 461 | 94 % |
| ` + STATE_W=10` | 216 | 26 735 | 85 % |
| ` + NSTAGE=9, NBAND=6` | 186 | 22 897 | 73 % |
| ` + MANT=0, FEAT_W=4` | 180 | 21 410 | 68 % |
| ` + SCORE_W=10` | 176 | 21 150 | 68 % |
| **`NSTAGE=8 NBAND=5 STATE_W=9 SCORE_W=9`** | **151** | **18 228** | **58 %** ✅ |

State width is the dominant lever: the cascade is `NSTAGE × STATE_W` flops and
nothing else comes close.

### 5b. Weight precision buys nothing

A hard-wired constant weight costs **zero flip-flops** at any precision — only
adder-tree area — so this was worth testing properly. It does not help:

| linear template weights | marvin AUC | sheila AUC |
|---|---:|---:|
| ternary {−1,0,+1} | 63.5 % | 83.3 % |
| int3 {−3…3} | 66.7 % | 84.9 % |
| int4 {−7…7} | 66.7 % | 84.9 % |
| int5 {−15…15} | 66.7 % | 84.9 % |

Flat from int3 upward. The bottleneck is not how finely the weights are
quantised — consistent with §3, where fp32 *linear* also caps out near 76 %.

### 5c. Everything measured, in one place

| option | marvin | sheila | fits 1×1? |
|---|---:|---:|---|
| linear ternary (as specified) | 63.5 % | 83.3 % | only when stripped to 58 % util |
| linear, int3…int5 constants | 66.7 % | 84.9 % | same |
| max over 2 / 4 / 8 / 16 templates | ~71 % | ~85 % | no — M× accumulators |
| 2-layer H=8 ternary, shift 2 | 66.6 % | 81.0 % | no |
| **2-layer H=16 ternary, shift 3** | **71.0 %** | 77.9 % | no |
| 2-layer H=8, fp32 first layer | 51.8 % | 84.8 % | — |
| *fp32 MLP-64 on the same features* | *91.9 %* | *97.4 %* | — |

### 5d. The open item: my two-layer training collapses

The two-layer runs are **not** a verdict on the architecture. Evidence:

- fp32 weights with a plain ReLU fail identically (50 % AUC, output pinned to
  the bias), so it is not quantisation.
- Hidden units die: only 11–37 % are ever non-zero after training, and the
  score range collapses to a band as narrow as [−2, −2].
- Median-bias initialisation and a leaky STE recovered part of it
  (50 % → 71 %), which is the signature of a dead-unit optimisation problem,
  not a capacity ceiling.
- The same feature set supports 91.9 % / 97.4 % under a plain MLP-64 trained on
  standardised, non-pooled features.

The difference between the working probe and the failing hardware model is the
combination of **raw un-normalised features** (mean ≈ 15) and **max-over-window
MIL pooling**. That is a conditioning problem with known fixes (normalise the
pre-activation during training and fold the scale into the constant bias at
export; warm up with mean pooling before switching to max). I have not done
that work, so the two-layer numbers above should be read as a floor, not a
result.

---

## 6. Is "sheila" the right keyword? (35-word sweep)

Yes — it wins the whole corpus, on the shipped hardware model and on both
float probes. The front end carries no notion of a keyword, so its output is
extracted **once** over all 35 Speech Commands words (1 500 clips/word,
52 500 clips, official speaker-disjoint hash split) and then re-labelled per
candidate: `train/extract_all.py`, then `train/sweep_words.py`.

Exact shipped semantics (H=4 ternary, 7-bit accumulator saturated *every*
frame, `clamp(acc>>1, 0, 15)`, ternary output layer, 2 staggered 16-frame
windows), 250 epochs, best-of-3 restarts picked on val, tie-aware AUC on clean
test clips:

| rank | word | hw AUC | fp32 linear | fp32 MLP-32 |
|---:|---|---:|---:|---:|
| 1 | **sheila** | **89.1 %** | **82.6 %** | **94.1 %** |
| 2 | six | 85.5 % | 79.0 % | 94.0 % |
| 3 | visual | 83.9 % | 77.6 % | 91.1 % |
| 4 | yes | 81.9 % | 75.3 % | 89.3 % |
| 5 | backward | 81.0 % | 58.9 % | 89.1 % |
| 6 | house | 79.7 % | 70.7 % | 89.6 % |
| … | marvin | 72.6 % | 68.0 % | 82.1 % |
| 35 | down | 60.6 % | 59.3 % | 71.2 % |

Full table in `artifacts/ww_sweep_{screen,exact}.json`. A confirmation run with
6 restarts from a different seed base reproduces the ordering (sheila 88.7 %,
six 87.2 %, yes 84.6 %, visual 82.6 %, marvin 74.4 %), so the top of the list
is not seed luck. With ~150 test positives per word the sampling error on one
AUC is ~2.5 points: sheila vs. six is a real but narrow lead, sheila vs.
everything else is not close.

Why these words: the front end is five log-magnitude band envelopes at 41.9 ms
resolution over a 671 ms window, so a keyword is only visible as a
*spectral-energy trajectory*. `sheila` opens with a broadband `/ʃ/` — energy in
the top bands that almost nothing else in the corpus produces — then moves to
a front vowel and an `/l/`, filling most of the window. `six` wins for the same
reason (`/s/…/ks/`), `visual` for its two fricatives. The losers are short
voiced monosyllables (`down`, `on`, `go`, `bird`, `bed`): one vowel blob in the
low bands, and every other word looks the same.

Two things this does **not** say. Recall at a usable false-alarm rate is still
unmeasured — 5 264 test clips put the floor at ~2 FA/hour, and the sweep's
`R@5FA` column is at most 6 % for every word, which is the measurement floor
talking as much as the model (§"What is still weak"). And the corpus caps the
question at 35 single words; a purpose-recorded multi-syllable phrase with a
sibilant onset ("hey sheila") should beat everything here, but there is no data
for it.

**Conclusion: keep `sheila`.** The keyword is not the lever — it is already
optimal for this corpus. The lever is classifier capacity: H=8 reaches 91.6 %
AUC but needs 95.6 % tile utilisation, i.e. 2×1 tiles.

Reproducing:

```bash
python train/extract_all.py  --per-word 1500 --jobs 10   # ~3 min
python train/sweep_words.py  --mode screen --epochs 60 --jobs 10
python train/sweep_words.py  --mode exact --epochs 250 --seeds 3 --jobs 10
```
