# Robustness of the sheila binary detector

Everything below is measured on `src/ww_weights.svh` (the `nn_optim` header,
`TAP0=3 NBAND=6 NFRAME=8`, test AUC 93.67 %) scored through the bit-exact
integer chip model over `artifacts/ww_feats_sheila_nb6.npz`, plus two new
measurements that needed the raw audio: continuous non-speech, and real input
gain. Every number is reproducible from `train/eval_header.py`'s primitives.

**Verdict in one line: the discrimination number is real and holds up under a
proper confidence interval, but there is no threshold at which this behaves
like a wake-word detector, and the dominant failure mode is input level.**

---

## 1. Is 93.67 % real?

Yes, with a much wider error bar than the ±0.24 seed spread suggests.

| | |
|---|---:|
| test AUC, all rows | 93.67 % |
| unique clips only (jitter copies removed) | 93.95 % |
| bootstrap 95 % CI, clip-level resample | **92.18 – 95.63** |
| equal-error rate (clip level, t = 5) | 14.4 % |

The test split reports 768 positives, but those are **192 unique clips × 4
time-jittered copies** of the same recordings — the augmentation is in the test
set, not just the training set. The effective positive sample is 192 clips from
**141 speakers**; negatives are 504 clips from 177 speakers across 34 words.
So the honest interval is ±1.7 points, not ±0.2. The +4.5-point gain over the
shipped 89.14 % still clears it, but "93.7 %" should be quoted as "94 ± 2".

The ±0.24 figure in `nn_optimization.md §4.6` is the spread of *training seeds*
on a fixed validation set. It measures optimisation noise. It is not a
generalisation error bar and should not be read as one.

## 2. The operating point — the real problem

Score is an integer in −4..19 over 5 window positions per second.

| threshold | recall | neg-clip FPR | neg-window FPR | FA/hour on word-dense speech |
|---:|---:|---:|---:|---:|
| 4 | 89.7 % | 15.7 % | 4.13 % | 886 |
| 6 | 76.6 % | 7.7 % | 1.87 % | 400 |
| 8 | 58.7 % | 3.2 % | 0.75 % | 162 |
| 10 | 38.7 % | 1.4 % | 0.32 % | 68 |
| **12 (shipped)** | **21.5 %** | **0.4 %** | **0.08 %** | **17** |
| 14 | 9.2 % | 0.2 % | 0.04 % | 9 |

There is no row where both columns are acceptable. At the shipped threshold the
chip misses four out of five utterances of its own wake word. At 90 % recall it
fires roughly every four seconds while someone is talking.

The 7-bit trim subtracts 0..127, so thresholds −115..12 are all reachable — the
trim is **not** the limiting factor. The ROC is.

**Per speaker, at the shipped threshold: 83 of 141 test speakers never fire at
all, 5 always fire.** The mean per-speaker recall is 20.7 %. This is not a
detector that works a bit for everyone; it works for a minority of voices and
not at all for most.

### Persistence does not rescue it

A k-of-N window vote is the standard cheap fix. Measured, it is strictly worse
than lowering the threshold:

| rule | recall | FA/hour |
|---|---:|---:|
| k=1, t=8 | 58.7 % | 114 |
| k=2, t=4 | 47.5 % | 143 |
| k=3, t=4 | 4.4 % | 29 |

The reason is structural: "sheila" is ~0.5 s and the window hop is 168 ms, so
only one window ever really contains the word. Median gap between a positive's
best and second-best window is 4 score points, and 83 % of positives have a
unique best window. Max-over-windows is already the right pooling; there is no
redundancy left to vote over.

## 3. Continuous non-speech — the measurement `DESIGN.md §10` says is missing

6.2 minutes of Speech Commands `_background_noise_` (dishes, a cat, an exercise
bike, a running tap, pink and white noise), streamed at the hardware hop,
2 146 windows per level.

| threshold | FA/hour @ peak 0.7 | @ −6 dB | @ −12 dB | @ −20 dB |
|---:|---:|---:|---:|---:|
| 12 (shipped) | **0** | 0 | 0 | 0 |
| 9 | 10 | 10 | 0 | 0 |
| 6 | 1 100 | 570 | 0 | 10 |
| 4 | 3 550 | 1 780 | 40 | 10 |
| 3 | 5 489 | 2 840 | 420 | 260 |

The good news: at the shipped threshold, zero false alarms in six minutes of
room tone (95 % upper bound ≈ 30/hour, so this is a weak bound, not a clean
bill of health).

The bad news: **white noise is the failure mode.** At t=4 — the threshold you
need for 90 % recall — full-scale white noise clears the threshold in 73.8 % of
windows. This is consistent with the confusion set on speech, where the only
words that false-accept at the shipped threshold are `seven` (8.3 %) and `six`
(7.1 %). The detector keys on the sibilant onset of "sheila", so any broadband
hiss — fan, tap, road noise, mic self-noise — is a partial wake word.

## 4. Input level — the dominant sensitivity

The chip has no AGC. Training peak-normalised every clip to 0.7. One feature
step is 3.01 dB (`MANT=1`).

Feature-offset proxy (add a constant to every band, then clamp):

| offset | −3 | −2 | −1 | 0 | +1 | +2 | +3 |
|---|---:|---:|---:|---:|---:|---:|---:|
| AUC | 49.0 % | 52.1 % | 77.9 % | **93.7 %** | 80.4 % | 65.8 % | 64.6 % |
| recall @ t=12 | 0 % | 0 % | 1.8 % | 21.5 % | 25.1 % | 53.3 % | 98.2 % |
| neg FPR @ t=12 | 0 % | 0 % | 0 % | 0.4 % | 5.6 % | 29.6 % | 96.2 % |

±3 dB costs 13–16 AUC points. ±6 dB puts it at chance. That is a narrower
window than any real microphone in any real room will hold.

That proxy is optimistic in one direction and pessimistic in another, so the
sweep was redone properly: the test split re-run through the front end at
several peak levels, then re-scored. The sigma-delta microphone model has its
own noise floor, so a gain change is *compressive* in the features, not a
constant shift — the mean feature falls only 1.1 steps over 24 dB.

| peak | dB | mean feat | AUC | recall @ t=12 | recall @ t=4 | neg FPR @ t=4 |
|---:|---:|---:|---:|---:|---:|---:|
| 0.700 | 0 | 5.74 | **93.30 %** | 19.8 % | 89.1 % | 15.7 % |
| 0.500 | −2.9 | 5.53 | 91.65 % | **3.1 %** | 79.9 % | 12.7 % |
| 0.350 | −6.0 | 5.36 | 84.94 % | **0.0 %** | 43.4 % | 6.9 % |
| 0.250 | −8.9 | 5.23 | 75.76 % | 0.0 % | 18.0 % | 2.6 % |
| 0.175 | −12.0 | 5.12 | 70.38 % | 0.0 % | 7.3 % | 1.4 % |
| 0.088 | −18.0 | 4.90 | 68.47 % | 0.0 % | 3.0 % | 0.6 % |
| 0.044 | −24.0 | 4.63 | 58.06 % | 0.0 % | 0.1 % | 0.2 % |

This is worse than the proxy suggested. **A 3 dB drop in input level takes
recall at the shipped threshold from 19.8 % to 3.1 %. A 6 dB drop takes it to
zero.** AUC survives longer than recall does — 85 % at −6 dB — which is exactly
the calibration-versus-ranking split seen in §5: the model still knows which
clip is more sheila-like, but the frozen threshold no longer sits anywhere near
the score distribution.

Since the trim can only lower the threshold, quiet input is the one direction
the trim *can* compensate — at the FA cost in §3. Loud input cannot be
compensated at all.

(The 0 dB row reads 93.30 % against the cached 93.67 %: this rebuild rolls the
jitter copies by index within the test subset rather than within the full plan,
so a handful of clips shift the other way. The 0.4-point difference is inside
the ±1.7 interval of §1 and does not affect any conclusion here.)

## 5. Front-end / mic response

One band pinned to the training median — the model for a filter that is dead,
or a microphone whose response differs from the sigma-delta model:

| band dead | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---:|---:|---:|---:|---:|---:|
| AUC | 88.7 % | 91.3 % | 91.4 % | 91.9 % | 87.0 % | 86.3 % |
| recall @ t=12 | 3.4 % | 9.5 % | 6.2 % | 34.2 % | 14.6 % | 3.6 % |

AUC degrades gracefully (−2 to −7 points), but recall **at a fixed threshold**
collapses. The ranking is preserved; the calibration is not. Any deviation
between the real microphone and the model in `train/wwhw.py` shifts the score
distribution enough to make the baked-in threshold meaningless — and the
threshold is silicon, adjustable only downward via the trim.

## 6. Evaluation-design issues, independent of the numbers

1. **Jitter copies are in the test split.** Four correlated rows per unique
   recording. It does not bias AUC much (93.67 vs 93.95) but it quadruples the
   apparent sample size and hides the real interval.
2. **Negatives are 1-second isolated words from the same corpus, mic chain and
   normalisation as the positives.** No room tone, no music, no far-field, no
   reverberation, no overlapping speakers, no non-English. The room-tone
   measurement in §3 is the first time any of that was tested, and it is 6
   minutes.
3. **Seed and threshold were both selected on validation.** Nothing is held out
   for the operating point itself.
4. **141 positive speakers** is small for a claim about speaker robustness, and
   §2 shows the per-speaker variance is enormous.
5. **The training bag must equal the hardware bag** (`train_hop=1` costs 9
   points — `nn_optimization.md §4.3`). That means the model is fitted to one
   specific alignment grid. On continuous audio the phase is arbitrary; nothing
   has tested whether the 5-window grid on a 1 s clip generalises to a stream.

## 7. The one that outranks all of the above

`test_detector_matches_model` **fails 37/40 frames** on this exact geometry
(`nn_optimization.md §7`). Every number in this document is a property of the
Python model, not of the RTL. The precedent is `DESIGN.md §9.2`: the rectified
microphone bug had RTL simulation, lint, the Python model and every area
estimate agreeing with each other, and all of them wrong — only gate-level
simulation caught it.

Until that test passes, "the chip does what this document describes" is
unproven.

## 8. What would move the needle, in order

1. **Fix the cocotb failure.** Nothing else matters until the RTL is known to
   implement the model.
2. **AGC or level normalisation.** §4 is the single biggest sensitivity and
   costs the most points. Even a coarse per-frame level subtract (a running
   max, subtracted from the log features — cheap in the log domain) would
   decouple the templates from absolute level. This changes silicon.
3. **Train with negatives that include broadband noise at multiple levels.**
   §3 shows the detector is a sibilance detector; the training set contains no
   hiss to teach it otherwise. This is free — training only — and is the
   highest value-per-effort item.
4. **Re-measure on hours of continuous audio,** not 6 minutes, before quoting
   any FA/hour figure.
5. Quote AUC as 94 ± 2 and stop treating the seed spread as an error bar.
