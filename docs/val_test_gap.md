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

## The epoch budget is not training length

`best_epoch`, once recorded, shows the winning checkpoint moving with the
budget rather than sitting at a fixed number of epochs:

| budget | best epoch | ratio |
|---|---|---|
| 250 | 193 | 0.77 |
| 1000 | ~610 | 0.61 |
| 2000 | 990 | 0.50 |
| 4000 | 1974 | 0.49 |

The cause is `qat.py:323`: `CosineAnnealingLR(opt, c.epochs)`. The learning-rate
schedule is *defined by* the epoch budget, so `epochs` sets the annealing rate
and the duration together. `epochs=250` does not mean "stop early", it means
"anneal four times faster and then stop".

Every epoch sweep in this project -- round 6's `epochs=400,2000`, and the
`250/2000/4000` arm here -- has therefore measured the two confounded. The
conclusion those sweeps support is only that *the schedule length* wants to be
around 1000-2000; they say nothing clean about stopping early, because no run
ever trained past its own annealing horizon.

A decoupled test needs the scheduler length pinned while the stopping point
moves -- `sched=const` makes `epochs` pure duration, and round 6 already swept
it. Until that is done, "early stopping is refuted" should be read narrowly: a
*shorter cosine schedule* is worse, which is a different claim.

## Early stopping: we do it, and no better rule exists from validation

`qat.run` keeps the argmax-validation checkpoint and restores it (`qat.py:394`,
`400-403`), so the model returned is the best one seen rather than the last.
That is early stopping in the only sense that changes accuracy; patience-based
termination would save compute and nothing else, since stopping sooner cannot
beat keeping the best of everything already seen.

There is nonetheless real headroom, because **test peaks about half as early as
validation**. `train/optim/stoprule.py` records both curves at every checkpoint
and scores five rules, three seeds, `epochs=1000` (test AUC at the checkpoint
each rule keeps):

| task | gap | argmax_val | first_99 | first_995 | half_budget | *oracle* |
|---|---|---|---|---|---|---|
| mosquito | +22.7 | 64.36 | +2.31 | +1.08 | +2.52 | *+4.93* |
| siren | +9.3 | 83.76 | +0.64 | 0.00 | +1.14 | *+2.54* |
| clap | +9.1 | 79.64 | −7.92 | −1.36 | −3.30 | *+4.22* |
| water | +8.7 | 70.44 | −0.08 | 0.00 | +0.48 | *+3.16* |
| catmeow | +7.7 | 86.29 | −1.35 | +1.00 | +0.23 | *+1.83* |
| babycry | +0.1 | 82.44 | +0.19 | −0.27 | +0.24 | *+0.99* |
| dogbark | −2.9 | 85.04 | −0.26 | −0.11 | +0.20 | *+0.57* |
| **mean** | | — | **−0.93** | **+0.05** | **+0.21** | ***+2.61*** |

The oracle beats current practice on all seven tasks by 2.61 on average, and
its margin tracks the domain shift: +4.93 and +4.22 where the gap is largest,
+0.57 and +0.99 where it is smallest. On mosquito test peaks at epoch 277 while
validation climbs to 630.

No usable rule captures it. `first_995` (+0.05) and `half_budget` (+0.21) are
indistinguishable from zero, and `first_99` is harmful (−0.93). `half_budget`
gains on six of seven and only clap's −3.30 drags its mean from +0.80 to +0.21
-- but choosing a rule per task by consulting test is the leakage the selection
discipline exists to prevent, so that +0.80 is not bankable.

Stopping on the honest signal does not help either. `Cfg.select="nosil"` picks
the checkpoint on validation with the synthetic negatives dropped; the two
criteria correlate 0.87-0.94 across checkpoints and choose the **identical**
epoch on every task tried. The mixture confound inflates the reported number
but never corrupted checkpoint selection -- which is worth knowing on its own,
because it means prior selection work was not damaged by it.

For mosquito, `corr(val_nosil, test_real)` across checkpoints is **+0.083**:
its validation carries essentially no information about honest test performance
at any point in training. That is the same in-domain problem as section 2,
reached from a third direction, and it is why the 2.61 points are reachable
only by making validation resemble test.

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

## Method fixes this produced

Three of the findings above were only visible because a measurement was wrong
first, so each one closed with a change to the tooling rather than a note.

**The baseline could be the wrong row.** `qat.Cfg.key()` diffs a config against
the *dataclass defaults*, not against the sweep's `--base`. Sweeping
`epochs=250` under `--base '{"epochs": 1000}'` therefore yields the key
`{"nframe": 8}` -- exactly what a baseline that never set `epochs` produces.
`grid.py` additionally skips any (config, seed-set) already on file, so the
real baseline was skipped as already-done and the sweep's own rows contained no
baseline at all. Differencing against the shortest key then compared every
variant against `epochs=250`, turning an `aug_time=3` result worth +0.93 into
+2.65 and making nine of ten variants look like wins when most were noise.
`grid.py` now stamps `base_key` on every row and prints where the baseline came
from when it declines to re-run it, and `train/optim/compare.py` resolves the
baseline over the whole file -- never the `--note` subset, since that is
precisely where a deduplicated baseline will not be -- and **exits rather than
guessing** when it cannot resolve one.

**Reports showed the inflated numbers.** `grid.py --report` now prints the gap
and, where the rows carry them, the real-audio pair, and accepts
`--sort val_nosil_mean` so a search can rank on validation with the synthetic
negatives removed.

**The trainer discarded its own diagnostics.** `run_seeds` dropped `best_epoch`
and never computed a val figure that selection had not already maximised. It
now reports `val_final`, `best_epoch_mean`, `val_nosil_mean` and
`test_nosil_mean`. The first two are what exposed the schedule confound above;
the last two are what showed mosquito to be below chance.

## The sweep's verdict: adopt nothing

13 configs x 6 seeds on four tasks spanning the whole range, at `pdm_gain=2.0`,
read through `train/optim/compare.py` against the correct baseline. Best result
per task:

| task | best Δtest | config |
|---|---|---|
| mosquito | +1.94 | wd=0.0001 |
| clap | +0.93 | aug_time=3 |
| vad | +0.45 | epochs=4000 |
| dogbark | +0.35 | leak=0.2 |

Small, and each task's winner is a different knob. But the decisive figure is
what the **selection discipline** would actually have bought, taking each
task's best-by-validation row:

| task | val-selected | Δval | Δtest |
|---|---|---|---|
| clap | wd=0.001 | +0.79 | +0.30 |
| dogbark | aug_time=1 | +0.71 | −0.07 |
| vad | lr_w1=0.16 | +1.00 | −0.10 |
| mosquito | epochs=4000 | +1.98 | **−2.33** |
| **mean** | | **+1.12** | **−0.55** |

Adopting the winners would gain 1.12 validation and **lose 0.55 test**. The
sweep is not merely null; selecting from it honestly is harmful, so the right
action is to adopt none of it. On mosquito the effect is stark enough to read
off one table: every config that gains test loses validation, and the
best-validation config has the worst test.

`aug_time`, which looked like the one survivor, does not survive either: +0.93
on clap, −0.11 on mosquito, −0.07 on dogbark, +0.06 on vad. One task in four.

That also closes the epoch question from the last direction. `epochs=250` helps
mosquito (+1.21) and vad (+0.16), hurts clap (−1.72) and dogbark (−0.34), mean
−0.17. The benefit tracks how badly a task overfits -- but no fixed budget
wins, and validation cannot say which regime a task is in.

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
