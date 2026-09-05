# Can `babycry` reach 90 %?

`babycry` detects "an infant is crying nearby" on the TinyTapeout 1×1 chip. It
sits at **78.54 % test AUC** after the search in
[task_optimization.md](task_optimization.md). This asks what it would take to
reach 90 %, and answers it.

**Short answer: no, and the reason is the front end, not the model.** The
detailed answer is below, because the *shape* of the limit decides what is
worth trying next.

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

## What would actually be needed

The question `babycry` asks is harder than it looks. Its negatives are not
"any other sound" — they are `Screaming, Yell, Shout, Child_speech, Laughter,
Giggle, Human_voice, Sigh, Gasp, Whispering, Chatter, Crowd, Cheering` plus
ESC-50's `laughing, sneezing, coughing, breathing, snoring`. It is
**infant cry against other human vocalisation**, deliberately adversarial, and
that is the right question for a detector meant to sit in a room with people.

Separating those two classes through six octave-wide bands at 3 dB resolution,
42 ms frames and a 335 ms window is close to what the representation supports.
An unconstrained float model with 60× the parameters gets 80.98.

There is also irreducible label noise: FSD50K's vocabulary has exactly one
relevant label, `Crying_and_sobbing`, with no separate infant class, so 42 of
the 164 positive test recordings are "crying and sobbing" that may be adult.

**90 % is reachable only by changing the question** — widening the negatives
from "other human vocalisation" to "anything at all". That would raise the
number substantially and would not make the detector better; it would make it
answer an easier question. Recorded here as an option, not taken.
