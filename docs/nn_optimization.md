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

## 2. Results

_(appended as each stage finishes)_

---

## 3. Rejected

_(with the measurement that rejected them)_
