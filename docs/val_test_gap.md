# Why validation and test disagree

Selection has always been on validation AUC, with test recorded and never used
to choose. That discipline only pays off if validation predicts test. Across
the ten detectors it sometimes does and sometimes does not, and the spread is
wide enough that "the model overfits" cannot be the whole story.

All figures below are the `epochs=1000` row at `pdm_gain=2.0`, 6 seeds,
mean over seeds.

| task | val | test | gap |
|---|---|---|---|
| mosquito | 87.77 | 65.07 | **+22.70** |
| vad | 85.86 | 69.26 | +16.61 |
| siren | 92.92 | 83.63 | +9.29 |
| clap | 88.07 | 79.02 | +9.05 |
| water | 80.01 | 71.35 | +8.66 |
| catmeow | 94.35 | 86.63 | +7.71 |
| drone (dads) | 99.02 | 98.76 | +0.26 |
| babycry | 82.24 | 82.16 | +0.08 |
| dogbark | 82.29 | 85.15 | **−2.87** |

A 25-point spread rules out any mechanism that acts the same way on every
task. Three candidates were separated before spending compute.

## 1. The metric inflates validation — real, but small

`qat.run` evaluates every `eval_every` epochs and keeps the **argmax-val**
checkpoint (`train/optim/qat.py:394`). At `epochs=1000, eval_every=5` the
reported validation AUC is therefore a *maximum over 200 correlated draws*,
while test is read once, at whichever checkpoint that maximum happened to
pick. Some of every gap is manufactured by the metric rather than by the data.

That predicts something checkable: more draws should mean more inflation, so
**the gap should grow with the epoch budget**. It does not.

| task | gap @400 | @1000 | @2000 |
|---|---|---|---|
| catmeow | +11.57 | +11.94 | +11.28 |
| clap | +16.08 | +15.96 | +15.78 |
| dogbark | −0.09 | +0.99 | +0.63 |
| babycry | +3.16 | +3.12 | +3.39 |

Between 80 and 400 draws the gap moves by fractions of a point and in no
consistent direction. The inflation is real but is not what separates
mosquito's +22.70 from dogbark's −2.87.

`qat.py` now records `val_final` (the last checkpoint's val AUC, which nothing
selected) and `best_epoch` alongside `val_auc`, so future runs bound this
directly instead of inferring it.

## 2. The splits are out-of-domain by construction — this is the real cause

`train/tasks.py` states the rule: splits are disjoint at the recording level,
using each corpus' own official split. That makes test a genuinely different
distribution, not a random holdout, and the amount of shift differs per corpus.

**FSD50K** (six of the eight tasks). `eval` becomes the test split and is
uploader-disjoint from `dev`; validation is carved out of `dev`
(`train/fsd50k.py:58-88`), and val items sharing an uploader with train are
pushed back into train. So validation is uploader-disjoint from train but
still drawn from the *dev* pool that train comes from. Test is a different
pool, annotated to a different standard.

**HumBugDB** (mosquito) is the extreme, and explains the worst number in the
table. The official split is by site:

    Test A = country == Tanzania and location_type == field
    Test B = country == UK       and location_type == culture

Validation, however, is carved from the *remaining train sites*
(`train/humbug.py:119-124`), grouped by `experiment()` — the tuple
`country|place|location_type|device_type|name`, where `name` is the source
recording. Validation therefore differs from training only in **which
recording** a clip was cut from. It shares every country, site and device.

So mosquito's validation asks "does this generalise to another recording from
the same hut, on the same phone?" and its test asks "does this generalise to
another country, on other hardware?". The +22.70 is the detector reading site
and device signature. Validation structurally cannot see that, and no amount
of tuning against validation will reveal it.

Test composition compounds it: mosquito test is 5 660 positive against 640
negative (90 % positive) versus validation's 77 %, and Test A alone is 1 153
mosquito against 10 background.

The two detectors that actually tape out sit at the other end. The drone's gap
is +0.26 and sheila's is negative — DADS splits are recording-disjoint within
one corpus, and Speech Commands has a genuine official split, so neither
carries a cross-corpus or cross-site jump.

## 3. Does validation still *rank* correctly?

A constant offset is harmless if the ordering survives, since only the ordering
drives selection. Spearman correlation between `val_mean` and `test_mean` over
every archived grid row with ≥4 seeds:

| task | rows | ρ(val, test) |
|---|---|---|
| drone (dads) | 13 | 0.973 |
| sheila | 146 | 0.963 |
| babycry | 58 | 0.855 |
| catmeow | 30 | 0.687 |
| clap | 30 | 0.678 |
| dogbark | 29 | 0.488 |

For the two shipped parts the ordering is nearly perfect: the long architecture
search was not chasing validation noise. It degrades on the newer tasks, and
dogbark at 0.488 means roughly half its ranking is noise — a caution against
reading small per-task wins there as real.

## What was refuted

**Early stopping as a shorter budget.** Cutting to 400 epochs loses test AUC in
three of four tasks against 1000 (catmeow −0.25, clap −0.12, babycry −0.13,
dogbark +0.80); 2000 epochs *wins* in three of four (+0.92, +1.12, +0.62).
Training longer is mildly better and the gap is flat, so there is nothing to
buy here. Note that early stopping in the sense of *best-checkpoint selection*
is already implemented and is doing its job — what is refuted is truncating the
budget on top of it.

**`aug_band` as a device-response proxy.** The hypothesis was that per-band ±1
level jitter stands in for a different microphone's frequency response, and
should therefore help most where the shift is a device change. It is the worst
of 36 rows in three of four tasks: clap test −3.64, babycry −3.92, dogbark
−1.32, and it *widens* the gap (dogbark +1.94, clap +1.62). Predicted the
opposite sign; the story was wrong.

## What the archive points at instead

Mined from the round-6 rows, ranked by gap reduction rather than by validation
AUC — which is what that round was ranked on, and why these were missed:

| knob | task | Δgap | Δtest |
|---|---|---|---|
| `aug_time=2` | catmeow | −2.13 | **+2.51** |
| `lr_w1=0.04` | clap | −2.70 | **+2.70** |
| `aug_time=1` | catmeow | −1.01 | +1.46 |
| `wd=0.0001` | clap | −1.33 | +1.18 |
| `leak=0.2` | dogbark | −1.08 | +0.90 |

`aug_time` jitters the window start, which attacks alignment dependence rather
than any domain cue, and it is the best single knob found. All of these predate
`pdm_gain=2.0`, so `scripts/valtest.sh` re-tests them at the corrected drive
and extends them to mosquito and vad, which round 6 never covered.

## What cannot be fixed, and what can

The gap is mostly an honest measurement of deployment generalisation. Shrinking
it by making test easier would be self-deception; the number is the point.

What *can* be fixed is that validation currently measures a different question
from test. For mosquito the fix is concrete: carve validation from a held-out
**site** rather than a held-out recording, so it asks the cross-site question
test asks. `train/humbug.py:64-67` documents why it was not done that way —
grouping by site alone leaves eight groups and a validation split with almost
no background recordings. A middle option is to hold out one site that carries
both classes. That change would make validation predictive and make selection
optimise cross-site generalisation, at the cost of breaking comparability with
every round measured so far.

Not yet attempted.
