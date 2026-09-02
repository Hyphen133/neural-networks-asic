# Drone detector on the sheila silicon

A second set of constants for `tt_um_wakeword`: the same 1×1 IHP design,
same front end, same classifier, but trained on **DADS** (Drone Audio
Detection Samples) to answer "is there a drone in earshot" instead of "did
someone say sheila". Nothing in the RTL changes except which weight header is
included, so everything established for sheila about area, timing and
verification carries over. This document records what was done, what was
measured, and how to build it.

Companion to [DESIGN.md](DESIGN.md), which describes the hardware in full.

## 1. Result

| | sheila (ships) | drone |
|---|---|---|
| dataset | Speech Commands v0.02 | DADS, 114 129 one-second clips |
| test AUC, hardware-exact model | 89.1 % | **95.3 %** |
| fp32 ceiling on the same features | 94.1 % | 98.2 % |
| non-zero template weights | 237 / 320 | 245 / 320 |
| yosys cell area (`area_check.sh`) | 21 242 µm², 67.8 % | 21 289 µm², 68.0 % |
| LibreLane core utilisation, 0 DRC / LVS | 95.9 % | **94.6 %** (§6) |
| cocotb, RTL `FRAME_LOG2=8` / `16` / gate-level netlist | 3/3 each | 3/3 each |

Operating points at the shipped threshold (−1, chosen on validation):

| threshold | drone clips firing (recall) | non-drone clips firing |
|---:|---:|---:|
| 1 | 39.1 % | 0.9 % |
| 0 | 41.9 % | 1.3 % |
| **−1 (shipped)** | **66.4 %** | **4.8 %** |
| −2 | 81.7 % | 8.7 % |

Synthetic room tone (near-silence) never fires at any of these thresholds.
The chip makes one decision every 336 ms on continuous audio, so a hovering
drone that fires 66 % of windows is caught within about a second; the trim
pins move the threshold in steps of 4 (§5) for quieter or noisier sites.

## 2. Data

DADS (`geronimobasso/drone-audio-detection-samples` on Hugging Face, MIT,
6.8 GB of parquet) is 163 591 drone clips and 16 729 non-drone clips at
16 kHz mono. It is *not* a balanced or split dataset:

- drone clips are 0.5 s slices of longer recordings, named `drone-N.wav`
  with consecutive `N` from the same recording;
- non-drone clips are 10 s each, from UrbanSound8K, TUT Acoustic Scenes,
  ESC-50 and DNC — event sounds (sirens, engines, dogs, jackhammers), not
  quiet rooms.

`train/dadsdata.py` handles both. **The split is by blocks of the numeric
index** (400 drone clips ≈ 200 s of one source, 40 non-drone clips ≈ 400 s),
hashed to train / val / test at 80 / 10 / 10. A random split puts slice `N`
in train and slice `N+1` in test and would inflate every number below; this
is the same lesson the sheila work learned from Speech Commands' speaker
hash.

`train/extract_dads.py` turns rows into the 1 s clips the chip model runs on:

- drone slices shorter than 1 s are **loop-tiled** — rotor noise is
  quasi-stationary and the detector's 671 ms window is longer than the clip;
- each 10 s non-drone clip yields up to 4 evenly spaced 1 s segments,
  near-silent segments dropped;
- 35 % of drone clips are kept (deterministic per index) so the two classes
  balance at ~57 k each; 2 000 synthetic room-tone negatives are added;
- every clip is peak-normalised to 0.7 and attenuated by a **log-uniform gain
  in [−12 dB, 0]**, fixed per clip. A 4-bit log feature moves 4 steps over
  12 dB, so this forces the model to learn the shape of the spectrum rather
  than its level.

Decoded clips are cached as int16 (3.6 GB) so a re-extraction with different
front-end parameters skips the parquet pass. Extraction of 114 k clips took
about 5 minutes on 18 cores. Features: 24 frames × 5 bands, range 0..10,
mean 5.87 — so `FEAT_OFF` stays at 6, unchanged from sheila.

## 3. Training

`train/train_sheila.py` unchanged in semantics — per-frame saturating 6-bit
accumulator, leaky straight-through requantiser, mean-pool warm-up then
max-over-windows, tie-aware AUC — with a `--device cuda` path and
`--name/--out` so the drone header does not overwrite the sheila one:

```
python train/extract_dads.py --jobs 18
python train/ceiling_probe.py --tag dads --save-teacher           # 98.2 % ceiling
python train/train_sheila.py --tag dads --arch mlp --H 4 --accw 6 --shift 1 \
    --nphase 2 --device cuda --seed 5 --name drone --emit --out src/ww_weights_drone.svh
python train/eval_header.py --set-fpr 0.05                         # threshold on val
```

What was tried, test AUC of the hardware-exact model:

| variant | AUC |
|---|---:|
| linear ternary template (no hidden layer) | 91.2 % |
| H=4, 6-bit accumulator, `HSHIFT=1` — 14 seeds | 93.3 – 95.3 % |
| `HSHIFT=0` / `HSHIFT=2` | 93.3 % / 94.4 % |
| 300 epochs, shorter warm-up | 94.4 % |
| distillation from the fp32 MLP (α = 0.5 / 0.8) | 95.1 % / 95.1 % |
| **seed 5, selected on validation AUC (94.7 %)** | **95.3 %** |

Distillation, which helped the two-keyword sheila model, does nothing here:
with a fixed 320-trit template the model is capacity-bound, not
supervision-bound. Where the remaining 3 points go is visible when the test
set is binned by the level augmentation:

| clip level | AUC |
|---|---:|
| −12 … −9 dB | 97.6 % |
| −9 … −6 dB | 96.3 % |
| −6 … −3 dB | 98.1 % |
| −3 … 0 dB | 99.2 % |
| all levels mixed | 95.3 % |

Within any one level the detector is near the fp32 ceiling; the errors are
*across* levels — a quiet drone scoring below a loud siren. The chip has no
automatic gain control, so this is the price of a level-blind 4-bit log
front end, and it is the first thing a future revision should address (a
per-band running maximum subtracted before the template would cost about
20 flip-flops).

`train/eval_header.py` re-scores every emitted header through the integer
chip model; for all 14 seeds it reproduced the training-time AUC to the last
digit, which is the check that the export is bit-exact.

## 4. The learned template

Rows are bands from high to low, columns the 16 frames; `+` = +1, `-` = −1,
`.` = 0. Output weights [−1, −1, +1, −1], biases [2, 1, −7, 0].

```
unit 0   bias +2   output -1
    243–486 Hz  - - - - - - - - - - - . + + + +
    486–971 Hz  - - - - . . + + + + + + + + + +
   971–1943 Hz  - - - - . . . - . + + . + + + +
   1.9–3.9 kHz  - . - - - - + + + - + + + + + +
   3.9–7.8 kHz  - . - - . . - . . - - . . . . .

unit 1   bias +1   output -1
    243–486 Hz  - - - - - - - - - - - - - - - -
    486–971 Hz  + + + + + + + + + + + . + + + +
   971–1943 Hz  + + + + + + + + + + + + + . + +
   1.9–3.9 kHz  + + + . + + . . . + . - - - . .
   3.9–7.8 kHz  + + + + + + + + + + + + + + + +

unit 2   bias -7   output +1
    243–486 Hz  + + + + + . + . . + - + + + + +
    486–971 Hz  . + . . . + + + + + + . + . + +
   971–1943 Hz  - - . . . - - . + - . . - . . .
   1.9–3.9 kHz  . + . + - . . . . . . + + . . .
   3.9–7.8 kHz  . + + + + . . . . . + + . + - +

unit 3   bias +0   output -1
    243–486 Hz  + . + + . . . - . . - - - - - -
    486–971 Hz  + + + + + + + + + . . . - - - -
   971–1943 Hz  . . - - - - - - - - - - - - - -
   1.9–3.9 kHz  - - - - - - - - - - - - - - - -
   3.9–7.8 kHz  - - - - - - - - - - - - - - - -

threshold -1; 245 of 320 weights non-zero
```

Features enter the adder tree as `fmax − 6`, so a `+` weight rewards a band
that is louder than the corpus average and a `−` weight rewards one that is
quieter. Read that way the four units are:

- **Unit 2 (the only positive unit)** is the drone: the 243–486 Hz band up
  for the whole window — the blade-pass fundamental and its first harmonics
  of a multirotor — with 486–971 Hz mostly up too, and nothing demanded of
  the middle. Its bias of −7 means the drone hum has to be sustained; a
  single loud frame does not clear the requantiser.
- **Unit 1 (negative)** is "everything above 486 Hz loud, bottom band
  quiet": broadband and high-band events — sirens, tyres, speech, rain —
  that have no low-frequency hum. It is the template's main veto.
- **Unit 3 (negative)** is nearly all `−` in the top three bands, with the
  bottom bands turning `−` in the second half: it fires when the spectrum is
  *below* average everywhere, i.e. on quiet ambience, and pulls the score
  down. This is the unit that keeps the room-tone false-alarm rate at zero.
- **Unit 0 (negative)** is the one unit with a temporal shape: quiet for the
  first half of the window, then the low and middle bands come up. It is an
  onset detector for low-band events — a vehicle passing, a door, a voice
  starting — and vetoes hum that has not been there for the whole window.

Units 1 and 2 are nearly constant along the time axis, which is what a
stationary target should produce: for them the 16-frame accumulator is a
clipped integrator of a spectral shape and saturates within a few frames.
Units 0 and 3 do use the time axis, to separate a steady hum from things
that merely pass through the same bands.

## 5. Building and testing the drone variant

The header is selected with one define; the default build is still sheila.

```
# RTL simulation, 20 s
cd test && WEIGHTS=drone make
FRAME_LOG2=16 WEIGHTS=drone make          # full-rate frames, ~10 min

# area estimate
DEFINES=-DWW_WEIGHTS_DRONE ./area_check.sh

# LibreLane flow, same as sheila
TAG=drone WEIGHTS=drone ./harden_local.sh
```

`test/test.py` reads `WEIGHTS` too, so the golden model uses the same header
as the RTL. The cocotb build directory now carries the frame length and weight
set (`sim_build/rtl_f16_drone`): cocotb only recompiles when a source file
changes, and with a shared directory a `FRAME_LOG2=16` run silently reused the
`FRAME_LOG2=8` compile — the front-end test then fails with 8/8 frames
mismatched within 1.3 ms of simulated time. That is the signature of a stale
build, not of a front-end bug. For a TinyTapeout submission of the drone variant, copy
`src/ww_weights_drone.svh` over `src/ww_weights.svh` — TinyTapeout does not
pass defines — and update `docs/info.md`.

Threshold trim is unchanged: `ui[7:1]` = 64 is nominal, each step moves the
threshold by 4 score units, so `0111111` is threshold −5 (98 % of drone
clips firing, 24 % of noisy-event clips) and `1000001` is +3 (32 % / 0.4 %).

## 6. Physical implementation

Run with the final header (threshold −1) through `harden_local.sh`,
LibreLane 3.0.6, IHP sg13g2, 1×1 tile, `PL_TARGET_DENSITY_PCT 85`:

| stage | sheila | drone |
|---|---:|---:|
| synthesised cells | 1 246 | 1 652 |
| stdcell area after hold repair | 27 749 µm² | 27 365 µm² |
| core utilisation | 95.9 % | 94.6 % |
| setup / hold worst slack | 6.85 / 0.15 ns | 6.49 / 0.13 ns |
| DRC / LVS errors | 0 / 0 | 0 / 0 |

The drone constants harden to 94.6 % core utilisation against sheila's 95.9 %, a difference of -385 µm² of standard cells, with timing met and no DRC or LVS errors. The run is `runs/drone` (`harden_local.sh` log in `artifacts/logs/harden_drone.log`). The 1×1 fit therefore does not rest on the estimate alone: it is the same flow, the same template and the same result the sheila submission has.

## 7. What is not established

- **Field false-alarm rate.** The 4.8 % figure is per 1 s clip of *event*
  audio; nothing here measures alarms per hour on real ambient sound at a
  real site, with a real PDM microphone. Same caveat as for sheila.
- **Distance.** Level augmentation stands in for distance; no recording in
  DADS is labelled with range.
- **Other rotorcraft and vehicles.** DADS non-drone audio is urban event
  sound. Lawnmowers, motorcycles and helicopters are under-represented and
  are the obvious confusers for a 243–971 Hz template.
- **Source-level leakage.** The block split is a proxy; DADS does not ship
  the source-recording identity, so some correlation between train and test
  drones remains possible. The 14-seed spread (2 points) is a better guide
  to reliability than any single number.

## 8. File map

```
src/ww_weights_drone.svh   generated constants, selected by -DWW_WEIGHTS_DRONE
train/dadsdata.py          DADS loader, recording-block split, tiling/segmenting
train/extract_dads.py      parallel two-phase feature extraction, int16 clip cache
train/ceiling_probe.py     fp32 MLP ceiling, optional soft targets
train/eval_header.py       score an emitted header with the integer chip model,
                           set the threshold at a validation false-positive rate
train/test_dadsdata.py     pytest for the loader
artifacts/ww_feats_dads.npz          cached features (114 129 × 24 × 5)
artifacts/ww_drone_mlp_dads_s5.npz   the selected model's tensors
```
