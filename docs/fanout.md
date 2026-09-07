# The 14 max-fanout violations, and how they went away

`drone_2` signed off with `design__max_fanout_violation__count: 14`. Every one
of them was a CTS leaf clock buffer:

```
Pin                     Limit  Fanout  Slack
clkbuf_leaf_10_clk/X        8      17     -9 (VIOLATED)
clkbuf_leaf_11_clk/X        8      17     -9 (VIOLATED)
...
clkbuf_leaf_4_clk/X         8      11     -3 (VIOLATED)
```

They are gone. `CTS_SINK_BUFFER_MAX_CAP_DERATE_PCT: 50` in `src/config.json`
takes the count to **0** for **+98 µm²**, no RTL change, and no accuracy change
of any kind. This file records why the obvious fixes do not work, why the
violation was harmless in the first place, and the sweep that found the one
that does.

## Why the constraint cannot simply be raised

The limit is not ours. `sg13g2_stdcell_typ_1p20V_25C.lib:36` sets

```
default_max_fanout : 8;
```

and OpenSTA takes `min(SDC, liberty)`. The generic SDC already asks for
`set_max_fanout 10.0` (`MAX_FANOUT_CONSTRAINT`), and 8 wins. Raising the SDC
value can therefore only ever be a no-op:

| config | fanout violations | stdcells | setup ws | hold ws |
|---|---:|---:|---:|---:|
| shipped `drone_2` | 14 | 1 675 | 6.40 ns | 0.135 ns |
| `MAX_FANOUT_CONSTRAINT: 24` | **14** | 1 675 | 6.40 ns | 0.135 ns |

Byte-identical, every metric. FINDINGS.md had this right as an observation
("the SDC wins") but the mechanism is the liberty floor, not SDC precedence.

Clock nets are also outside `repair_design`'s remit -- fanout repair does not
touch them by design, because buffering the clock is CTS's job. So there is no
resizer knob for this either. It is a CTS problem or it is nothing.

## The violation was electrically vacuous

Before fixing it, it is worth being clear that nothing was actually wrong.

The worst leaf net carries, from the run's own SPEF and liberty:

| | |
|---|---|
| wire cap, `clknet_leaf_7_clk` | 0.0324 pF |
| 17 × `sg13g2_dfrbp_1` CLK pin cap (0.00280 pF) | 0.0475 pF |
| **total load** | **≈ 0.08 pF** |
| `sg13g2_buf_8` `max_capacitance` | **2.4 pF** |

The buffer was driving **3 %** of what it is characterised for. Max-slew and
max-cap violations were both 0 at all three corners, clock skew was 0.26 ns on
a 20 ns period, and the flow has **no max-fanout checker** -- the 17 checker
steps gate setup, hold, max slew, max cap, DRC, LVS, antennas, the power grid
and wirelength, and fanout is metrics-only. `default_max_fanout : 8` is a
library-wide bookkeeping default, and on a `buf_8` driving 17 flop clock pins
it is roughly 30× more conservative than the cell.

That is the honest framing: this was a cosmetic metric, and the fix is worth
taking because it is nearly free, not because the part was at risk.

## Shrinking the model cannot fix it

The instinct is that 205 flops is too many for a 15-leaf tree, so fewer flops
would mean less fanout. The four parts already hardened say otherwise:

| part | flops | stdcells | fanout violations |
|---|---:|---:|---:|
| `sheila` | 205 | 1 714 | 14 |
| `sheila_2` | **203** | **1 643** | **15** |
| `drone` | 205 | 1 652 | 15 |
| `drone_2` | 205 | 1 675 | 14 |

`sheila_2` is the smallest part by both measures and has the *most* violations.
CTS clusters sinks against the leaf buffer's cap budget and then creates as
many leaves as it needs, so halving the flop count halves the number of leaves
and leaves the fanout *per leaf* exactly where it was.

So the accuracy lever is not expensive here. It is **inert**. Nothing in
`nn_optimization.md` or `pruning.md` could have moved this number.

## The sweep

`scripts/fanout_sweep.sh`, 11 configs, each a full LibreLane run through
`harden_local.sh` with `WEIGHTS=drone`. Every config changes only the clock
tree: same RTL, same weight header, same logic netlist, so test AUC is 98.92 %
in every row by construction. The `base` control reproduced the shipped
`drone_2` on every metric, which is what makes the rest comparable.

| config | fanout vio | clkbuf | stdcell | hold buf | util % | setup ws | hold ws | power mW |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| shipped `drone_2` | 14 | 21 | 1 675 | 290 | 94.60 | 6.40 | 0.135 | 4.76 |
| `base` (control) | 14 | 21 | 1 675 | 290 | 94.60 | 6.40 | 0.135 | 4.76 |
| `MAX_FANOUT_CONSTRAINT: 24` | 14 | 21 | 1 675 | 290 | 94.60 | 6.40 | 0.135 | 4.76 |
| `CTS_MAX_CAP: 0.3` | 14 | 21 | 1 675 | 290 | 94.60 | 6.40 | 0.135 | 4.76 |
| `CTS_CLK_BUFFERS: [buf_4, buf_2]` | 1 | 33 | 1 693 | 294 | 94.85 | 6.76 | 0.147 | 4.68 |
| **`DERATE_PCT: 50`** | **0** | 49 | 1 704 | 287 | **94.93** | 6.30 | **0.147** | 4.91 |
| `DERATE_PCT: 80` | 0 | 49 | 1 704 | 287 | 94.93 | 6.30 | 0.147 | 4.91 |
| `CTS_CLK_BUFFERS: [buf_2]` | 0 | 49 | 1 704 | 287 | 94.93 | 6.30 | 0.147 | 4.91 |
| `[buf_4, buf_2]` + `SIZE: 8` | 0 | 41 | 1 688 | 287 | 95.25 | 6.56 | 0.132 | 4.98 |
| `CTS_SINK_CLUSTERING_SIZE: 8` | *0 at CTS* | -- | -- | -- | -- | -- | -- | -- |
| `CTS_SINK_CLUSTERING_SIZE: 6` | *0 at CTS* | -- | -- | -- | -- | -- | -- | -- |
| `CTS_SINK_CLUSTERING_SIZE: 4` | *0 at CTS* | -- | -- | -- | -- | -- | -- | -- |
| `SIZE: 8` + `MAX_DIAMETER: 20` | *0 at CTS* | -- | -- | -- | -- | -- | -- | -- |

`DERATE_PCT` is `CTS_SINK_BUFFER_MAX_CAP_DERATE_PCT`. The last four fixed the
fanout at CTS and then **failed `DPL-0036`** in post-CTS hold repair -- the same
legalisation wall that forced `NBAND=5` on sheila. Full sign-off checks on the
completing rows: DRC, LVS, antenna, max-slew, max-cap and power-grid violations
were 0 everywhere, as in the baseline.

### Why the size cap fails and the derate does not

`CTS_SINK_CLUSTERING_SIZE: 8` does exactly what it says -- the leaf histogram
goes from `10:2, 11:1, 13:2, 14:2, 15:1, 16:4, 17:2` to
`2:1, 3:4, 4:8, 5:1, 7:1, 8:22`, 29 leaves, nothing over 8. It then cannot be
placed, because it keeps `sg13g2_buf_8` as the sink buffer:

| | leaves | sink cell | leaf area |
|---|---:|---|---:|
| shipped | 15 | `sg13g2_buf_8` (23.59 µm²) | 354 µm² |
| `SINK_CLUSTERING_SIZE: 8` | 29 | `sg13g2_buf_8` | 684 µm² |
| **`DERATE_PCT: 50`** | 38 | **`sg13g2_buf_2` (9.07 µm²)** | **345 µm²** |

That is the whole trick. Derating the cap budget makes the *choice of cell*
fall to `buf_2`, and 38 small leaves cost slightly **less** silicon than 15
large ones. Total stdcell area moves 27 377.5 → 27 475.5 µm², +98 µm², +0.36 %,
and the fill-cell count goes *up* (433 → 446) -- the design ends with more
slack, not less. The explicit `CTS_CLK_BUFFERS: [sg13g2_buf_2]` reaches the
identical tree, which confirms the mechanism.

`buf_4` is the interesting near-miss: 28 leaves, one net left at fanout 9, and
better than baseline on both setup slack (6.76 ns) and power (4.68 mW). Adding
`SINK_CLUSTERING_SIZE: 8` closes that last net but lands at 95.25 %
utilisation, the tightest of any completing config.

## What shipped

`"CTS_SINK_BUFFER_MAX_CAP_DERATE_PCT": 50` in `src/config.json`, hardened as
`hardened/drone_3`.

`src/config.json` is the only viable home for it:
`tt-support-tools/project.py:486` regenerates `src/user_config.json` from
scratch on every CI harden, so a knob placed there is silently dropped, while
`IHPTech.librelane_config` is `{}` and overrides nothing in `config.json`.

`drone_3` against `drone_2`:

| | `drone_2` | `drone_3` |
|---|---:|---:|
| **max-fanout violations** | **14** | **0** |
| stdcell area | 27 377.5 µm² | 27 475.5 µm² |
| utilisation | 94.60 % | 94.93 % |
| fill cells | 433 | 446 |
| clock buffers / inverters | 21 / 11 | 49 / 15 |
| hold buffers | 290 | 287 |
| setup ws (slow 1.08 V 125 °C) | 6.404 ns | 6.300 ns |
| hold ws (fast 1.32 V −40 °C) | 0.1354 ns | **0.1468 ns** |
| clock skew | 0.258 ns | 0.277 ns |
| power | 4.76 mW | 4.91 mW |
| DRC / LVS / antenna / slew / cap / PG | 0 | 0 |
| **test AUC** | **98.92 %** | **98.92 %** |

The costs are +0.15 mW of clock power (28 more buffers toggling every cycle)
and 0.10 ns of setup slack out of 6.4 ns on a 20 ns period. Hold slack, the
margin that is actually thin on this part, improves by 0.011 ns.

### The shared config also fixes sheila

`src/config.json` is shared, and the CI default build (no `VERILOG_DEFINES`) is
the **sheila** variant, so this derate lands on sheila whether or not anyone
asked it to. That was checked rather than assumed (`runs/sheila_3`), and sheila
comes out ahead on every axis:

| | `sheila_2` | `sheila_3` |
|---|---:|---:|
| **max-fanout violations** | **15** | **0** |
| setup ws (slow) | 7.668 ns | **7.753 ns** |
| hold ws (fast) | 0.1655 ns | **0.1755 ns** |
| stdcells | 1 643 | 1 676 |
| clock buffers | 22 | 49 |
| utilisation | 93.91 % | 94.31 % |
| power | 2.56 mW | 2.56 mW |
| DRC / LVS / antenna / slew / cap / PG | 0 | 0 |

Unlike the drone, sheila pays *nothing* -- both slacks improve and power is
unchanged to three figures, because sheila sits 0.7 points lower in utilisation
and had the headroom to absorb the extra leaves comfortably.

`hardened/sheila_2` is left in place as the archived part of record; promoting
`runs/sheila_3` into `hardened/sheila_3` is a separate tape-out decision and
has not been taken here.

## Negative results, recorded so they are not retried

1. **`MAX_FANOUT_CONSTRAINT`, any value.** The liberty's 8 is a floor.
   Byte-identical output at 24.
2. **`CTS_MAX_CAP`.** `configure_cts_characterization -max_cap 0.3` does not
   drive sink clustering. Byte-identical output.
3. **Fewer flops / a smaller model.** Inert, per the four-part table above.
4. **`CTS_SINK_CLUSTERING_SIZE` on its own**, at 8, 6 or 4, with or without
   `CTS_SINK_CLUSTERING_MAX_DIAMETER`. Fixes the fanout, fails `DPL-0036`,
   because it keeps the large sink buffer.
5. **`CTS_SINK_BUFFER_MAX_CAP_DERATE_PCT: 80`.** Works, but is identical to 50
   -- the plateau is wide, so there is nothing to gain by pushing it.

## Reproducing

```sh
nohup scripts/fanout_sweep.sh > artifacts/fanout_sweep.log 2>&1 &
python3 scripts/fanout_sweep_report.py
```

The report reads `runs/fo_*/final/metrics.json` and the `CTS-0016` histogram
from each run's CTS log, so configs that die at `DPL-0036` still report what
CTS achieved before the flow fell over. A full run is about 2.5 minutes.
