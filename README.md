![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# Sheila wake-word spotter — TinyTapeout IHP 1×1

A self-contained keyword spotter for the word **"sheila"** on a single
TinyTapeout IHP sg13g2 tile. PDM microphone bit-stream in, LED out. No host,
no memory, no weights to load: the classifier is hard-wired at tape-out.

```
   PDM mic ──ui[0]──► ┌────────────────────────────────────────────┐
   1.5625 MHz ◄─uo[0]─┤ 9-stage dyadic cascade (rotating ring)     │
                      │ 5 octave bands → log2 level (priority enc.) │
                      │ per-band max over 41.9 ms frames            │
                      │ H=4 ternary templates × 16 frames, 6-bit    │──uo[3]──► LED
                      │ saturating accumulators, 2 staggered windows│
   trim ──ui[7:1]───► │ ternary output layer > threshold + trim     │
                      └────────────────────────────────────────────┘
```

| | |
|---|---|
| keyword | "sheila", Google Speech Commands v0.02 |
| test AUC | 89.1 % (speaker-disjoint split, tie-aware) |
| tile | 1×1 IHP, 95 % core utilisation after the flow, 0 DRC, timing met |
| clock | 50 MHz |
| verification | cocotb, bit-exact against the Python model at RTL and gate level |

The RTL and the Python model in `train/wwhw.py` implement the same integer
arithmetic, so the number above is what the silicon computes. What is **not**
measured is recall at a fixed false-alarm rate; `FINDINGS.md` says exactly
what is and is not established.

## Layout

```
src/tt_um_wakeword.sv   the design (parameterised; defaults are what tapes out)
src/ww_weights.svh      generated weights: hidden templates, biases, output layer, threshold
src/ww_weights_drone.svh  alternative constants: DADS drone detector, selected with -DWW_WEIGHTS_DRONE (docs/DRONE.md)
src/config.json         LibreLane config from the ttihp template (PL_TARGET_DENSITY_PCT 85)
info.yaml               TinyTapeout project metadata and pinout
docs/info.md            datasheet: how it works, how to test, external hardware
docs/DESIGN.md          full design reference: signal chain, architecture, training, flow, verification
test/                   cocotb: reset, front end bit-exact, detector vs. model; RTL and GL
train/wwhw.py           bit-exact software twin of the RTL
train/wwdata.py         Speech Commands loader, official speaker-disjoint split
train/extract.py        run the front end over the dataset, cache features
train/train_sheila.py   quantisation-aware training, emits ww_weights.svh
train/extract_all.py, sweep_words.py   35-word keyword sweep (FINDINGS.md §6)
train/dadsdata.py, extract_dads.py, eval_header.py, ceiling_probe.py   drone detector pipeline (docs/DRONE.md)
area_check.sh           yosys cell-area estimate against the sg13g2 liberty
harden_local.sh         the TinyTapeout LibreLane flow, locally, in Docker
FINDINGS.md             the measurement trail, including why the first version did not fit
TAPEOUT_PLAN.md         the decisions behind this repository's shape
hardened/sheila, hardened/drone   final physical designs from LibreLane: GDS, LEF, DEF, netlists, SDC, lib, metrics, render
```

## Same silicon, second target: drone detection

The RTL is a fixed front end plus a hard-wired template, so retargeting it is
a matter of new constants. `src/ww_weights_drone.svh` holds a drone / no-drone
detector trained on DADS (114 k clips): **95.3 % test AUC** (fp32 ceiling
98.2 %), hardens to 94.6 % core utilisation with 0 DRC on the same 1×1 tile.
Build it with `WEIGHTS=drone` (`test/`, `harden_local.sh`) or
`DEFINES=-DWW_WEIGHTS_DRONE ./area_check.sh`; the default build is still
sheila. Data, split, results and limits: [docs/DRONE.md](docs/DRONE.md).

## Hardened designs

The final physical designs are checked in under `hardened/`, one directory per
target, straight from the `final/` stage of the LibreLane 3.0.6 run described
in `harden_local.sh` (`runs/wokwi6` for sheila, `runs/drone` for the drone
constants). Each holds `gds/tt_um_wakeword.gds` (the tape-out layout),
`lef/`, `def/`, `nl/` (gate-level netlist, the one the cocotb gate-level test
runs against), `pnl/` (powered netlist), `sdc/`, `lib/`, `vh/`, the flow
`metrics.json` and `metrics.csv`, and `render/tt_um_wakeword.png`.

| | sheila | drone |
|---|---|---|
| tile | 1×1 IHP sg13g2, 31 318 µm² die, 28 942 µm² core | same |
| std cells / instances | 1714 / 2102 | 1652 / 2089 |
| std cell area | 27 749 µm² | 27 365 µm² |
| core utilisation | 95.9 % | 94.6 % |
| setup slack, slow corner (1.08 V, 125 °C) | +6.85 ns | +6.49 ns |
| hold slack, fast corner (1.32 V, −40 °C) | +0.148 ns | +0.125 ns |
| setup / hold violations, all corners | 0 / 0 | 0 / 0 |
| magic DRC / LVS errors | 0 / 0 | 0 / 0 |
| antenna, slew, cap violations | 0 | 0 |
| max-fanout warnings | 14 | 15 |
| total power estimate (typ corner) | 2.8 mW | 4.3 mW |

Both were hardened from the same `src/tt_um_wakeword.sv` at commit `42f7cd3`
or later, i.e. after the mic-input sign fix; only the weight header differs.
The max-fanout counts are warnings from the ttihp template's fanout limit and
are not sign-off violations. To rebuild: `./harden_local.sh` (sheila) or
`WEIGHTS=drone ./harden_local.sh`, then copy `runs/<tag>/final/` here.

## Quick start

```bash
# tests (needs iverilog, python -m pip install -r test/requirements.txt)
cd test && make                 # RTL, short frames, ~20 s
FRAME_LOG2=16 make              # RTL at the tape-out frame length, slow
GATES=yes make                  # gate-level netlist (copy it to test/gate_level_netlist.v)

# area and hardening
./area_check.sh                 # yosys estimate; add ~30 % for the flow
./harden_local.sh               # LibreLane 3 in Docker, same flow as the GDS action

# retrain (dataset: speech_commands_v0.02, 2.3 GB, under artifacts/data/)
python train/extract.py --targets sheila --aug 4 --neg-per-word 150 \
    --nstage 9 --nband 5 --tap0 4 --state-w 10 --mant 1 --feat-w 4 --tag sheila_hw
python train/train_sheila.py --tag sheila_hw --arch mlp --H 4 --WL 1 --shift 1 \
    --nphase 2 --accw 6 --epochs 250 --seed 3 --emit
```

## Lessons that cost area

The yosys cell-area estimate said 78 % of the tile. The real flow said 106 %.
Three things the estimate never sees, all in `FINDINGS.md` §7:

- sg13g2 has no reset-less flip-flop, so a synchronous reset costs a tie cell
  and a mux per flop. Use an asynchronous reset.
- Every flop gets a hold buffer under TinyTapeout's margins. Budget ~73 µm²
  per flop, not 49.
- Fanout repair on the reset and FSM nets adds another ~1 000 µm².

And one that would have cost the chip: yosys parses `-N'(x)` as a cast with a
negated width, not as a negated value, so the microphone input was rectified in
the netlist while RTL simulation, lint and the Python model all agreed with each
other. Only the gate-level simulation caught it. Run it.

## License

Apache-2.0 (see `LICENSE`). Google Speech Commands is CC BY 4.0.
