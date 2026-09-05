# Can `babycry` reach 90 %?

`babycry` detects "an infant is crying nearby" on the TinyTapeout 1×1 chip -- a
cry against almost any other sound, the confusable human vocalisations having
been excluded from the data rather than used as negatives (see below). It
sits at **78.54 % test AUC** after the search in
[task_optimization.md](task_optimization.md). This asks what it would take to
reach 90 %, and answers it.

**Short answer: not on this chip — but the task itself supports it.** A 16-band
mel spectrogram of the same clips reaches **94.7 %**, and even a 4-unit
classifier on mel features reaches 92.3. So the data, the labels and the
question are all fine; the dyadic front end costs about 14 points and nothing
affordable recovers them.

That is a different claim from "90 % is unreachable", which is what an earlier
revision of this document said. The detailed answer is below, because the
*shape* of the limit decides what is worth trying next.

## Where the 11.5 points would have to come from

Three places, and only one of them is large enough to matter.

| | measured | headroom to 90 |
|---|---|---|
| the classifier | chip 78.54, fp32 ceiling on the same features **80.98** | **2.4** |
| the front end | best affordable variant is what already ships | ~0 |
| the data / the question | — | everything else |

The fp32 ceiling is the number that governs. It is an unconstrained MLP-256 on
exactly the features the chip sees, at the geometry it ships, selected on
validation: **82.81 val / 80.98 test**. To reach 90 % on test the *ceiling*
would have to rise by nine points. No classifier change can do that, and the
chip is already within 2.4 of it.

## Round 1 — the cheap levers, all of them

### Window length (`NFRAME`, `NPHASE=2`, `STATE_W=9` features)

| `NFRAME` | span | mlp4 (predicts the chip) | mlp256 (the ceiling) |
|---:|---:|---|---|
| 4 | 168 ms | 77.91 / 76.37 | 81.27 / 80.40 |
| **8** | **335 ms** | **78.81 / 77.84** | **82.81 / 80.98** |
| 16 | 671 ms | 78.60 / 77.70 | 81.80 / 81.43 |
| 24 | 1006 ms | **79.90 / 79.92** | **83.77 / 83.16** |

`NFRAME=24` is the best window by 1.1 val and 2.1 test at the rung that
predicts the chip — **and it is not legal hardware.** `NFRAME` must be a power
of two: `frame_idx = cnt[CNT_W-1:FRAME_LOG2]` wraps at `2^FIDX_W`, not at
`NFRAME`. The legal neighbours do not deliver it: 16 (671 ms) scores no better
than 8, and 32 (1.34 s) needs four times the weight ROM and 2 s of clip.

The `mlp4` rung is used deliberately here rather than `mlp256`: round 24 of
[task_optimization.md](task_optimization.md) established that an unquantised
probe read at a width far above `NHID=4` overstates what the hardware can use,
by as much as 7 AUC.

### Band layout (`NBAND=6`, `STATE_W=9`, mlp256 val at `NFRAME=8`)

| variant | bands | val | test |
|---|---|---:|---:|
| **base** `TAP0=3` | 243 Hz – 15.5 kHz | **82.81** | 80.98 |
| `t2` `TAP0=2` | 486 Hz – 31 kHz | 82.59 | 80.55 |
| `t5` `TAP0=5` | 61 Hz – 3.9 kHz | 82.47 | 80.93 |
| `t4` `TAP0=4` | 121 Hz – 7.8 kHz | 82.24 | 80.47 |
| `k3` `K_SHIFT=3` | 121 Hz – 7.8 kHz | 77.34 | 75.17 |

**The shipped layout is already the best of the five.** Nothing here is worth
an area gate. `k3` is much worse, which is informative: moving the whole comb
down an octave costs 5.5 points, so the cry's high harmonics carry real weight
and the intuition that an infant cry "lives at 300–600 Hz" is wrong for this
detector.

## Round 1b — is the 78.54 even real?

`train/tasks.py` warns in its own docstring that a detector "could score well
by recognising the corpus instead of the sound", and `babycry` is the one task
that cannot obey its rule: **85 % of its positives are `donateacry`, which is
100 % cry and supplies no negatives**, while essentially all negatives are
FSD50K and ESC-50. In the test split that is 684 of 968 positive clips against
zero matching negatives.

`train/optim/corpus_audit.py` rebuilds the group hashes in the npz, maps every
clip back to its corpus, and slices the AUC:

| comparison | n_pos | n_neg | AUC |
|---|---:|---:|---:|
| overall — what the pipeline reports | 968 | 975 | **78.54** |
| **matched: fsd50k pos vs fsd50k neg** | 236 | 654 | **77.63** |
| matched: esc50 pos vs esc50 neg | 48 | 119 | 70.05 |
| crossed: donateacry pos vs all others | 684 | 975 | 77.42 |
| others only, donateacry dropped | 284 | 975 | 81.25 |

**No corpus shortcut exists.** The corpus-matched number (77.63) is within a
point of the headline, and the crossed comparison — the one a corpus cue would
inflate — is *lower* than the headline, not higher. The 78.54 is genuine cry
detection. This was worth checking and the suspicion was wrong.

One incidental finding: `donateacry` cries are the *harder* ones. Dropping them
raises the AUC to 81.25.

## What the task actually asks

The second set in each `tasks.py` entry is the **ambiguous** set, not the
negatives — `_top_up` calls `fsd50k.negatives(p, a, want)` with `p, a =
FSD[task]`, and `split_rows` documents its return as *"(positives, usable
negatives) — ambiguous rows belong to neither"*. So:

| role | FSD50K | ESC-50 |
|---|---|---|
| **positive** | `Crying_and_sobbing` | `crying_baby` |
| **excluded entirely** | `Screaming, Yell, Shout, Child_speech_and_kid_speaking, Laughter, Giggle, Chuckle_and_chortle, Human_voice, Sigh, Gasp, Whispering, Chatter, Crowd, Cheering, Human_group_actions` | `laughing, sneezing, coughing, breathing, snoring` |
| **negative** | the rest of the corpus, a **44 457-clip pool** over ~180 classes | the other 44 classes, 1 760 clips |

The arithmetic confirms it: ESC-50 is 2 000 clips, 40 positive, 5 × 40
ambiguous, and the reported pool is 1 760 = 2000 − 40 − 200.

**`babycry` is therefore "cry vs almost anything"** — dogs, engines, doors,
music, water, rain — with the confusable human vocalisations *removed from the
dataset* rather than used as hard negatives. That is the easier of the two
framings, and it is already the one in use.

An earlier revision of this document said the opposite: that the negatives were
the human-vocalisation set, that the task was deliberately adversarial, and
that widening the negatives was the only route to 90 %. All three were wrong.
There is no easier framing left to adopt, and dropping the ambiguous exclusion
would make the task **harder**, not easier.

## Rounds 4-7 — changing the front end itself

Rounds 1-3 moved parameters *inside* the dyadic cascade. The cascade forces one
property no parameter can touch: the bands are exactly one octave wide, because
stage b runs at half the rate of stage b-1 and a band is the difference between
neighbours. `train/optim/fe_alt.py` builds front ends the chip does not have,
in the same feature format, so the same validation-selected ladder scores them.

**First, the bound.** Test AUC, `mlp256`, `NFRAME=8`:

| front end | float | through a 4-bit log |
|---|---:|---:|
| `mel40` — 40 mel bands | 94.93 | 84.85 |
| `mel16` | **94.73** | 84.48 |
| `mel8` | 92.60 | 84.83 |
| `half` — half-octave, 12 bands | 90.83 | — |
| `oct1` — **the chip's own filterbank** | 89.89 | 86.44 |
| `oct2` — 12 dB/oct skirts, ~free | 89.47 | 86.56 |
| the actual chip | — | **80.98** |

**90 % is achievable on this task.** `mel16` reaches 94.7, and even a 4-unit
classifier on mel features gets 92.3 — above target. The task, the data and the
labels are not the limit. The front end is, and it is costing about 14 points.

Two ideas die here. **`oct2` is not better than `oct1`** (89.47 vs 89.89
float): a second-order difference of the same states, which would have cost one
subtract and no new state, buys nothing. **`half` buys ~1 point** for double
the cascade state. And band count saturates early — `mel8`→`mel16` is +2.1,
`mel16`→`mel40` is +0.2 — so six bands is not what starves this detector.

**Second, where the 14 points actually are.** Adding one impairment at a time
to `oct1`, on PCM:

| | test | attributable loss |
|---|---:|---:|
| `oct1` float | 89.89 | — |
| + 4-bit log readout | 86.44 | 3.5 |
| + integer cascade, `in_amp=512` | 86.31 | ~0 |
| + integer cascade, **`in_amp=64`** (the chip's) | **81.39** | **4.9** |

The last row reproduces the real chip (80.98) to within half a point, so the
model is faithful *at that operating point*. The 4.9 points are truncation in
`(x - state) >> K`: three more bits of scale recover essentially all of it.

`IN_AMP = 1 << (STATE_W - 3)`, so `in_amp=512` is `STATE_W=12` — a parameter
tried only at 9 and 10 in this project. That looked like the largest
unexploited lever in the design.

**It is not. The gain does not survive real PDM input.** Running the actual
extractor rather than the PCM model:

| `STATE_W` (real PDM) | val | test |
|---:|---:|---:|
| **9 — what ships** | **82.81** | **80.98** |
| 10 | 77.85 | 76.60 |
| 11 | 80.25 | 78.34 |
| 12 | 79.96 | 78.68 |

Every value above 9 is worse, and the curve is not even monotonic. With
`MANT=1` each `STATE_W` step doubles the band magnitudes and shifts every
feature by exactly two levels on the 16-level log grid, changing how the
distribution sits against the clamp and the zero floor; and with a 1-bit input
the cascade's truncation error plausibly acts as dither that the coarse log
readout benefits from. Arithmetic precision improves monotonically with
`STATE_W`; the *feature encoding* does not.

**The methodological lesson.** The PCM model agreed with the chip to 0.4 at
`in_amp=64` and I took that as validation, then trusted its derivative along a
new axis. Agreement at one operating point says nothing about the gradient. The
model was never wrong about PCM — it was answering a question about a different
input.

## What would actually be needed

The limit is the front end, and now it is the front end without an excuse.
Six octave-wide bands at 3 dB resolution, 42 ms frames and a 335 ms window
cannot separate an infant cry from general audio better than **80.98 %**, and
that is measured with an unconstrained float model 60× the size. The chip gets
78.54 of the available 80.98.

There is also irreducible label noise: FSD50K's vocabulary has exactly one
relevant label, `Crying_and_sobbing`, with no separate infant class, so 42 of
the 164 positive test recordings are "crying and sobbing" that may be adult.

Reaching 90 % needs a representation that resolves what a cry actually is: a
pitched, strongly modulated voice with a 300–600 Hz fundamental and a
characteristic rise-fall melody. Octave-wide bands smear the fundamental and
its harmonics into the same bin; a per-frame maximum over 42 ms discards the
melody. Neither is fixable inside 22 150 µm² — `NBAND=7` alone is 1 620 µm²
over budget.
