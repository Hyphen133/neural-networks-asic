# Eight more detectors on the sheila silicon

Same chip as `docs/DESIGN.md`, same front end, same classifier, eight more
weight headers. This document records which corpus each detector is trained on,
why that corpus and not another, how the split is drawn, and what the two
reference numbers -- the fp32 ceiling and the hardware-exact model -- came out
at.

Companion to [DRONE.md](DRONE.md), which did this once for a single second
target. This is the same exercise generalised, so the pipeline is now
corpus-agnostic (`train/clipset.py`, `train/tasks.py`, `train/extract_clips.py`).

---

## 1. What the chip can hear

Nothing about the front end is learned, so a task has to fit it rather than the
other way round. At the geometry that hardened as `runs/sheila_nb6`
(`TAP0=3`, `NBAND=6`, `NFRAME=8`):

| | |
|---|---|
| bands | 243-486, 486-971, 971-1943, 1943-3886, 3886-7771, 7771-15542 Hz |
| resolution | 4-bit log magnitude, 3 dB per step |
| frame | 41.9 ms, max-pooled |
| decision window | 8 frames = 335 ms, hop 168 ms, 2 staggered phases |
| classifier | 4 hidden units, ternary weights, 6-bit saturating accumulator |

So a detector sees **the shape of a coarse spectral envelope and how it moves
over a third of a second**. That is the whole basis on which each of these
eight tasks was expected to work or not.

---

## 2. Corpora

Downloaded by `scripts/fetch_audio_datasets.py`, which is resumable and
unattended. Sizes are the download, not the unpacked tree.

| corpus | size | licence | why it is here | official split |
|---|---:|---|---|---|
| [FSD50K](https://zenodo.org/records/4060432) | 24.7 GB | per-clip CC, dataset CC-BY 4.0 | 51 197 Freesound clips over 200 AudioSet classes -- the negative class for six of the eight tasks and positives for five | dev (train/val) + eval, **uploader-disjoint between dev and eval** |
| [UrbanSound8K](https://zenodo.org/records/1203745) | 6.0 GB | CC BY-NC 4.0 | 929 siren and 1 000 dog_bark slices, against FSD50K's 132 and 536 | 10 folds, slices of one recording kept together |
| [ESC-50](https://github.com/karolpiczak/ESC-50) | 0.9 GB | CC BY-NC 3.0 | a third recording channel for six tasks; far too small to train on alone | 5 folds, source-file disjoint |
| [MUSAN](https://openslr.org/17) | 11.1 GB | per-file CC / US public domain | 109 h of speech (12 languages), music and noise -- the second corpus for VAD | none |
| [HumBugDB](https://zenodo.org/records/4904800) | 4.1 GB | CC BY 4.0 | 18.3 h mosquito and 14.6 h background, recorded on the same rigs | Test A = Tanzania field, Test B = UK culture |
| [donateacry](https://github.com/gveres/donateacry-corpus) | 0.07 GB | ODbL 1.0 | ~1 100 infant cries from ~260 different phones and homes | none |

### Corpora deliberately not used

* **CryCeleb2023** -- 26 093 cry segments, forty times donateacry. Rejected:
  every clip is a Samsung A10 held 10-15 cm from the mouth in one hospital
  protocol, so against FSD50K negatives a detector only has to recognise that
  microphone. Also gated, and CC BY-NC-**ND**.
* **AudioSet baby cry** -- the best channel diversity available (2 390
  unrelated uploads), but the HuggingFace mirror costs 49.8 GB to reach 120
  clips, or 2.44 TB for all 2 270.
* **Wingbeats (279 k clips), Abuzz** -- mosquito species corpora with **no
  negatives at all**. A presence detector built on them must borrow negatives
  from an unrelated corpus, which is exactly the failure HumBugDB avoids.
* **LeakDB, BattLeDIM** -- named as leak datasets throughout the literature;
  both are simulated hydraulic pressure and flow series, not audio.

---

## 3. The eight tasks

Defined in `train/tasks.py` as positive labels, ambiguous labels and corpora.
The *ambiguous* set is used by neither class: sounds where the label is
genuinely unclear. Calling `Applause` a negative for a clap detector, or `Dog`
without `Bark` a negative for a bark detector, teaches something false.

| task | question the LED answers | positives from | negatives from |
|---|---|---|---|
| `babycry` | an infant is crying nearby | donateacry + FSD50K `Crying_and_sobbing` + ESC-50 `crying_baby` | FSD50K, ESC-50 |
| `mosquito` | a mosquito is flying within earshot | HumBugDB `sound_type=mosquito` | HumBugDB `background` |
| `clap` | someone clapped | FSD50K `Clapping` + ESC-50 `clapping` | FSD50K, ESC-50 |
| `dogbark` | a dog is barking | FSD50K `Bark` + UrbanSound8K `dog_bark` + ESC-50 `dog` | all three |
| `catmeow` | a cat is meowing | FSD50K `Meow` + ESC-50 `cat` | FSD50K, ESC-50 |
| `vad` | somebody is speaking | FSD50K speech classes + MUSAN `speech` | FSD50K non-speech, MUSAN `music` + `noise` |
| `siren` | an emergency siren is sounding | UrbanSound8K `siren` + FSD50K `Siren` + ESC-50 `siren` | all three |
| `water` | water is running, dripping or trickling | FSD50K `Water_tap_and_faucet`, `Trickle_and_dribble`, `Gurgling`, `Drip`, `Sink_(filling_or_washing)` + ESC-50 `water_drops`, `pouring_water` | FSD50K, ESC-50 |

### Why `water` is not "water leak"

There is no public corpus of real pipe leaks recorded through a microphone in
air. What exists is contact-sensor data: the Hong Kong field set (Mendeley
`hkn8mxcjyz`) is 96 noise-logger and hydrophone recordings at **4096 Hz** with
no leak/no-leak token in the file names, and the lab testbed sets are hydrophone
and accelerometer signals at 8 kHz. Neither is what this chip's microphone
would hear. The detector therefore answers *"is water running"*, the closest
question the available data supports, and it is labelled a proxy everywhere it
appears.

### Why `mosquito` uses one corpus and the rest use two or three

Every corpus that supplies positives also supplies negatives, wherever it can.
If speech came only from MUSAN and non-speech only from FSD50K, a detector
could score well by recognising the corpus. HumBugDB is the exception in the
other direction: its background recordings come from the same cups, huts and
phones as its mosquito recordings, so *adding* FSD50K would hand the model a
corpus cue it does not currently have. donateacry is the exception that could
not be avoided -- no infant-cry corpus ships matched negatives -- which is why
the babycry positives also come from FSD50K and ESC-50, both of which supply
their own negatives.

---

## 4. Splits

Official wherever one exists, and never overridden:

| corpus | train | val | test |
|---|---|---|---|
| FSD50K | dev `split=train` | dev `split=val` | **eval** (uploader-disjoint from dev) |
| UrbanSound8K | folds 1-8 | fold 9 | fold 10 |
| ESC-50 | folds 1-3 | fold 4 | fold 5 |
| HumBugDB | remaining sites | 12 % of sites, stratified | Test A + Test B |
| MUSAN | 80 % | 10 % | 10 %, stratified by file |
| donateacry | 80 % | 10 % | 10 %, stratified by upload UUID |

Two corrections applied on top:

* **FSD50K's dev train/val is only *minimally* uploader-contaminated by
  design** -- the paper trades within-class contamination against class
  balance. `fsd50k.index` moves any val clip whose uploader also appears in
  train back into train, so the split this project uses is strictly disjoint.
* **Where no official split exists, groups are assigned by
  `clipset.stratified_split`, not by hashing.** Hashing a few hundred groups
  leaves the class balance of a 10 % validation split several points off
  target, and MUSAN's groups differ in size by two orders of magnitude. The
  stratified assignment walks groups in hash order -- deterministic and
  independent of file order -- and places each where the per-label deficit
  against target is largest. Measured: donateacry 80.0 / 9.9 / 10.1 %, MUSAN
  speech share 21.1 / 21.3 / 20.9 % across the three splits.

Negatives are drawn **per split**, in proportion to that split's positives, so
the class ratio does not drift between train, val and test either.

`clipset.describe` prints the number of groups shared between any two splits at
every extraction. It should always be zero.

---

## 5. From recordings to features

`train/extract_clips.py`, the corpus-agnostic sibling of `extract_dads.py`,
using the recipe the drone detector validated:

* every recording becomes 1 s clips at 16 kHz, peak-normalised to 0.7 and then
  attenuated by a **log-uniform gain in [-12 dB, 0]** fixed per clip, so the
  detector has to learn the shape of a spectrum rather than its level;
* **positives take the loudest windows, negatives evenly spaced ones.** An
  event corpus is weakly labelled: a 10 s clip tagged `Bark` is mostly the
  silence around a bark, and evenly spaced windows would label that silence
  positive. Ranking candidate windows by energy cannot invent an event, it only
  declines to label the gaps;
* positives are repeated at independent gains (`--pos-aug`), and the number of
  windows taken per negative is chosen automatically so the two classes come
  out near balance;
* 2 000 synthetic room-tone negatives, because "nothing is happening" is the
  condition the chip spends most of its life in;
* decoded clips are cached as int16, so re-extracting at a different front-end
  geometry never repeats the decode.

Features are extracted at `TAP0=3, NBAND=6` -- bands 3-8. That is a strict
superset of the drone geometry's bands 4-8, so one extraction serves both, and
a task that turns out to prefer the drone geometry costs no second decode.

---

## 6. Results

Produced by `scripts/run_tasks.py`: for each task it extracts features, runs the
fp32 ceiling probe, then trains the hardware-exact model over N seeds, selects
on **validation only**, emits a weight header, and re-scores that header through
the independent integer chip model in `train/eval_header.py`. The last step is
the sanity check -- if the two AUCs disagree the export is wrong and the header
must not be used.

Recipe for all eight, identical to the one that hardened as `runs/sheila_nb6`:
`H=4` ternary, `HACC_W=6`, `HSHIFT=1`, `NFRAME=8`, `NPHASE=2`, 1 000 epochs,
8 seeds, selection on validation only.

| task | clips | fp32 ceiling | **chip** | val | val sd | weights | export |
|---|---:|---:|---:|---:|---:|---:|---|
| `catmeow` | 4 625 | 83.77 | **79.74** | 92.06 | 0.16 | 151/192 | exact |
| `siren` | 14 177 | 82.60 | **76.24** | 83.25 | 0.80 | 156/192 | exact |
| `dogbark` | 17 531 | 81.18 | **75.74** | 76.89 | 0.54 | 152/192 | exact |
| `babycry` | 15 819 | 75.91 | **72.36** | 76.53 | 0.40 | 140/192 | exact |
| `clap` | 7 359 | 68.36 | **72.32** | 86.11 | 0.78 | 160/192 | exact |
| `water` | 15 336 | 73.11 | **68.92** | 76.71 | 0.58 | 150/192 | exact |
| `vad` | 29 949 | 63.09 | **63.72** | 80.06 | 0.46 | 151/192 | exact |
| `mosquito` | 43 499 | 64.08 | **62.96** | 90.32 | 0.17 | 161/192 | exact |

For reference on the same silicon: sheila 93.67 %, drone 95.27 %.

"exact" means `eval_header.py` re-scored the emitted header through the integer
chip model and got the training AUC back to the last decimal place, for all
eight. Every header came out at `centre=6` -- the `FEAT_OFF` the RTL already
has -- so none of them needs a parameter change, and at 140-161 of 192 non-zero
weights they sit either side of shipped sheila's 149/192 at the same geometry.
That makes the adder tree comparable but does not prove the fit: `area_gate.py`
has not been run on these headers, and it is the only thing that decides.

### What the numbers say

**None of the eight reaches sheila or drone.** The two shipped detectors answer
questions that are nearly stationary over a third of a second -- a rotor tone, a
two-syllable word spoken at the mic. These eight are mostly transient events
buried in an open-ended negative class drawn from 200 other sound types. That
is a harder question, not a worse model.

**The chip tracks its ceiling to within 3.4-6.4 points** on the six tasks where
the ceiling is meaningful, against the drone's 2.9 and the old sheila's 5.0. The
quantised 4-unit ternary classifier is not what limits these detectors.

**On `clap` and `vad` the chip *beats* the fp32 probe** (+4.0, +0.6). The probe
is a fixed 30-epoch MLP-64 reported at its final epoch; with only 366 training
positives it overfits -- `clap` reaches val 84.2 % and test 68.4 %. So the
ceiling is a same-recipe reference point, not an upper bound, and on
small-positive tasks the ternary model's regularisation wins. On those two rows
the number to read is the chip's.

**Every task loses ground from val to test**, some heavily. Two distinct causes,
both real rather than artefacts of the pipeline:

* `mosquito` (90.32 -> 62.96) is the official HumBugDB split doing its job. Test
  A is Tanzanian field sites with unseen rigs -- a deliberate domain shift, 93 %
  positive with 97 background recordings. The fp32 probe drops identically
  (91.38 -> 64.08), so the front end supports mosquito detection at ~90 % in
  domain and *neither* model transfers to new sites. That is a statement about
  the data, not about six bands.
* `vad` (80.06 -> 63.72) and the FSD50K-heavy tasks lose ground because the test
  split *is* FSD50K eval: a different uploader population, exhaustively
  labelled, harder than dev by construction. The uploader-disjointness pruning
  also leaves `vad` with only 111 positive recordings in validation, so seed
  selection there is noisier than its +-0.46 suggests.

**`vad` is the one clear negative result.** Ceiling 63.09 against chip 63.72:
the two agree, so the *features* are the limit. Speech is distinguished by
formant structure, and across six octave bands at 3 dB what survives is
syllabic envelope modulation, which music and much broadband noise also have. A
speech gate is not a good fit for this front end, whatever the band layout
suggests.

**`catmeow`, `siren` and `dogbark` are the three worth pursuing.** All three are
sustained or repeated harmonic events with a distinctive band signature, all
three clear 75 % on a properly disjoint test split, and `catmeow` does it from
the smallest dataset here (4 625 clips) -- which suggests data, not the front
end, is what is holding it back.

### Not done

* `area_gate.py` has not been run on any of these headers, so none of them is
  yet known to fit the 1x1 tile.
* cocotb has not been run against any of them. `test_detector_matches_model`
  currently fails on the `NFRAME=8` sheila build (`docs/nn_optimization.md` 7)
  and these eight share that geometry, so that has to be resolved before any of
  them means anything in silicon.
* No hyper-parameter search was run for any task: every one uses sheila's recipe
  unchanged. The per-task gains that the sheila search found (+4.5 AUC) are
  therefore still on the table for all eight.

