# Accuracy optimisation log — sheila and drone

Branch `nn_optim`. Goal: raise the test AUC of both classifiers **without
breaking the 1×1 tile**, and record what moved the number and what did not.

This is a running log. Sections are appended as evidence arrives; a claim with
no number next to it has not been measured yet.

---

## 0. Where we start

Both detectors are the same silicon (`src/tt_um_wakeword.sv`), differing only
in the weight header. Shipped configuration:

| parameter | value |
|---|---|
| `NHID`, `HACC_W`, `HSHIFT`, `FEAT_OFF` | 4, 6, 1, 6 |
| `NFRAME`, `NPHASE`, `NBAND` | 16, 2, 5 |
| `NSTAGE`, `STATE_W`, `TAP0`, `K_SHIFT` | 9, 10, 4, 2 |
| `MANT`, `FEAT_W`, `FRAME_LOG2` | 1, 4, 16 |

| model | test AUC | fp32 ceiling on the same features |
|---|---:|---:|
| sheila (`src/ww_weights.svh`, seed 3) | 89.1 % | 94.1 % (MLP-32) |
| drone (`src/ww_weights_drone.svh`, seed 5) | 95.3 % | 98.2 % (MLP-64) |

So there is a **5-point** gap on sheila and a **3-point** gap on drone between
the hardware model and what the fixed front end can support. Closing part of
that gap is the target; it is an optimisation problem, not a capacity one.

### The constraint that shapes everything

`runs/wokwi6` (sheila) finishes at **95.9 % core utilisation** and `runs/drone`
at 94.6 %. Flip-flops dominate and cost ~73 µm² each on this flow after hold
repair (`docs/DESIGN.md §7.2`). Every candidate is therefore classified before
it is trusted:

| class | what it touches | area consequence |
|---|---|---|
| **free** | epochs, schedule, pooling, augmentation, seed, selection | none — training only |
| **near-free** | `NFRAME`, `HSHIFT`, `FEAT_OFF`, weight precision `WL` | ROM/adder-tree only, no flops; decided by `area_check.sh` |
| **costly** | `NHID`, `HACC_W`, `NPHASE`, `STATE_W`, `NSTAGE` | adds flops; almost certainly does not fit |

The search spends its budget in that order.

---

## 1. Method

`train/optim/qat.py` is a parametrised superset of `train/train_sheila.py`
with identical hardware semantics. Its self-test reproduces the shipped recipe
exactly, so any difference in a later number comes from the knob that was
turned and not from a rewrite:

```
$ python train/optim/qat.py cuda
sheila baseline seed 3: val 87.85%  test 89.93%   (train_sheila.py: val 87.9  test 89.93)
```

The drone baseline reproduces too — `Cfg.baseline('dads', epochs=250, seed=5)`
gives **val 94.59 % / test 95.23 %** against the shipped header's 95.3 %.

`train/optim/grid.py` drives it. Rules that keep the search honest:

- **Selection is on validation only.** Test AUC is recorded on every run and
  used for nothing.
- **Configurations are ranked by the mean over seeds, not the best.** The
  sheila validation split has 1 258 clips, so one run moves ≈1 point on seed
  noise; a best-of-N number mostly measures N.
- Results append to `artifacts/optim/<tag>.jsonl` keyed by (config, seed set),
  so an interrupted search resumes instead of repeating.

Cost on the GB10: 12 s per sheila run (250 epochs), 55–135 s per drone run.

---

## 2. Plan

Session of 2026-09-04, 17:30–22:30 local. Stages, in the order the budget is
spent, cheapest evidence first:

| stage | what | cost class | status |
|---|---|---|---|
| A0 | shipped baseline, 6 seeds, as a reference distribution | — | **done** |
| A1 | sheila coordinate sweep over 20 free dimensions, 4 seeds | free | running |
| A2 | drone coordinate sweep, reduced, 3 seeds, 100-epoch screen | free | queued behind A1 |
| B | front-end variants: `K_SHIFT`, `TAP0`, `NBAND`, `MANT`/`FEAT_W` | free to near-free | running |
| C1 | near-free silicon constants: `NFRAME`, `WL` | near-free | running |
| C2 | costly dims (`NHID`, `HACC_W`, `NPHASE`) measured only to size the gap | costly | running |
| D | combination of the winners, cross sweep, 12 seeds | mixed | pending |
| E | drone re-run on the winning front end | mixed | pending |
| F | emit headers, `eval_header.py`, `area_check.sh`, commit | — | pending |

The GPU sits at 8 % utilisation during a sweep — the per-frame accumulator
loop is a Python loop over 16 `einsum` calls, so throughput is bound by kernel
launches, not by the device. Three sweeps therefore run concurrently at close
to three times the aggregate rate; they write to different `.jsonl` files (or
append single lines to the same one, deduplicated on reload).

Commands are recorded with each result below so any number can be re-derived.

The front-end variants in stage B all hold `NSTAGE=9` and `STATE_W=10` — the
cascade is `NSTAGE × STATE_W` flip-flops and nothing else in the front end
comes close, so holding those two fixed keeps the flop count where it is while
`TAP0`, `K_SHIFT` and the log resolution move freely:

```
python train/extract.py --targets sheila --aug 4 --neg-per-word 150 \
    --nstage 9 --state-w 10 --nband <5..7> --tap0 <2..4> \
    --mant <0..2> --feat-w <3..5> --k-shift <1..3> --tag sheila_<v>
```

---

## 3. The fit gate

**A configuration that does not fit the 1×1 tile is not an improvement, whatever
its AUC.** So area is measured for every candidate before its accuracy is
believed, not once at the end.

`train/optim/area_gate.py` does this in 5 s per configuration.
`area_check.sh` cannot: it synthesises four hard-coded configurations, and more
importantly `NFRAME`, `NBAND` and `NHID` all change the required width of
`WW_ROW`/`WW_HBIAS` in the header included at `src/tt_um_wakeword.sv:69,71`.
Yosys reads past the end of the shipped 640-bit constant without complaint and
reports an area for a design that cannot exist. The gate therefore writes a
throwaway header of the correct width — at the shipped 74 % non-zero density,
because zeros really do drop out of the adder tree — next to a copy of the RTL,
so `` `include `` resolves to it rather than to `src/`.

### The budget

| quantity | value | source |
|---|---:|---|
| shipped, synthesised | 21 238 µm² | `area_gate.py --label shipped-real` |
| shipped, after the flow | 27 744 µm² | `runs/wokwi6`, 95.9 % core |
| flow multiplier | 1.306× | ratio of the two |
| 100 % of the core | 28 930 µm² | 27 744 / 0.959 |
| **synthesised budget** | **22 150 µm²** | 28 930 / 1.306 |

That is **+900 µm² over shipped, about twelve flip-flops**. The multiplier is
not uniform — it is hold-repair buffering on flip-flop self-loops, so it acts on
sequential area and barely touches combinational area. A candidate with more
flops than shipped is therefore downgraded to `TIGHT` even when the area passes,
and `TIGHT` means a real `harden_local.sh` run has to confirm it before it ships.

### Two structural constraints found in the RTL

`NHID`, `NPHASE` and `NFRAME` must all be **powers of two**. `HD_W =
$clog2(NHID)` with `c_hd = slot[HD_W-1:0]` and `c_ph = slot[SLOT_W-1 -: PH_W]`
mis-decode the phase for any other `NHID`; and `frame_idx = cnt[CNT_W-1:FRAME_LOG2]`
wraps at `2^FIDX_W`, not at `NFRAME`. So `NHID=6`, `NFRAME=12/20/24` are not
legal hardware, and results for them are reported below only as diagnostics.

### What fits

Measured, synthetic header (~+134 µm² versus a real one, so rows are compared
with each other):

| configuration | synth µm² | est. core | verdict |
|---|---:|---:|---|
| `NPHASE=1` | 19 992 | 90.3 % | FIT (but −2.7 AUC) |
| **`NFRAME=8`** | **20 797** | **93.9 %** | **FIT** |
| `NFRAME=8, TAP0=3` | 20 872 | 94.2 % | FIT |
| `MANT=0, FEAT_W=4` | 21 077 | 95.2 % | FIT |
| *shipped (synthetic header)* | *21 372* | *96.5 %* | *reference* |
| `NFRAME=8, NBAND=6, TAP0=3` | 21 674 | 97.8 % | TIGHT |
| `MANT=0, FEAT_W=3, NBAND=6` | 21 421 | 96.7 % | TIGHT |
| `MANT=0, FEAT_W=3, HACC_W=7` | 21 248 | 95.9 % | TIGHT |
| `NFRAME=12` | 21 958 | 99.1 % | TIGHT *(illegal: not 2ⁿ)* |
| `HACC_W=7` | 22 310 | 100.7 % | **FAIL** |
| `NBAND=6, TAP0=3` | 22 468 | 101.4 % | **FAIL** |
| `MANT=2, FEAT_W=5` | 22 347 | 100.9 % | **FAIL** |
| `NFRAME=20` / `NFRAME=24` | 22 167 / 22 891 | 100.1 / 103.3 % | **FAIL** |
| `NBAND=7, TAP0=2` | 23 620 | 106.6 % | **FAIL** |
| `NHID=8` | 26 023 | 117.5 % | **FAIL** |
| `NPHASE=4` | 25 087 | 113.3 % | **FAIL** |

Three changes *free up* area — `MANT=0` (−1 003 µm²), `NPHASE=1` (−1 380),
`NFRAME=8` (−575) — which is what makes this a joint search rather than a list:
a saving can pay for an upgrade that fails on its own. `NBAND=7` cannot be
bought even with the halved ROM (`NFRAME=8, NBAND=7` = 22 559, FAIL).

---

## 4. Results

### 4.1 Front end (stage B) — shipped classifier on each feature set

Four seeds, ranked by mean validation AUC. Area from the gate above.

| front end | val AUC | test AUC | area | fits? |
|---|---:|---:|---:|---|
| `NBAND=7, TAP0=2` | 90.03 ± 0.33 | 91.87 | 23 620 | ✗ |
| `NBAND=6, TAP0=3` | 89.02 ± 0.66 | 91.25 | 22 468 | ✗ alone |
| `TAP0=3` | 88.02 ± 0.28 | 90.04 | 21 505 | ✓ |
| `TAP0=2` | 87.60 ± 0.12 | 88.53 | 21 478 | ✓ |
| *shipped* | *87.55 ± 0.41* | *89.39* | *21 238* | ✓ |
| `K_SHIFT=3` | 86.53 ± 0.43 | 86.97 | 21 599 | ✓ |
| `MANT=2, FEAT_W=5` | 85.52 ± 0.92 | 87.58 | 22 347 | ✗ |
| `MANT=0, FEAT_W=3` | 84.80 ± 0.34 | 86.36 | 20 369 | ✓ |
| `K_SHIFT=1` | 78.95 ± 0.61 | 79.61 | 21 647 | ✓ |

More bands is the strongest front-end lever and the one the tile cannot afford.
Widening the poles (`K_SHIFT=1`) is catastrophic. Coarsening the log to 6 dB
costs 2.75 points — the `FEAT_W=3` clamp at 7 may be part of that, which is why
`MANT=0, FEAT_W=4` is being extracted separately.

### 4.2 Silicon constants (stage C) — shipped front end

| change | val AUC | test AUC | area verdict |
|---|---:|---:|---|
| `NHID=8` | 91.08 ± 0.40 | 92.78 | FAIL (26 023) |
| **`NFRAME=8`** | **90.08 ± 0.35** | **92.60** | **FIT (20 797)** |
| `NHID=6` | 89.81 ± 0.29 | 91.70 | illegal (not 2ⁿ) |
| `NFRAME=12` | 88.88 ± 0.36 | 90.96 | illegal (not 2ⁿ) |
| `NPHASE=4` | 88.71 ± 1.47 | 90.59 | FAIL (25 087) |
| unbounded accumulator | 88.53 ± 0.30 | 90.23 | — (diagnostic) |
| *shipped* | *87.55 ± 0.41* | *89.39* | *reference* |
| `HACC_W=7` | 87.40 ± 0.65 | 89.75 | FAIL (22 310) |
| `HACC_W=8` | 87.38 ± 0.40 | 89.98 | FAIL (23 125) |
| `WL=3` (int3 weights) | 86.16 ± 0.46 | 87.96 | needs new RTL |
| `NFRAME=20` | 86.00 ± 0.17 | 88.47 | FAIL + illegal |
| `NPHASE=1` | 84.85 ± 0.25 | 86.11 | FIT (19 992) |
| `WL=7` (int4 weights) | 79.97 ± 0.24 | 81.53 | needs new RTL |

**`NFRAME=8` is a strict improvement: +2.5 validation points and 575 µm²
cheaper than shipped.** A 335 ms window at a 168 ms hop gives five window
positions over a one-second clip instead of two, and the max over them finds the
most discriminative part of the word rather than averaging the whole of it.

**The 6-bit accumulator was never the bottleneck.** Widening it to 7 or 8 bits
changes nothing (87.40 / 87.38 against 87.55), and even an unbounded accumulator
only reaches 88.53. Saturation is not what is costing the 5 points to the fp32
ceiling.

**Higher weight precision hurts**, reproducing `FINDINGS.md` §5b on the
two-layer model: int3 loses 1.4 points, int4 loses 7.6. Ternary is not a
compromise here, it is a regulariser.

### 4.3 Training-only dimensions (stage A)

Nothing in the free dimensions moved the shipped configuration more than seed
noise (±0.4). The best were `lr_w1=0.04` (87.79), `pool=max` (87.77) and
`epochs=1000` (87.74) against the baseline's 87.55 — all inside one standard
deviation.

Two dimensions hurt badly and are worth recording:

| change | val AUC | why it was tried |
|---|---:|---|
| `train_hop=1` | 78.13 ± 0.93 | train on every window start, not just the hardware's |
| `train_hop=2` | 79.63 ± 0.60 | as above, half the density |
| `pool=mean` / `pool=topk` | 83.63 ± 0.78 | alternatives to log-sum-exp bag pooling |

Training on a denser set of window positions than the chip evaluates costs nine
points. The bag the loss pools over has to be the bag the hardware actually
scores; making it richer teaches the model to rely on alignments it will never
see.

---

## 5. Rejected

| candidate | rejected by | number |
|---|---|---|
| `NHID=8`, `NHID=6` | area / legality | 26 023 µm² (117 % core); `NHID` must be 2ⁿ |
| `HACC_W=7`, `HACC_W=8` | area **and** accuracy | 22 310 µm² FAIL, and no AUC gain anyway |
| `NBAND=7` | area, even with `NFRAME=8` | 23 620 / 22 559 µm² |
| `NPHASE=4` | area | 25 087 µm² (113 % core) |
| `NPHASE=1` | accuracy | fits at 19 992 µm² but −2.7 AUC |
| `NFRAME=20/24` | area and legality | 22 167 / 22 891 µm²; not a power of two |
| `MANT=2, FEAT_W=5` | area and accuracy | 22 347 µm², −2.0 AUC |
| `K_SHIFT=1` | accuracy | −8.6 AUC |
| int3 / int4 weights | accuracy (and new RTL) | −1.4 / −7.6 AUC |
| `train_hop=1/2` | accuracy | −9.4 / −7.9 AUC |
