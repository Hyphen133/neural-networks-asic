# Pushing the eight detectors toward 90 %

A 20-round search. Each round states one hypothesis, runs the smallest
experiment that can refute it, and puts the winning design through
`train/optim/area_gate.py` — a candidate that does not fit the TinyTapeout 1×1
IHP tile is not an improvement, whatever it scores.

Companion to [nn_optimization.md](nn_optimization.md), which did this for
`sheila` and `drone`, and to [new_tasks.md](new_tasks.md), which built the eight
detectors this document tries to improve.

## Target and start state

The target is **90 % test AUC**. Every task starts below it:

| task | chip AUC at round 0 |
|---|---:|
| `catmeow` | 79.74 |
| `siren` | 76.24 |
| `dogbark` | 75.74 |
| `babycry` | 72.36 |
| `clap` | 72.32 |
| `water` | 68.92 |
| `vad` | 63.72 |
| `mosquito` | 62.96 |

All eight share one geometry — the one that hardened as `runs/sheila_nb6`:
`NSTAGE=9, TAP0=3, NBAND=6, K_SHIFT=2, STATE_W=10, MANT=1, FEAT_W=4,
FRAME_LOG2=16, NFRAME=8, NPHASE=2, NHID=4, HACC_W=6, HSHIFT=1, SCORE_W=10`,
ternary weights, and each is a separate build selected by a weight-header
`ifdef`, exactly as `sheila` and `drone` already are. So each detector gets the
whole tile to itself and may pick its own geometry.

**Area budget: 22 150 µm² synthesised** (`area_gate.py` derives it from the
95.9 % core utilisation the shipped part hardens at; see
[nn_optimization.md](nn_optimization.md) §3).

## Rules

* Selection is on **validation only**. Test AUC is recorded and never used to
  choose a configuration.
* Every reported design is gated for area at its own geometry. `area_gate.py`
  pushes its whole `DEFAULTS` dict through `chparam`, which overrides the
  per-build `ifdef` in `src/tt_um_wakeword.sv`, so every call passes `--set` for
  the geometry actually being measured.
* Results append to `artifacts/optim/probe.jsonl` (headroom),
  `artifacts/optim/area.jsonl` (fit) and `artifacts/optim/new_tasks.jsonl`
  (finalised detectors).

---

## Round 1 — where is the headroom, and does the current design fit?

**Hypothesis.** Two things are unknown before anything can be optimised: (a)
whether the eight emitted headers fit the tile at all — `new_tasks.md` §6 says
the gate was never run on them; and (b) how much of the gap to 90 % is the
*classifier* and how much is the *front end*. The `ceiling_auc` numbers already
recorded cannot answer (b): `train/ceiling_probe.py` trains one MLP-64 for a
fixed 30 epochs, reports it at the **final** epoch with no validation
selection, and on two tasks the ternary chip model beats it — a ceiling that
sits below the floor is not measuring capacity, it is measuring overfitting.

**Experiment.**

* `train/optim/area_gate.py` on each of the eight real headers, at
  `TAP0=3 NBAND=6 NFRAME=8`.
* A new `train/optim/probe.py`: a capacity ladder (bare linear → MLP-256) on
  the same cached features and the same multiple-instance pooling, every rung
  **selected on validation** and reported at that epoch, averaged over 3 seeds.
  The linear rung matters on its own — the ternary template is a linear form
  over the same features, so it brackets the design from below.

### 1a — the fit gate

All eight fit. Synthesised area 21 164–21 614 µm² against the 22 150 µm²
budget, 207 flops each.

| task | cells | synth µm² | est. core util | verdict |
|---|---:|---:|---:|---|
| `babycry` | 1235 | 21 164 | 95.5 % | TIGHT |
| `water` | 1242 | 21 311 | 96.2 % | TIGHT |
| `mosquito` | 1254 | 21 398 | 96.6 % | TIGHT |
| `siren` | 1260 | 21 460 | 96.9 % | TIGHT |
| `clap` | 1265 | 21 496 | 97.0 % | TIGHT |
| `vad` | 1272 | 21 507 | 97.1 % | TIGHT |
| `dogbark` | 1279 | 21 606 | 97.5 % | TIGHT |
| `catmeow` | 1284 | 21 614 | 97.6 % | TIGHT |

TIGHT rather than FIT only because 207 flops exceeds the shipped 205 — the
same two flops `runs/sheila_nb6` already hardened with at 95.55 % core, 0 DRC,
0 LVS. The eight differ from each other only in weight density (140–161 of 192
non-zero), which is worth 450 µm² of adder tree between the lightest and the
heaviest.

**The headroom above the worst of them is 536 µm².** That is the money
available for every later round.

### 1b — the honest headroom map

fp32, validation-selected, 3 seeds, on exactly the features the chip sees:

| task | chip | linear | MLP-32 | MLP-64 | MLP-256 | **headroom** | gap |
|---|---:|---:|---:|---:|---:|---:|---:|
| `catmeow` | 79.74 | 77.79 | 83.25 | 84.22 | 83.81 | **83.81** | +4.1 |
| `siren` | 76.24 | 79.33 | 81.89 | 82.10 | 81.64 | **81.64** | +5.4 |
| `dogbark` | 75.74 | 70.26 | 80.96 | 81.34 | 81.00 | **81.00** | +5.3 |
| `babycry` | 72.36 | 70.00 | 75.13 | 74.63 | 76.60 | **76.60** | +4.2 |
| `clap` | 72.32 | 57.64 | 69.75 | 72.73 | 74.39 | **74.39** | +2.1 |
| `water` | 68.92 | 67.26 | 73.48 | 71.98 | 74.06 | **73.48** | +4.6 |
| `mosquito` | 62.96 | 65.21 | 65.05 | 65.25 | 65.70 | **65.70** | +2.7 |
| `vad` | 63.72 | 62.18 | 63.52 | 63.93 | 63.81 | **63.81** | +0.1 |

"headroom" is the test AUC of the rung that won on **validation**, so it is
comparable with the chip column and is not the maximum of the row.

**The result that governs the whole search: not one task can reach 90 % on
these features.** The best unconstrained fp32 model on the current front end
tops out at 83.8 % (`catmeow`) and 81.6 % (`siren`). Even a model with 60×
the parameters, full float precision, no accumulator saturation and no ternary
quantisation cannot answer these questions better than that from six octave
bands at 3 dB, 41.9 ms resolution.

So the 90 % target is not a classifier problem. Rounds from here have to change
what the front end measures, and the only currency for that is the 536 µm² in
§1a — or area freed elsewhere.

Three subsidiary readings:

* **`vad` is finished** — on *these* features. Chip 63.72 against a headroom of
  63.81: nothing a classifier can do. That last clause is the load-bearing one
  and I did not treat it that way at the time; round 9 changes the front end
  and `vad`'s headroom goes to 69.24.
* **The ternary H=4 template is already close to an MLP-32** on five of eight
  tasks, and beats the *linear* rung by 2–15 points everywhere. Its four
  hidden units are doing real nonlinear work.
* **`clap` and `mosquito` collapse from validation to test** (86.3→74.4,
  91.7→65.7) in the fp32 probe exactly as they do on the chip. That is the
  split doing its job — HumBugDB's unseen Tanzanian field sites, FSD50K's
  separate eval uploader population — not a modelling failure.

---

## Round 2 — can the classifier get wider?

**Hypothesis.** Round 1 shows the ternary H=4 template sits 2–5 points under an
fp32 MLP-32 on the same features. `NHID` must be a power of two
(`c_hd = slot[HD_W-1:0]`), so 4→8 is the only step. It was measured FAIL once
before, but at `NFRAME=16`, where the weight ROM is twice the size it is now.
Re-measure at the geometry actually in use, and try every way of paying for it.

**Experiment.** Nine `area_gate.py` runs at `TAP0=3 NBAND=6 NFRAME=8`.

| label | change | cells | flops | synth µm² | core | verdict |
|---|---|---:|---:|---:|---:|---|
| `r2_h4_ref` | — (synthetic header, 74 % density) | 1301 | 207 | 21 857 | 98.7 % | TIGHT |
| `r2_h8` | `NHID=8` | 1509 | 257 | 26 151 | 118.1 % | **FAIL** |
| `r2_h8_dbg0` | `NHID=8, DEBUG_PINS=0` | 1512 | 259 | 26 173 | 118.2 % | **FAIL** |
| `r2_h8_sw9` | `NHID=8, SCORE_W=9` | 1506 | 257 | 26 139 | 118.0 % | **FAIL** |
| `r2_h8_sw9_dbg0` | both | 1504 | 259 | 26 160 | 118.1 % | **FAIL** |
| `r2_h8_nb5` | `NHID=8, NBAND=5` | 1407 | 253 | 25 099 | 113.3 % | **FAIL** |
| `r2_h8_nf4` | `NHID=8, NFRAME=4` | 1388 | 255 | 25 068 | 113.2 % | **FAIL** |
| `r2_h8_st9` | `NHID=8, STATE_W=9` | 1460 | 248 | 25 268 | 114.1 % | **FAIL** |
| `r2_nphase4` | `NPHASE=4` | 1423 | 256 | 25 357 | 114.5 % | **FAIL** |

**Refuted — and the refutation was wrong. See round 14.** `NHID=8` costs
4 294 µm² and 50 flops at `NPHASE=2`. Halving the bands, halving the window,
dropping the debug pins, narrowing the score and narrowing the cascade state
together do not recover a third of it, and `NPHASE=4` fails the same way from
the other direction.

The correct reading of that table is in the sentence I wrote to explain it and
then did not act on: *"the hidden accumulators are replicated per phase, so
eight units at two phases is sixteen 6-bit saturating accumulators."* The cost
is `NSLOT = NPHASE × NHID`, not `NHID`. Every configuration in this round holds
`NPHASE=2` and so doubles `NSLOT` along with `NHID`; none of them tests the
hidden width on its own. Round 14 does, and `NHID=8` fits comfortably.

Two facts worth keeping:

* `DEBUG_PINS=0` makes area **worse** (+22 µm², +2 flops), for the third time
  in this project. Driving pins low is not free; the debug muxes are cheaper
  than the constant drivers that replace them.
* `STATE_W` 10→9 is worth **883 µm² and 9 flops**. That is the largest single
  saving found anywhere so far, it is bigger than the 536 µm² of headroom in
  §1a, and it is the obvious way to pay for a front-end change. What it costs
  in accuracy is unmeasured on these eight tasks.

**Conclusion after two rounds.** The classifier cannot get wider and, per round
1, could not reach 90 % even if it did. The search now goes entirely to the
front end, funded by `STATE_W=9` if the accuracy cost is acceptable.

---

## Round 3 — which front-end axis is starving these detectors?

**Hypothesis.** Five candidate changes to what the front end measures, each
plausible on its own grounds, probed on four pilot tasks spanning the range
(`catmeow` best, `siren`/`dogbark` mid, `water` low-but-with-headroom):

| | change | why |
|---|---|---|
| `k3` | `K_SHIFT` 2→3 | moves the six octaves from 243 Hz–15.5 kHz down to 121 Hz–7.8 kHz. The corpora are 16 kHz sources, so today's top band is mostly above the content. |
| `nb7` | `NBAND` 6→7 | keeps all six and adds 121–243 Hz underneath (`NSTAGE` 9→10). |
| `m2` | `MANT` 1→2 | 1.5 dB log steps instead of 3 dB (`FEAT_W` 4→5). |
| `fl15` | `FRAME_LOG2` 15 | 21 ms frames: twice the time resolution. |
| `fl17` | `FRAME_LOG2` 17 | 84 ms frames: half the resolution, twice the span. |

plus a free control — probe the *unchanged* features over other window lengths
(`NFRAME` 2, 4, 16, 24), which costs only weight ROM.

`train/extract_clips.py` gained `--cache-tag` for this, so a geometry sweep
re-runs only the front end and never re-decodes a corpus.

**Result.** Validation AUC (the selection criterion), best rung of an
MLP-32/64 ladder, 2 seeds. Baseline is the shipped geometry at `NFRAME=8`.

| variant | `catmeow` | `siren` | `dogbark` | `water` |
|---|---:|---:|---:|---:|
| baseline | 92.29 | 86.67 | 79.29 | 78.89 |
| `k3` | 93.12 | 86.89 | **83.05** | **84.09** |
| `nb7` | 92.93 | 88.74 | 80.57 | 80.09 |
| `m2` | 92.91 | **88.83** | 80.14 | 80.68 |
| `fl15` @ `NFRAME`=16 | **93.38** | 87.33 | 80.00 | 81.12 |
| `fl17` | 92.60 | 88.37 | 78.07 | 81.55 |
| `NFRAME`=16 | 92.00 | 88.49 | 78.59 | 81.79 |
| `NFRAME`=24 | 91.65 | 89.04 | 78.81 | 82.36 |

**Every axis is worth about a point, and which axis is strongly per-task.**
`K_SHIFT=3` is the best change available to `dogbark` (+3.8) and `water`
(+4.2) and the worst available to `catmeow` on test (80.74 against a baseline
84.22) — a dog's bark and running water live in the octaves `k3` moves the
bands onto, a cat's meow does not. Since each detector is already its own
build, that is a per-task choice, not a compromise.

**The subsidiary result turned out to matter more than the main one.** The
free window-length control found `NFRAME=4` scoring as well as `NFRAME=8` on
`catmeow` (test 85.06 vs 84.22) and `babycry` (76.25 vs 76.60) — at half the
weight ROM. That reopened the area question and produced round 7.

Area, measured alongside: `K_SHIFT=3` +58 µm², `FRAME_LOG2=15` +58,
`FRAME_LOG2=17` −66, `MANT=2` +809 (FAIL alone), `NFRAME=16` +740 (FAIL
alone), `NBAND=7` +1620 (FAIL, and still FAIL with `STATE_W=9`).

---

## Round 4 — what does the funding source cost?

**Hypothesis.** `STATE_W` 10→9 frees 883 µm², more than the 536 µm² of
headroom in §1a, and is the only way to pay for `MANT=2` or `NFRAME=16`. But
it narrows the cascade state, and its accuracy cost has never been measured on
anything. If it costs more than `MANT=2` gains, the branch is dead.

**Experiment.** Extract `st9` alone and each affordable stack, probe at the
window lengths that fit.

**Result — `STATE_W=9` is not a cost. It is a gain, on every task tried.**

| | baseline | `st9` | `m2st9` | `fl15st9` | `fl15m2st9` |
|---|---:|---:|---:|---:|---:|
| `catmeow` | 92.29 | 94.43 | 92.96 | 93.67 | **94.61** |
| `siren` | 86.67 | **90.14** | 89.49 | 88.58 | 89.04 |
| `dogbark` | 79.29 | 83.74 | 83.37 | 83.88 | **84.32** |
| `water` | 78.89 | **85.74** | 82.77 | 84.34 | 83.51 |

(validation AUC; `st9`, `fl15st9` and `fl15m2st9` at `NFRAME=16`, the rest at 8.)

A narrower cascade state regularises: 9 bits still resolves every band the log
feature can encode, and the extra bit was only carrying dither. The gain is
+2.1 to +6.9 validation points for −883 µm².

**But the best-by-validation stacks do not fit.** `NFRAME=16` was never gated
in round 3, and when it was: `NFRAME=16, MANT=2, STATE_W=9` is 22 666 µm²
(fl16) or 22 488 (fl15), both over the 22 150 budget. `NFRAME=16` with
`MANT=1` and `STATE_W=9` fits at 21 690 (fl16) and 21 507 (fl15). So the
fitting winners are `st9` for `catmeow`, `siren` and `water`, and `fl15st9`
for `dogbark` — `MANT=2` is affordable only at `NFRAME` ≤ 8.

---

## Round 5 — is a second statistic per band worth more than a seventh band?

**Hypothesis.** The front end keeps exactly one number per band per frame: the
maximum log magnitude over 41.9 ms. Everything else the band did is discarded.
A second statistic costs one more `FEAT_W` register and one more comparator
per band — no extra cascade stage, no extra decimator, no extra 10-bit state —
which is far cheaper than `NBAND=7`'s +1620 µm². Three candidates: `max,min`
(modulation depth — a transient against a steady tone), `max,mean` (average
energy), `max,min,mean`.

`train/optim/fe_stats.py` runs the same bit-exact cascade as
`wwhw.frontend_batch` and records all four statistics; its `max` plane was
checked **byte-identical** to `extract_clips.py`'s output on `catmeow` before
the round was run.

**Result — this is the largest effect in the whole search.**

Test AUC, best rung by validation, at the window length that won:

| task | baseline | `max,min` | `max,mean` | `max,min,mean` |
|---|---:|---:|---:|---:|
| `babycry` | 74.63 | 75.68 | **83.67** | 84.59 |
| `catmeow` | 84.22 | 84.97 | **87.66** | 87.75 |
| `siren` | 82.10 | 82.15 | **86.98** | 86.82 |
| `dogbark` | 81.34 | 80.70 | **83.68** | 84.64 |
| `water` | 71.98 | 71.13 | **75.09** | 77.26 |
| `clap` | 72.73 | 72.65 | **75.14** | 74.40 |

**It is specifically the average, not "a second number".** `max,min` is worth
nothing at all — on three of six tasks it is *worse* than one statistic — while
`max,mean` is worth +2.4 to +9.0 test AUC. And `max,min,mean` is not reliably
better than `max,mean`, so the min carries no information the other two lack:
the frame minimum is the band's quiet floor, dominated by which decimation
phase the frame boundary lands on.

For scale: the mean is worth more on `babycry` alone (+9.0) than every cascade
change in rounds 3 and 4 combined, on any task.

**Caveat that round 10 exists to settle:** `mean` as measured is an exact
per-frame average, and each band ticks a different number of times per frame
(2^(`FRAME_LOG2`−b)), so an exact mean needs a per-band divisor. It is not
buildable as it stands.

---

## Round 6 — is sheila's training recipe wrong for these corpora?

**Hypothesis.** `new_tasks.md` §6 says plainly that "no hyper-parameter search
was run for any task": all eight use the recipe tuned on Speech Commands.
These corpora differ from it in every way an optimiser cares about — 4 625 to
43 499 clips, 8 % to 78 % positive, 92 to 3 330 validation positives — so
per-task tuning of the *free* knobs should be worth a point at zero area.

**Experiment.** Coordinate sweep over 28 configurations (pooling, warmup,
epochs, four kinds of augmentation, label smoothing, positive weighting,
requantiser leak, three learning rates, weight decay, EMA, schedule, `HSHIFT`),
4 seeds each, ranked by mean validation AUC.

**Refuted.** The best configuration beats the shipped recipe by **0.5 points on
`catmeow` (92.39 vs 91.87) and 0.2 on `clap` (84.61 vs 84.42)**, both inside
the seed spread. The winners — `aug_time=1`, `epochs=2000`, `pos_weight=2.0` —
are all marginal, and no knob moved any task by more than its own ±0.3–0.8
standard deviation.

This is a useful negative. It says the recipe was never the problem, and it
means every later round can keep using it unchanged rather than re-tuning per
front end.

---

## Round 7 — the affordable design space, enumerated

**Hypothesis.** Round 3 gated one axis at a time against `NFRAME=8` and
concluded `MANT=2` needs `STATE_W=9` and `NBAND=7` is unreachable. Round 3's
own control undermines both: `NFRAME=4` scores as well as 8 on two tasks and is
worth 577 µm². The box should be enumerated, not walked.

**Experiment.** 72 points: `NFRAME` ∈ {2,4,8} × `NBAND` ∈ {6,7} × `MANT` ∈
{1,2} × `STATE_W` ∈ {9,10} × `FRAME_LOG2` ∈ {15,16,17}, later extended with
`NFRAME`=16. Every point is a synthetic header at the shipped 74 % weight
density, which runs 250–700 µm² heavier than the real headers the eight tasks
emit — so a TIGHT here is comfortable in practice and a FAIL is real.

**Result. 35 of 72 fit**, and the reachable menu is much narrower than the
axis-at-a-time view suggested:

* **`NBAND=7` and `MANT=2` never fit together**, at any window length.
* **`NBAND=7` needs both `NFRAME` ≤ 4 and `STATE_W=9`** (21 614–21 799 TIGHT).
  At `STATE_W=10` it fails at every window length.
* **`MANT=2` needs `NFRAME` ≤ 8 and `STATE_W=9`** — or `NFRAME` ≤ 4 on its own.
* `FRAME_LOG2` is free across the whole box (±60 µm²), so time resolution
  costs nothing and can be chosen per task on accuracy alone.
* The cheapest fitting point is `NFRAME=2, STATE_W=9` at 19 585 µm² — 88.4 % of
  core, 2 565 µm² under budget. Halving the window twice pays for a great deal.

---

## Round 8 — should the chip track its own input level?

**Hypothesis.** The chip subtracts one *constant*, `FEAT_OFF`, from every band
of every frame, so a detector trained at one input level degrades at another.
`docs/robustness.md` measures exactly that: a 3 dB level drop takes sheila's
recall from 19.8 % to 3.1 %. Replacing the constant with a level the design
tracks for itself — `m += (x − m) >> shift` per band, one accumulator and one
shift, updated once per frame — should help, and should help most on the
corpora with the widest recording-level spread.

**Experiment.** Three time constants (shift 1, 2, 3) plus `clip`, a
non-causal per-clip mean subtraction that is not implementable and is there
only as the upper bound on what the causal version could reach. All eight tasks.

**Refuted on all eight, including by the upper bound.** Validation / test AUC:

| task | none | ema shift 1 | shift 2 | shift 3 | clip (upper bound) |
|---|---|---|---|---|---|
| `babycry` | **77.81 / 74.87** | 73.75 / 71.33 | 74.77 / 72.01 | 74.57 / 73.04 | 74.05 / 71.95 |
| `catmeow` | **92.38 / 83.98** | 87.08 / 73.09 | 89.28 / 75.35 | 89.44 / 75.58 | 89.76 / 76.22 |
| `clap` | **85.93 / 72.57** | 82.40 / 67.36 | 82.76 / 66.17 | 83.16 / 69.20 | 83.89 / 68.89 |
| `dogbark` | 79.10 / **81.37** | 79.25 / 79.52 | 79.29 / 80.66 | 79.40 / 81.26 | 79.65 / 79.84 |
| `mosquito` | **91.62 / 64.71** | 89.33 / 63.49 | 89.72 / 63.74 | 90.01 / 65.15 | 91.02 / 65.55 |
| `siren` | **86.57 / 81.98** | 82.02 / 75.40 | 82.66 / 75.68 | 82.28 / 76.75 | 83.29 / 76.62 |
| `vad` | **82.32 / 63.47** | 80.14 / 62.58 | 80.89 / 63.36 | 81.30 / 64.51 | 81.34 / 63.16 |
| `water` | **79.92 / 73.52** | 77.84 / 68.44 | 79.46 / 69.27 | 79.46 / 70.23 | 79.92 / 72.70 |

**Absolute level is a feature, not a nuisance, on all eight corpora.** Removing
it costs 3–9 validation points, and the non-causal bound costs almost as much,
so this is not a matter of choosing a better time constant. It makes sense once
stated: in every one of these datasets a positive clip is one where the event
is *near the microphone*, and loudness is a real part of the answer. That is a
property of the corpora rather than of the question, and it is worth writing
down — a detector built this way will be level-sensitive in the field, as
`robustness.md` already found for sheila.

Note the contrast with round 5, which is not a contradiction: round 5 *adds* a
smoothed level as an extra feature and keeps the max; round 8 *replaces* the
level by subtracting it. The model wants both the peak and the average, and
wants to keep the absolute value of both.

---

## Round 9 — score every front end that fits, on every task

**Hypothesis.** Rounds 3 and 4 used four pilot tasks. Round 7 says the
reachable menu is three front ends, all at `STATE_W=9`: `st9` (6 bands, 3 dB),
`m2st9` (6 bands, 1.5 dB), `nb7st9` (7 bands, 3 dB, needs `NFRAME` ≤ 4). Score
all three on all eight tasks at `NFRAME` 2, 4 and 8, so the per-task choice is
made on evidence rather than on the pilots.

**Result.** Best by validation, with the test AUC alongside. **✓** marks the
row as fitting at that window length — `nb7st9` fails at `NFRAME=8`
(22 279–22 397) and `m2st9` fails at `NFRAME=16`, so the best-scoring row is
not always the one that can be built.

| task | chip today | round-1 headroom | best fitting front end | val | test |
|---|---:|---:|---|---:|---:|
| `babycry` | 72.36 | 76.60 | `st9` nf8 ✓ | 82.45 | **80.94** |
| `catmeow` | 79.74 | 83.81 | `st9` nf8 ✓ | 93.03 | **84.87** |
| `clap` | 72.32 | 74.39 | `nb7st9` nf2 ✓ | 87.35 | **75.92** |
| `dogbark` | 75.74 | 81.00 | `m2st9` nf8 ✓ | 83.37 | **84.03** |
| `mosquito` | 62.96 | 65.70 | base nf8 ✓ | 91.67 | **65.70** |
| `siren` | 76.24 | 81.64 | `m2st9` nf8 ✓ | 89.49 | **83.29** |
| `vad` | 63.72 | 63.81 | `nb7st9` nf4 ✓ | 84.39 | **68.76** |
| `water` | 68.92 | 73.48 | `m2st9` nf4 ✓ | 84.20 | **75.81** |

**Every task moves, and `vad` moves most.** Round 1 called `vad` finished —
chip 63.72 against a headroom of 63.81, the features being the whole limit.
That was true of the front end round 1 measured and I generalised it past its
evidence: with `STATE_W=9` and a seventh band, `vad`'s headroom is 69.24, and
the fitting version is 68.76. The lesson is that "the features are the limit"
is a statement about one set of features, and the honest way to write it is
with the geometry attached.

**`mosquito` is the exception and its validation column is a trap.** Every
front end raises its validation AUC — `m2st9` reaches 92.04 against the
baseline's 91.67 — and every one of them *lowers* test, `st9` to 59.38 and
`nb7st9` to 57.53. The HumBugDB split is Tanzanian field sites with unseen
recording rigs, and a front end tuned to score better in-domain transfers
worse. Selection is on validation, so a naive pipeline would pick `m2st9`
here and lose 4.6 points. `mosquito` keeps the shipped front end.

**Hypothesis.** Round 5's `mean` is not buildable: it is an exact per-frame
average and each band ticks a different number of times per frame
(2^(`FRAME_LOG2`−b)), so it needs a per-band divisor. The buildable form is a
leaky integrator carried across frame boundaries — `favg += (feat − favg) >>
AVG_SHIFT`, one accumulator and one shift per band, the same arithmetic the
cascade already does `NSTAGE` times per tick.

**The RTL.** `src/tt_um_wakeword.sv` gains `NSTAT` (1 or 2) and `AVG_SHIFT`.
`NSTAT=2` adds a second rotating ring `favg`, rotated in lockstep with `fmax`
so the band under update is always at the head, held shifted left by
`AVG_SHIFT` so the shift is exact and no rounding state is lost. It is
deliberately **not** cleared at the frame boundary: it is an integrator with a
time constant of its own, and clearing it every frame would turn it back into a
per-frame statistic whose value depends on where the boundary fell. The
template then reads `NBAND*NSTAT` features per row, band-major — `[band0 max,
band0 avg, band1 max, …]`, the same order `fe_stats.py` writes.

**Regression first.** At the default `NSTAT=1` the design synthesises to
**1301 cells, 207 flops, 21 857 µm²** — identical in all three numbers to
`r3_ref` measured before the change. Both builds still elaborate under
iverilog, with the same (pre-existing) `constant selects in always_*` warning
the unmodified file produces. The change is a true no-op for everything that
ships today.

**Result — the average costs two bands.** `NSTAT=2`, `AVG_SHIFT=3`,
`STATE_W=9`:

| `NBAND` | `NFRAME`=2 | `NFRAME`=4 | `NFRAME`=8 |
|---|---:|---:|---:|
| 4 | 21 427 TIGHT | **21 975 TIGHT** | 22 563 FAIL |
| 5 | 23 949 FAIL | 24 212 FAIL | 25 215 FAIL |
| 6 | 26 146 FAIL | 26 921 FAIL | 27 483 FAIL |

A shorter time constant does not rescue the fifth band — `AVG_SHIFT=1` narrows
the accumulator by two bits and still lands at 23 209. Each extra band with
`NSTAT=2` costs a `STATE_W` word, a `FEAT_W` max register, an `AVG_W`
accumulator, a cascade stage **and** two adder-tree columns, so the marginal
band is roughly 2 500 µm² rather than the ~400 it costs at `NSTAT=1`.

**But two further savings buy the fifth band back.** `NPHASE` 2→1 halves the
hidden accumulator ring (`NSLOT = NPHASE*NHID`) and `HACC_W` 6→5 narrows every
one of them:

| configuration | synth µm² | verdict |
|---|---:|---|
| `NBAND=5 NFRAME=2 NPHASE=1 HACC_W=5` | **20 979** | FIT |
| `NBAND=5 NFRAME=4 NPHASE=1 HACC_W=5` | **21 340** | TIGHT |
| `NBAND=5 NFRAME=2 NPHASE=1 HACC_W=6` | 22 263 | FAIL |
| `NBAND=6 NFRAME=2 NPHASE=1 HACC_W=5` | 23 439 | FAIL |

So **five bands is the ceiling with the average, and six is unreachable by any
combination tried.** Neither saving is free in accuracy: `NPHASE=1` stops the
scored windows overlapping, which coarsens the multiple-instance bag, and the
bag has to be the one the hardware scores — `nn_optimization.md` measured a
mismatched training grid costing 9 points. `HACC_W=5` is invisible to the fp32
probe entirely and needs the quantised trainer to evaluate.

**This leaves three candidate shapes**, which rounds 12 and 13 score against
each other and against the six-band max the eight detectors ship with today:

* 6 bands, max only — today, `NSTAT=1`
* 4 bands, max + average, `NPHASE=2`, `HACC_W=6`
* 5 bands, max + average, `NPHASE=1`, `HACC_W=5`

---

## Round 14 — the accumulator ring, not the hidden units

**Hypothesis.** Round 11 bought the fifth band by dropping `NPHASE` to 1, which
halves `NSLOT = NPHASE × NHID`. That is the same quantity round 2 blamed on
`NHID`. If `NSLOT` is what costs, then `NHID=8` at `NPHASE=1` has exactly the
eight accumulators the shipped `NHID=4, NPHASE=2` design already has, and the
classifier width round 2 declared dead should fit.

**Confirmed.** `TAP0=3 NBAND=6 NSTAT=1`:

| configuration | cells | flops | synth µm² | core | verdict |
|---|---:|---:|---:|---:|---|
| `NHID=8 NPHASE=1 NFRAME=2 STATE_W=9` | 1161 | 196 | **20 053** | 90.5 % | FIT |
| `NHID=8 NPHASE=1 NFRAME=4 STATE_W=9` | 1272 | 198 | **21 064** | 95.1 % | FIT |
| `NHID=8 NPHASE=1 NFRAME=4 STATE_W=9 HACC_W=5` | 1214 | 190 | **20 218** | 91.3 % | FIT |
| `NHID=8 NPHASE=1 NFRAME=8 STATE_W=9` | 1312 | 200 | 21 443 | 96.8 % | TIGHT |
| `NHID=8 NPHASE=1 NFRAME=4 STATE_W=10` | 1325 | 207 | 22 054 | 99.6 % | TIGHT |
| `NHID=8 NPHASE=2 NFRAME=8 STATE_W=10` (round 2) | 1509 | 257 | 26 151 | 118.1 % | FAIL |

**`NHID=8` is 4 708 µm² cheaper at `NPHASE=1` than at `NPHASE=2`** — it is
the ring that costs, and round 2's conclusion was an artefact of never varying
`NPHASE` with it. Doubling the hidden width is affordable, and it matters:
round 1 measured a bare linear read-out 7–12 points below an fp32 MLP-32 on
the same features, with the ternary `H=4` template sitting between them.

**And both large levers fit together.** `NHID=8`, `NSTAT=2`, 4 bands,
`NPHASE=1`, `HACC_W=5`, `STATE_W=9`, `NFRAME=4`: **21 634 µm², TIGHT** — twice
the hidden width *and* the per-band average, inside the same tile.

What `NPHASE=1` and `HACC_W=5` cost in accuracy is not something the fp32
probe can see: `NPHASE` sets the multiple-instance bag the chip scores, and
`HACC_W` is the saturating accumulator the probe does not have. Both need the
quantised trainer, which is what the capacity cross-sweep (`H` × `NPHASE` ×
`HACC_W`, 4 seeds, all eight tasks) measures.
