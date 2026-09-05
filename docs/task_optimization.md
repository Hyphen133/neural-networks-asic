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

* **`vad` is finished.** Chip 63.72 against a headroom of 63.81. The features
  are the entire limit and there is nothing a classifier can do. Any gain has
  to come from the front end or not at all.
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

**Refuted, decisively.** `NHID=8` costs 4 294 µm² and 50 flops — the hidden
accumulators are replicated per phase, so eight units at two phases is sixteen
6-bit saturating accumulators plus sixteen requantisers plus twice the adder
tree. Halving the bands, halving the window, dropping the debug pins, narrowing
the score and narrowing the cascade state together do not recover a third of
it. `NPHASE=4` fails for the same reason from the other direction.

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
