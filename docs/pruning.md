# Pruning drone_2

drone_2 is the most accurate detector in this repo — 98.92 % test AUC — and it
is also the one with the least room left: `runs/drone_g20h` places at 94.6 %
core utilisation with 205 flip-flops, 1 675 standard cells and 4.76 mW of
estimated total power. Every search before this one asked what it would cost to
make it *better*, and measured only configurations that grow. This document
asks the opposite question: **what does drone_2 give up if it is made
smaller?**

Since this study started, `runs/drone_3` has added the fanout fix
([fanout.md](fanout.md)): the same netlist at 94.93 % utilisation, 27 476 µm²
and 4.91 mW with 0 max-fanout violations. That is a `src/config.json` change,
not an RTL one, so every number below — which is measured at synthesis, before
CTS — applies unchanged to both builds, and §7's power comparison should be
read against drone_3's **4.91 mW**.

Companion to [nn_optimization.md](nn_optimization.md) (the accuracy search),
[DRONE.md](DRONE.md) (the detector) and [DESIGN.md](DESIGN.md) (the hardware).
Harness: `scripts/prune_drone.sh`, `train/optim/area_gate.py`,
`train/optim/qat.py`.

## 1. What "pruning" can mean here, and what each kind touches

Four different things get called pruning, and in this design they are not
variations of one lever — they act on different parts of the chip:

| kind | knob | what it removes |
|---|---|---|
| **weight pruning** | `qat.py` `wt=…`, `l1=…` | non-zero weights in the ternary template |
| **row pruning** | `tie=…` | *distinct rows* of the weight table (weights shared across frames) |
| **geometry pruning** | `NHID`, `NPHASE`, `NFRAME`, `HACC_W` | hidden units, window phases, weight ROM, accumulator bits |
| **front-end pruning** | `NBAND`, `STATE_W`, `MANT` | filterbank bands, cascade precision, log resolution |

Area is measured with `train/optim/area_gate.py`, which synthesises the real
RTL at the geometry in question and compares against the 22 150 µm²
synthesised budget the flow multiplier implies (that file's header derives the
budget). One synthesis is ~1.3 s, so the whole area frontier below runs
*before* any training and decides what is worth training at all.

Two baselines are quoted throughout, and they differ by 400 µm²:

| baseline | synth area | cells | flops |
|---|---:|---:|---:|
| drone_2's real header (`src/ww_weights_drone.svh`, 259/320 non-zero) | 21 164 µm² | 1 242 | 205 |
| a synthetic template at the same 81 % density | 21 527 µm² | 1 285 | 205 |

A real template is 400 µm² cheaper than a random one of the same density. That
is the first hint of where the area actually goes, and §2 turns it into a
measurement.

## 2. Weight pruning buys nothing — and the reason matters

`src/tt_um_wakeword.sv:28` says "a zero simply drops that term from the adder
tree, so sparsity is free area". The first half is true. The second half is
not, and the difference is the difference between sparsity being a lever and
sparsity being a non-event.

Synthesising the drone geometry with a synthetic template at several densities,
four independent draws each (`--density`, `--seed`):

| non-zero fraction | mean synth area | spread over 4 draws |
|---:|---:|---:|
| 100 % | 20 721 µm² | ±42 |
| 81 % (what drone_2 has) | 21 581 µm² | ±68 |
| 50 % | 21 600 µm² | ±44 |
| 30 % | 21 444 µm² | ±160 |
| 10 % | 20 553 µm² | ±126 |

Pruning the template from 81 % to 50 % non-zero — throwing away two fifths of
the weights — moves the area by **+19 µm²**, inside the noise of re-drawing the
template. Even at 30 % the saving is 137 µm², 0.6 %.

Area is *maximal in the middle* and falls at both extremes, which says where the
cost is: not the adder tree, but the logic that selects the current row of the
weight table. The classifier visits one `(unit, frame)` row per step,
`wsel = c_hd*NFRAME + c_slot` (`tt_um_wakeword.sv:309`), and yosys builds that
table as constant-folded selection logic whose size tracks how *varied* the
rows are, not how many non-zeros they hold: an all-±1 table and an all-zero
table are both cheap, a maximum-entropy table is dearest. The 400 µm² gap
between drone_2's real header and a random template of the same density is the
same effect — trained rows repeat each other, random rows do not.

Weight-level pruning is therefore free but pointless here, and — equally worth
knowing — a *denser* template costs nothing either. There is no area reason to
regularise the template in either direction.

## 3. Row pruning: prune the table, not the weights

If the cost is the table's variety, the way to prune it is to make rows
identical. `tie=k` trains one row per block of `k` frames and repeats it, so
`NFRAME/k` distinct rows describe the window. **No RTL parameter changes**: the
header still holds 16 rows, they are just equal in blocks, and the silicon that
already exists reads it as-is.

This is not an arbitrary constraint for this detector. [DRONE.md](DRONE.md) §4
already noted that the drone template's two dominant units are "nearly constant
along the time axis, which is what a stationary target should produce". `tie`
spends that observation as area.

Real emitted headers, drone geometry, synthesised:

| rows per unit | synth area | vs the untied header |
|---|---:|---:|
| 16 (untied) | 21 106 µm² | — |
| 4 (`tie=4`) | 20 327 µm² | −779 µm² |
| 2 (`tie=8`) | 20 545 µm² | −561 µm² |
| 1 (`tie=16`) | 20 436 µm² | −670 µm² |

−780 µm² for a training-side constraint and no hardware change: an order of
magnitude more than weight pruning bought. What it costs in accuracy is §5.

## 4. The geometry frontier

One parameter at a time, against the synthetic 81 % baseline (21 527 µm²).
`NHID`, `NPHASE` and `NFRAME` must stay powers of two
(`c_hd = slot[HD_W-1:0]`; `frame_idx` wraps at `2^FIDX_W`).

| change | cells | flops | synth area | Δ area | Δ flops | est. core | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| — (drone_2 geometry) | 1 285 | 205 | 21 527 | — | — | 97.2 % | TIGHT |
| `NHID` 4→2 | 1 147 | 179 | 19 039 | −2 488 | −26 | 86.0 % | FIT |
| `NHID` 4→1 | 1 124 | 166 | 18 087 | −3 440 | −39 | 81.7 % | FIT |
| `NPHASE` 2→1 | 1 234 | 181 | 19 992 | −1 535 | −24 | 90.3 % | FIT |
| `NFRAME` 16→8 | 1 215 | 203 | 20 797 | −730 | −2 | 93.9 % | FIT |
| `NFRAME` 16→4 | 1 135 | 201 | 20 054 | −1 473 | −4 | 90.5 % | FIT |
| `NFRAME` 16→2 | 1 093 | 199 | 19 566 | −1 961 | −6 | 88.3 % | FIT |
| `HACC_W` 6→5 | 1 275 | 197 | 21 053 | −474 | −8 | 95.0 % | FIT |
| `HACC_W` 6→4 | 1 177 | 189 | 19 663 | −1 864 | −16 | 88.8 % | FIT |
| `SCORE_W` 10→9 | 1 269 | 205 | 21 440 | −87 | 0 | 96.8 % | TIGHT |
| `NBAND` 5→4, top band dropped (`TAP0` 4→5) | 1 203 | 201 | 20 662 | −865 | −4 | 93.3 % | FIT |
| `NBAND` 5→3, top two dropped (`TAP0` 4→6) | 1 157 | 197 | 19 952 | −1 575 | −8 | 90.1 % | FIT |
| `NBAND` 5→4, bottom band dropped (`NSTAGE` 9→8) | 1 195 | 191 | 20 066 | −1 461 | −14 | 90.6 % | FIT |
| `STATE_W` 10→9 | 1 274 | 196 | 20 991 | −536 | −9 | 94.8 % | FIT |
| `STATE_W` 10→8 | 1 222 | 187 | 20 002 | −1 525 | −18 | 90.3 % | FIT |
| `MANT` 1→0 (`FEAT_W` 4→3) | 1 177 | 200 | 20 369 | −1 158 | −5 | 92.0 % | FIT |

And the combinations (all FIT, none needs a re-gate):

| combination | cells | flops | synth area | Δ area | est. core |
|---|---:|---:|---:|---:|---:|
| `NHID=2 NPHASE=1` | 1 109 | 167 | 18 148 | −3 379 | 81.9 % |
| `NHID=2 NPHASE=1 NFRAME=8` | 1 102 | 165 | 17 974 | −3 553 | 81.2 % |
| `NHID=2 NPHASE=1 HACC_W=5` | 1 090 | 165 | 17 802 | −3 725 | 80.4 % |
| `NBAND=4 NHID=2 NPHASE=1` | 1 086 | 163 | 17 829 | −3 698 | 80.5 % |
| all of it: `NBAND=4 STATE_W=9 NPHASE=1 NHID=2 NFRAME=8` | 944 | 152 | 15 989 | −5 538 | 72.2 % |

The floor is a quarter of the chip: 15 989 µm² against 21 527, 152 flip-flops
against 205, 72 % core against 97 %. Whether any of it is usable is an accuracy
question, and §5 answers it.

**Run-time work, not only area.** `NPHASE` and `NHID` also set how much the
classifier *does*: `S_CLASS` visits `NSLOT = NPHASE × NHID` accumulator slots
per frame, each an `NBAND`-wide add (`tt_um_wakeword.sv:149`, `:204`, `:422`).
drone_2 runs 8 slots × 5 bands = 40 adds per frame; `NHID=2 NPHASE=1` at four
bands runs 8, five times less switching in the classifier. That is the "usage"
half of the question, and §7's hardened power number measures it.

## 5. What each prune costs in accuracy

Every row below is 4 seeds at 250 epochs — a quarter of the shipped recipe,
paid by the baseline too, so the comparison is fair even though the absolute
numbers sit ~0.1 under the shipped 1000-epoch model (98.64 here against 98.76
shipped). Selection is on validation; test is reported, never used to choose.
`µm²/pt` is the area bought per AUC point given up: **higher is better value**,
and a free prune has none.

| prune | val mean ± sd | test | Δ test | area | Δ flops | µm²/pt |
|---|---:|---:|---:|---:|---:|---:|
| — baseline | 98.94 ± 0.16 | 98.64 | — | — | — | — |
| `tie=2` | **99.09 ± 0.17** | **98.76** | **+0.12** | −561 | 0 | free |
| `NPHASE` 2→1 | **99.05 ± 0.19** | **98.91** | **+0.27** | −1 535 | −24 | free |
| `wt=0.9` | 99.03 ± 0.19 | 98.71 | +0.07 | ~0 | 0 | — |
| `l1=0.01` | 98.98 ± 0.10 | 98.71 | +0.07 | ~0 | 0 | — |
| `wt=1.1` (→75 % non-zero) | 99.01 ± 0.17 | 98.67 | +0.03 | ~0 | 0 | — |
| `tie=4` | 98.87 ± 0.23 | 98.58 | −0.06 | −779 | 0 | ~13 000 |
| `tie=8` | 98.38 ± 0.28 | 98.07 | −0.57 | −670 | 0 | 1 175 |
| `NHID` 4→2 | 97.69 ± 0.58 | 97.46 | −1.18 | −2 488 | −26 | **2 110** |
| `NBAND` 5→4 (drop 3.9–7.8 kHz) | 97.31 ± 0.57 | 97.80 | −0.84 | −865 | −4 | 1 030 |
| `STATE_W` 10→9 | 98.02 ± 0.29 | 97.71 | −0.93 | −536 | −9 | 576 |
| `NBAND=4` + `STATE_W=9` | 96.68 ± 0.43 | 97.31 | −1.33 | −1 724 | −13 | 1 296 |
| `NBAND` 5→3 | 95.41 ± 0.63 | 95.73 | −2.91 | −1 575 | −8 | 541 |
| `MANT` 1→0 | 96.21 ± 0.50 | 96.34 | −2.30 | −1 158 | −5 | 503 |
| `HACC_W` 6→5 | 97.94 ± 0.53 | 97.34 | −1.30 | −474 | −8 | 365 |
| `HACC_W` 6→4 | 95.13 ± 1.53 | 94.61 | −4.03 | −1 864 | −16 | 463 |
| `NFRAME` 16→8 | 97.51 ± 0.49 | 96.65 | −1.99 | −730 | −2 | 367 |
| `NFRAME` 16→4 | 95.16 ± 0.18 | 94.20 | −4.44 | −1 473 | −4 | 332 |
| `NFRAME` 16→2 | 93.13 ± 0.35 | 92.05 | −6.59 | −1 961 | −6 | 298 |
| `tie=16` (1 row/unit) | 95.86 ± 1.16 | 95.41 | −3.23 | −670 | 0 | 207 |
| `NHID` 4→1 (linear template) | 94.47 ± 0.88 | 93.98 | −4.66 | −3 440 | −39 | 738 |
| `NHID=2` + `NPHASE=1` | 97.49 ± 0.45 | 97.33 | −1.31 | −3 379 | −38 | **2 580** |
| `NPHASE=1` + `NFRAME=8` | 98.00 ± 0.33 | 97.24 | −1.40 | −2 499 | −26 | 1 785 |
| `NHID=2` + `NFRAME=8` | 95.32 ± 0.80 | 94.73 | −3.91 | −2 787 | −28 | 713 |
| `NHID=2 NPHASE=1 NFRAME=8` | 96.06 ± 0.42 | 95.32 | −3.32 | −3 553 | −40 | 1 070 |

Four things this says that were not obvious beforehand:

1. **Two prunes are free, and free on validation as well as on test.**
   `NPHASE=1` and `tie=2` both rank *above* the unpruned baseline on the
   selection metric, so a val-only rule picks them with no knowledge of test.
   Their margins are inside one seed spread, so the claim is "free", not
   "better".
2. **Weight pruning is a no-op in both directions** — `wt` and `l1` move
   accuracy by less than a seed spread and area by less than the synthesis
   noise floor, anywhere from 75 % to 88 % non-zero.
3. **The prunes are roughly additive, and additivity is a rule of thumb rather
   than a law.** `NHID=2 NPHASE=1 NFRAME=8` costs 3.32 points against the 2.90
   its parts predict, and `NHID=2 NPHASE=1` costs 1.31 against 0.91 — but
   `NPHASE=1 NFRAME=8` costs 1.40 against a predicted 1.72, i.e. *less* than
   its parts. Nothing reaches §4's 15 989 µm² floor cheaply, and any pair worth
   shipping has to be measured rather than summed.
4. **Integration time is the detector, and the worst thing to cut.** Every
   `NFRAME` row sits at the bottom of the value ranking (298–367 µm²/pt),
   which is the area frontier agreeing with the mechanism: unit 2's bias of −7
   requires the hum to be sustained ([DRONE.md](DRONE.md) §4).

The front-end rows come from their own extractions, so their splits are not
equally hard — `NBAND=4`, `NBAND=3` and `MANT=0` score *higher* on test than on
validation, where every same-feature row has val above test by ~0.3. Use them
for the size of the loss, but rank them against each other rather than against
the classifier-side rows.

## 6. Recommendation

**Adopt `NPHASE=1` together with `tie=2`. Nothing else pays.**

Measured on real emitted headers at their own geometry, not summed from two
one-at-a-time rows:

| build | cells | flops | synth area | est. core | verdict |
|---|---:|---:|---:|---:|---|
| drone_2 as it ships | 1 242 | 205 | 21 164 µm² | 95.5 % | FIT |
| `NPHASE=1` | 1 183 | 181 | 19 460 µm² | 87.9 % | FIT |
| `NPHASE=1` + `tie=2` | **1 134** | **181** | **19 030 µm²** | **85.9 %** | FIT |

**−2 134 µm² (−10 %), −24 flip-flops, −108 cells and 9.6 points of core
utilisation, at no measured accuracy cost.** The value is not the smaller
number: drone_2 places at 94.6 % with no room for a fanout or hold-repair
surprise, and this hands back the margin whose absence made `NBAND=6`
unplaceable (`tt_um_wakeword.sv:45`).

What each half does:

* **`NPHASE=1`** deletes the second staggered window. The accumulator ring goes
  from `NSLOT = 2×4` slots to 4 (`:149`) — that is the 24 flip-flops — and the
  classifier takes 4 steps per frame instead of 8. Windows stop overlapping:
  verdicts arrive every 671 ms instead of every 335 ms. It is free *for this
  detector* because a rotor hum is stationary, so an event split across a
  window boundary still fills both halves. It would not be free for sheila,
  where a 400 ms wake word cut in two is lost; sheila keeps `NPHASE=2`.
* **`tie=2`** shares one template row between each pair of frames: 8 distinct
  rows per unit instead of 16, i.e. 84 ms of template time resolution instead
  of 42 ms. **No RTL parameter changes** — the header still holds 16 rows, in
  equal pairs, and the shipped silicon reads it unmodified. The saving is
  yosys folding the duplicated rows out of the row-selection logic (§2–3).

If the tile ever needs more than this, the best value on the whole frontier is
**`NHID=2` together with `NPHASE=1`**: 3 379 µm² and 38 flip-flops for 1.31
points (2 580 µm²/pt, better than `NHID=2` alone at 2 110), landing at 81.9 %
estimated core. After that, `NBAND` 5→4 (865 µm² for 0.84) and `STATE_W` 10→9
(536 for 0.93). Do **not** spend it on `NFRAME`, `HACC_W`, `MANT`, `tie=16` or
`NHID=1`.

Not worth doing at all:

* **weight-level pruning** (`wt`, `l1`) — free, but buys nothing (§2);
* **`HOLD_FRAMES`** — 16→1 is −82 µm², inside the noise, and **zero** flops,
  because `hold` was declared `[FIDX_W:0]` and sized by `NFRAME` rather than by
  the parameter. **This entry found the right fact and drew the wrong
  conclusion.** A register too narrow to hold `HOLD_FRAMES` does not merely make
  the parameter free to change — on the wake word (`NFRAME=8`, so 4 bits) it
  made `4'(16)` truncate to zero, and the LED never lit at all. `hold` is now
  sized `$clog2(HOLD_FRAMES+1)`; see [hold_width.md](hold_width.md). The area
  numbers above stand, and the drone's 16→2 now ships for output latency rather
  than for area: −209 µm² and −28 instances, in `hardened/drone_4`.
  `HOLD_FRAMES=1` remains unusable — the load and the decrement fall in the same
  frame, so it blinks for a few hundred clocks and is never seen;
* **`SCORE_W` 9 or 8 and `DEBUG_PINS=0`** — measured at or above the shipped
  area (§4, and [nn_optimization.md](nn_optimization.md) for `DEBUG_PINS`).

## 7. What is still owed before this ships

The recommendation rests on 250-epoch rankings and synthesised area. It is not
a tape-out claim until:

1. `scripts/finalise_task.sh` trains `NPHASE=1 tie=2` at **1000 epochs over 8
   seeds**, selects on validation, and `eval_header.py` reproduces the training
   AUC through the independent integer chip model — the check that the export
   is bit-exact;
2. that final header is synthesised at its own geometry (`area_gate.py
   --header`); the table in §6 used a short-budget header, whose *constants*
   are what the area depends on, but the shipped one has to be re-measured;
3. `harden_local.sh` places it — utilisation, timing at all three corners,
   DRC/LVS, and `power__total` against drone_2's **4.76 mW**. Expect the area
   to land and the power to move little: the cascade runs 9 cycles every 640 ns
   while the classifier runs 8 cycles every 41.94 ms, so switching is dominated
   by the front end and the clock tree, which neither `NPHASE` nor `NHID`
   touches. If that prediction is wrong, this section will say so;
4. cocotb passes at `FRAME_LOG2=8`, `16` and gate level, with
   `test/test.py:test_cfg()`'s drone branch updated for `NPHASE=1`.

`NPHASE` also has to move into the `WW_WEIGHTS_DRONE` `ifdef`, as `TAP0`,
`NBAND` and `NFRAME` already have: the two builds would no longer share it.

## 8. What is not established

* **Verdict rate is not in the metric.** AUC scores the maximum over a clip's
  windows, so it is blind to `NPHASE=1` halving the decisions per second.
  Nothing here measures detections per second on a passing drone, or false
  alarms per hour on continuous ambient audio — the same gap
  [DRONE.md](DRONE.md) §7 records for the shipped part.
* **The area noise floor is ±55 µm² at best** and ±160 µm² at some densities.
  Every claim under ~200 µm² here is noise, which is why `HOLD_FRAMES` and
  weight pruning are reported as nothing.
* **The row-selection mechanism of §2 is inferred, not proven.** Three
  consistent observations support it — area peaks at intermediate density, real
  templates beat random ones of equal density by 400 µm², identical rows save
  780 µm² — but no netlist-level attribution was done.
* **Front-end prunes were trained on one extraction each, at one recipe.**
  `NBAND=4` losing only 0.84 makes it the most interesting unexplored
  direction — it frees the whole 3.9–7.8 kHz octave the drone barely uses — but
  it was not tuned, combined with `NPHASE=1`, or hardened.
* **`FRAME_LOG2` was not swept.** If verdict *latency* rather than area is the
  constraint, that is the knob (15 → 21 ms frames, a 336 ms window, verdicts
  every 168 ms at the same 16 rows), and it is untested at `pdm_gain=2.0`.
