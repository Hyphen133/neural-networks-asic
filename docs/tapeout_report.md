# The two shipped parts at `pdm_gain=2.0`

Both detectors are the same silicon: one module, `src/tt_um_wakeword.sv`, built
twice. `-DWW_WEIGHTS_DRONE` swaps the weight header and the filterbank
geometry; nothing else differs. Both are re-trained here at the corrected
microphone drive and re-hardened.

**Both parts now sign off clean**, and both beat what shipped by a wide margin.

| | **sheila** | **drone** |
|---|---|---|
| detects | the word "sheila" | drone presence |
| test AUC | **94.88 %** | **98.92 %** |
| originally shipped | 89.14 % | 95.27 % |
| improvement | **+5.74** | **+3.65** |
| hardening | **clean** (`hardened/sheila_2`) | **clean** (`hardened/drone_3`) |

`drone_3` is `drone_2` with a re-tuned clock tree and a byte-identical logic
netlist -- same 98.92 % AUC, but 0 max-fanout violations instead of 14. See
`docs/fanout.md`.

Sheila took one extra step. At `NBAND=6` it scores 95.28 % but will not place
(DPL-0036), so it ships at `NBAND=5` and 94.88 %. That costs **0.40 AUC** and
buys 3.3 points of placement utilisation. Keeping `TAP0=3` is what makes the
trade cheap: the band count is worth 0.40, the 7.8-15.5 kHz octave is worth
3.9, and that octave carries the fricative in "sheila". Adopting the drone's
`TAP0=4/NBAND=5` instead would have scored 90.60 %.

## Accuracy

Selection is on **validation** AUC only; test is recorded and never used to
choose. `finalise.py` re-scores the emitted header through the independent
integer model in `train/eval_header.py`, so an export that disagrees with its
own training run is caught rather than shipped.

| | sheila_2 | drone_2 |
|---|---|---|
| seeds | 16 | 8 |
| validation | 93.20 ± 0.49 | **99.05 ± 0.13** |
| selected seed | 2 (val 94.06) | 6 (val 99.28) |
| test at that seed | **94.88 %** | **98.92 %** |
| test mean over seeds | 94.67 % | 98.79 % |
| header re-score | 94.88 % — **OK** | 98.92 % — **OK** |
| non-zero weights | 118 / 160 | 259 / 320 |
| test set | 1 272 clips, 768 positive | 12 117 clips, 5 598 positive |
| val − test gap | −1.68 | +0.26 |

The drone's ±0.13 seed spread is the tightest this project has measured; at the
old drive the same configuration gave ±0.61. Driving the modulator properly did
not just raise the mean, it made the problem better conditioned.

### Operating points

Threshold is trimmed on `ui_in[7:1]`, so these are selectable in the field
rather than fixed at tape-out.

**sheila_2** — score range −5..15, shipped threshold 14

| threshold | recall | negative clips firing |
|---|---|---|
| 13 | 51.6 % | 1.0 % |
| 12 | 61.6 % | 1.6 % |
| 9 | 76.8 % | 4.6 % |
| 6 | 86.6 % | 8.5 % |

**drone_2** — score range −30..15, shipped threshold 14

| threshold | recall | negative clips firing |
|---|---|---|
| 10 | 65.8 % | 0.7 % |
| 8 | 85.2 % | 1.9 % |
| 6 | 95.2 % | 4.1 % |
| 4 | 98.3 % | 7.8 % |

Synthetic room tone fires 0.0 % of the time on the drone at the shipped
threshold.

The drone dominates at every operating point: 95.2 % recall at 4.1 % false
alarms, where sheila reaches 76.8 % at 4.6 %. Both need a threshold well below
the shipped 14 to reach useful recall, which is what the `ui_in[7:1]` trim is
for.

## Silicon

1×1 IHP sg13g2 tile, 202.08 × 154.98 µm = 31 318 µm² die.

| | sheila_2 | drone_2 |
|---|---|---|
| synthesised area | 20 877 µm² | 21 145 µm² |
| placement utilisation | **78.38 %** | 79.36 % |
| core utilisation | 93.91 % | 94.60 % |
| standard cells | 1 643 | 1 675 |
| hold buffers | 294 | 290 |
| DRC (magic / routing) | **0 / 0** | **0 / 0** |
| LVS / antenna | **0 / 0** | **0 / 0** |
| setup / hold violations | **0 / 0** | **0 / 0** |
| worst setup slack | 7.67 ns | 6.40 ns |
| worst hold slack | 0.165 ns | 0.135 ns |
| power | 2.56 mW | 4.76 mW |
| **result** | **Flow complete** | **Flow complete** |

Against the parts they replace, both are cheaper as well as more accurate:
sheila_2 uses 71 fewer cells than the shipped sheila (1 643 against 1 714),
holds 0.82 ns more setup slack and draws 0.23 mW less, while scoring 5.74 AUC
higher. drone_2 costs 23 more cells than the shipped drone for 3.65 AUC.

### The failure that forced NBAND=5

At six bands sheila does not place. The resizer inserts 101 buffers across 23
fanout-violating nets plus 292 hold buffers, and those ~390 cells do not fit at
81.66 % utilisation. The liberty sets `default_max_fanout : 8`; the violating
nets are the FSM state decodes, one driving **104** loads and another 54. That
is inherent to sharing one time-multiplexed datapath across 205 flops, so it is
not a coding slip that can be tidied away. Raising `MAX_FANOUT_CONSTRAINT` to
16 was tried and changed nothing -- identical 81.662 % utilisation, same
DPL-0036. The reason it changed nothing is now known: the liberty's
`default_max_fanout : 8` is a floor, because OpenSTA takes
`min(SDC, liberty)`, so the SDC knob can only ever tighten the limit. These are
data nets and distinct from the CTS clock-leaf violations closed in
`docs/fanout.md`.

Two other hypotheses were tested and **both were wrong**, recorded here so they
are not tried again. The frame-mean feature was blamed for 127 µm² of dead
silicon; guarding its disabled path changes the netlist by **zero cells**, and
the apparent 127 µm² was an artefact of comparing across two different weight
headers. The gain-2.0 weights were suspected of tipping it over; they are
*smaller* than the gain-0.5 ones (81.66 % against 82.13 %) and fail anyway.

What actually worked was reducing `NBAND`, because `NBAND` is the expensive
parameter -- every band is an `fmax` register, a cascade tap and a ring slot --
whereas `NFRAME` only widens the constant ROM. That is also why the drone
places comfortably on five bands despite carrying a *larger* weight ROM
(640 bits against 320).

### Drone sign-off

| check | result |
|---|---|
| DRC (magic) | **0** |
| DRC (routing) | **0** (568 → 240 → 254 → 1 → 0 over iterations) |
| LVS errors | **0** |
| antenna violations | **0** |
| setup violations | **0** at all three corners |
| hold violations | **0** at all three corners |
| worst setup slack | 6.40 ns (slow 1.08 V 125 °C) |
| worst hold slack | 0.135 ns (fast 1.32 V −40 °C) |
| power grid violations | **0** |
| core utilisation | 94.60 % |
| total power | 4.76 mW |
| standard cells | 1 675 (205 sequential, 290 hold buffers, 433 fill) |

One thing about the six-band failure is still not understood, and is worth
recording rather than papering over: `runs/sheila_nb6` hardened `NBAND=6`
**cleanly** on 2026-09-04 at 81.15 % utilisation. The current RTL fails at
81.66 % with either weight set. Something between those dates took 0.5 points
of margin and it was not the frame-mean feature; the per-build geometry ifdef
is the remaining unexamined candidate. `NBAND=5` sidesteps it with 3.3 points
of headroom rather than 0.5, so the part is no longer balanced on that edge --
but the regression itself is unexplained.

## What produced the gains

Not architecture. `--pdm-gain 2.0`.

The front end fed its 2nd-order sigma-delta model
`peak_normalise(0.7) × attenuation(0.25..1) × gain` with `gain=0.5`, driving the
modulator at ~0.18 of its usable ±1 range and discarding ~11 dB of SNR before
the cascade saw anything. Correcting it is worth **+2.16 AUC on sheila and +3.65 on the drone**, at zero
area — same geometry, same header widths, no RTL change, no re-gate. (Sheila's
figure is its own five-band geometry measured at both drives: 92.72 → 94.88.)

For contrast, a 13-configuration × 6-seed sweep of every free training knob
(`epochs`, `aug_time`, `wd`, `label_smooth`, `leak`, `lr_w1`, `sched`) across
four tasks found nothing worth adopting; selecting its winners on validation
would have **lost** 0.55 test AUC on average. See `docs/val_test_gap.md`.

## Blockers

1. **`test/test.py:test_detector_matches_model` fails on the `NFRAME=8` sheila
   build** (37/40 frames) while the drone passes 3/3 on the same RTL. It cannot
   be run in this environment — cocotb is not installed in the LibreLane image
   and cannot be added (no pip in the venv, no host iverilog, no network in the
   container). **Nothing should tape out until this passes.** Note the sheila
   geometry has since changed to `NBAND=5`, so this test has never been run
   against the part in `hardened/sheila_2` at all -- `test/test.py:43` was
   updated to match, but updating the expectation is not the same as passing.
2. Any weight or geometry change invalidates the hardened artefacts;
   `hardened/sheila_2` matches `runs/sheila_2` and `hardened/drone_2` matches
   `runs/drone_g20h`, each against the header currently in `src/`.
3. The `NBAND=6` placement regression above is unexplained. It does not block
   these parts, but it will resurface the next time the design grows.

## Reproducing

```
scripts/gain20_ship.sh      # measure both parts at gain 0.5 and 2.0
scripts/finalise_g20.sh     # export both headers, then harden both
```

`finalise_g20.sh` writes `src/ww_weights.svh` and `src/ww_weights_drone.svh`.
Both are tracked, so `git checkout src/ww_weights*.svh` restores the gain-0.5
headers exactly.

Geometry, for the record — identical except the three marked rows:

| parameter | sheila | drone |
|---|---|---|
| `TAP0` | 3 | **4** |
| `NBAND` | 5 | 5 |
| `NFRAME` | **8** | 16 |
| `NSTAGE` | 9 | 9 |
| `STATE_W` | 10 | 10 |
| `MANT` / `FEAT_W` | 1 / 4 | 1 / 4 |
| `FRAME_LOG2` | 16 | 16 |
| `NPHASE` / `NHID` / `HACC_W` | 2 / 4 / 6 | 2 / 4 / 6 |
| `WW_ROW` | 320 bits | 640 bits |
