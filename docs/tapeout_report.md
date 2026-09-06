# The two shipped parts at `pdm_gain=2.0`

Both detectors are the same silicon: one module, `src/tt_um_wakeword.sv`, built
twice. `-DWW_WEIGHTS_DRONE` swaps the weight header and the filterbank
geometry; nothing else differs. Both are re-trained here at the corrected
microphone drive and re-hardened.

**One of the two is ready and one is not.** The drone signs off clean. Sheila
is 6.1 AUC better than the part that originally shipped but currently fails
detailed placement, and that failure is unexplained.

| | **sheila** | **drone** |
|---|---|---|
| detects | the word "sheila" | drone presence |
| test AUC | **95.28 %** | **98.92 %** |
| previously | 93.67 % (gain 0.5) | 95.27 % (gain 0.5) |
| originally shipped | 89.14 % | — |
| hardening | **FAILS** (DPL-0036) | **clean** |

## Accuracy

Selection is on **validation** AUC only; test is recorded and never used to
choose. `finalise.py` re-scores the emitted header through the independent
integer model in `train/eval_header.py`, so an export that disagrees with its
own training run is caught rather than shipped.

| | sheila | drone |
|---|---|---|
| seeds | 16 | 8 |
| validation | 93.27 ± 0.38 | **99.05 ± 0.13** |
| selected seed | 9 (val 93.86) | 6 (val 99.28) |
| test at that seed | **95.28 %** | **98.92 %** |
| test mean over seeds | 95.08 % | 98.79 % |
| header re-score | 95.28 % — **OK** | 98.92 % — **OK** |
| test set | 1 272 clips, 768 positive | 12 117 clips, 5 598 positive |
| val − test gap | −1.98 | +0.26 |

The drone's ±0.13 seed spread is the tightest this project has measured; at the
old drive the same configuration gave ±0.61. Driving the modulator properly did
not just raise the mean, it made the problem better conditioned.

### Operating points

Threshold is trimmed on `ui_in[7:1]`, so these are selectable in the field
rather than fixed at tape-out.

**sheila** — score range −8..15, shipped threshold 14

| threshold | recall | negative clips firing |
|---|---|---|
| 14 | 42.4 % | 1.0 % |
| 11 | 66.0 % | 2.0 % |
| 8 | 81.9 % | 4.6 % |
| 6 | 88.7 % | 7.9 % |

**drone** — score range −30..15, shipped threshold 14

| threshold | recall | negative clips firing |
|---|---|---|
| 10 | 65.8 % | 0.7 % |
| 8 | 85.2 % | 1.9 % |
| 6 | 95.2 % | 4.1 % |
| 4 | 98.3 % | 7.8 % |

Synthetic room tone fires 0.0 % of the time on the drone at the shipped
threshold.

The drone dominates at every operating point: 95.2 % recall at 4.1 % false
alarms, where sheila reaches 81.9 % at 4.6 %. Sheila's useful high-recall
region needs a threshold well below the shipped 14.

## Silicon

1×1 IHP sg13g2 tile, 202.08 × 154.98 µm = 31 318 µm² die.

| | sheila | drone |
|---|---|---|
| synthesised area | 21 686 µm² | 21 145 µm² |
| placement utilisation | 81.66 % | **79.36 %** |
| hold buffers | 292 | 290 |
| fanout violations | 23 | — |
| **result** | **DPL-0036, flow aborted** | **Flow complete** |

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

### Why sheila fails and the drone does not

Not timing — the design meets timing. It is *placement*: the resizer inserts
101 buffers across 23 fanout-violating nets plus 292 hold buffers, and those
~390 cells do not fit alongside 1 300 others at 81.7 % utilisation.

The liberty sets `default_max_fanout : 8`. The violating nets are the FSM state
decodes: one net drives **104** loads, another **54**, two more 25 each. That is
architectural. The design shares one datapath — one cascade, one adder tree,
one classifier — time-multiplexed by a state machine, so `S_CASC`/`S_CLASS`/
`S_ROLL` gate the enable of nearly every one of the 205 flops. It is the direct
cost of the area-saving decision to share rather than replicate.

The drone escapes it with 5 bands to sheila's 6. `NBAND` is the expensive
parameter — each band adds an `fmax` register, a cascade tap and a ring slot —
whereas `NFRAME` only widens the constant ROM. So the drone's *larger* weight
ROM (640 bits against 384) still yields the *smaller* chip, and its 2.3 points
of extra slack is what lets its 290 hold buffers land.

**The failure is recent and unexplained.** `runs/sheila_nb6` hardened this same
architecture cleanly on 2026-09-04 at 81.15 % utilisation — Flow complete, 0
DRC, 0 LVS. The current build fails at 81.66 %. Both weight sets fail on the
current RTL (gain 0.5 at 82.13 %, gain 2.0 at 81.66 %) while the older RTL
passed, so it is the RTL, not the weights. A frame-mean feature landing between
those dates was measured and **cleared**: guarding its disabled path changes the
netlist by zero cells. The remaining candidate is the per-build geometry ifdef.
Untried: raising `MAX_FANOUT_CONSTRAINT` above 8, which would insert fewer
buffers on nets that have 6.4 ns of setup slack to spare. That is a flow
setting; it changes no logic.

## What produced the gains

Not architecture. `--pdm-gain 2.0`.

The front end fed its 2nd-order sigma-delta model
`peak_normalise(0.7) × attenuation(0.25..1) × gain` with `gain=0.5`, driving the
modulator at ~0.18 of its usable ±1 range and discarding ~11 dB of SNR before
the cascade saw anything. Correcting it is worth **+1.61 AUC on sheila and
+3.65 on the drone**, at zero area — same geometry, same header widths, no RTL
change, no re-gate.

For contrast, a 13-configuration × 6-seed sweep of every free training knob
(`epochs`, `aug_time`, `wd`, `label_smooth`, `leak`, `lr_w1`, `sched`) across
four tasks found nothing worth adopting; selecting its winners on validation
would have **lost** 0.55 test AUC on average. See `docs/val_test_gap.md`.

## Blockers

1. **Sheila does not harden.** Above.
2. **`test/test.py:test_detector_matches_model` fails on the `NFRAME=8` sheila
   build** (37/40 frames) while the drone passes 3/3 on the same RTL. It cannot
   be run in this environment — cocotb is not installed in the LibreLane image
   and cannot be added (no pip in the venv, no host iverilog, no network in the
   container). **Nothing should tape out until this passes.**
3. Any further weight change invalidates `runs/drone_g20h`; it is the run that
   matches the header currently in `src/ww_weights_drone.svh`.

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
| `NBAND` | **6** | 5 |
| `NFRAME` | **8** | 16 |
| `NSTAGE` | 9 | 9 |
| `STATE_W` | 10 | 10 |
| `MANT` / `FEAT_W` | 1 / 4 | 1 / 4 |
| `FRAME_LOG2` | 16 | 16 |
| `NPHASE` / `NHID` / `HACC_W` | 2 / 4 / 6 | 2 / 4 / 6 |
| `WW_ROW` | 384 bits | 640 bits |
| non-zero weights | 135 / 192 | 259 / 320 |
