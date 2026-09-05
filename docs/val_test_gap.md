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

## 3. Validation and test are not the same mixture

Two components of the negative class are **fixed in absolute size**, so they
occupy a much larger share of the smaller split. Both make validation easier
than test in a way that has nothing to do with the detector.

**Synthetic silence.** `extract_clips.py:95-104` adds `--silence 2000`
room-tone negatives and assigns them with `clipset.hash_split`, roughly
10/10/80. About 202 therefore land in validation and 202 in test *regardless of
how big either split is*:

| task | silence % of val neg | of test neg | ratio | gap |
|---|---|---|---|---|
| vad | 26.6 | 3.8 | **7.0×** | +16.61 |
| water | 34.5 | 9.0 | 3.8× | +8.66 |
| clap | 47.5 | 18.1 | 2.6× | +9.05 |
| catmeow | **67.6** | 33.6 | 2.0× | +7.71 |
| siren | 27.4 | 21.7 | 1.3× | +9.29 |
| dogbark | 22.8 | 15.2 | 1.5× | −2.87 |
| babycry | 23.9 | 20.7 | 1.2× | +0.08 |
| mosquito | 20.3 | 31.6 | 0.6× | +22.70 |

Two thirds of catmeow's validation negatives are synthetic room tone, against a
third of its test negatives. Its 94.35 validation AUC is substantially
measuring "cat versus silence" while its 86.63 test AUC measures "cat versus
real sound". vad is the starkest ratio at 7×, and it has the second-largest
gap.

**ESC-50.** The same shape for a different reason: ESC-50 ships 40 clips per
class over 5 folds, so it contributes an almost constant 48 positives to every
split while FSD50K scales. ESC-50 positives are 52.2 % of catmeow's validation
positives but 12.3 % of its test positives; 26.1 % against 4.4 % for clap.
ESC-50 is clean curated single-source audio, so this pushes the same way.

Neither is the whole story — the ranking is not monotonic (siren has a high gap
at low enrichment; dogbark a negative gap at 1.5×), and **mosquito runs the
other way entirely**, with more silence in test than validation and still the
largest gap. That is independent confirmation that mosquito's gap is site
shift and not mixture.

But it means part of every reported validation number is a mixture artefact,
and it is the part that is straightforwardly fixable: scale the silence count
per split instead of hash-splitting a fixed 2 000, or report validation AUC
with the synthetic negatives excluded.

### Measured

AUC decomposes exactly over a partition of the negative class, since it is a
Mann-Whitney statistic and the positives are shared:

    AUC = f * AUC_silence + (1 - f) * AUC_real

Setting `AUC_silence = 1.0` predicts what the real-audio AUC must be. `qat.py`
now measures it directly as `val_nosil` / `test_nosil`, and the two agree to
within seed noise -- mosquito's validation was predicted at 84.63 and measured
at 84.65 -- so the synthetic negatives are indeed almost perfectly separable
and the arithmetic can be trusted.

Two seeds, `epochs=1000`, `pdm_gain=2.0`:

| task | val | val real | test | test real | gap | gap real |
|---|---|---|---|---|---|---|
| mosquito | 87.75 | 84.65 | 63.73 | **47.35** | +24.02 | **+37.30** |
| catmeow | 94.36 | **82.62** | 85.76 | 78.57 | +8.60 | +4.05 |
| water | 80.45 | **70.40** | 70.68 | 67.81 | +9.77 | +2.59 |

Three things follow.

**The mosquito detector does not transfer.** Its real-audio test AUC is 47.35,
below chance. The reported ~64 is carried entirely by the synthetic negatives;
against real audio from an unseen site the detector has no discriminative
power at all. This is the same conclusion section 2 reached from the split
construction, now measured rather than inferred, and it is not something any
free knob can repair.

**Reported validation is inflated wherever silence is a large share.**
Catmeow's headline 94.35 is 82.62 on real audio -- 11.7 points of it is "cat
versus silence" -- and water's 80.45 is 70.40. These are the numbers that have
been driving selection and filling every summary table.

**Most of catmeow's and water's gap was never real.** 8.60 becomes 4.05 and
9.77 becomes 2.59. Those two generalise considerably better than the headline
suggested; only mosquito gets worse under an honest accounting.

`best_epoch` from the same runs: 572 (mosquito), 440 (catmeow), 150 (water) out
of 1 000. The winning checkpoint arrives early -- water uses 15 % of its budget
-- which is independent confirmation that the later epochs add nothing and that
best-checkpoint restore is already doing what early stopping would.

## 4. Does validation still *rank* correctly?

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
