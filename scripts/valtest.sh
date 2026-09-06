#!/usr/bin/env bash
# Why validation and test disagree, and what actually shrinks the gap.
#
# At pdm_gain=2.0 the gap runs from +22.70 (mosquito) to -2.87 (dogbark). A
# 25-point spread cannot come from a mechanism that is identical for every
# task, so the archive was mined before any compute was spent here. What that
# mining already settled -- see docs/val_test_gap.md -- and what is left:
#
# SETTLED, NO RUN NEEDED:
#  * A shorter epoch budget does not help. Across catmeow/dogbark/clap/babycry,
#    epochs=400 loses test AUC in 3 of 4 against 1000, and 2000 wins in 3 of 4
#    (+0.92, +1.12, +0.62). Early stopping as a *budget cut* is refuted.
#  * The gap is flat in the epoch budget (catmeow 11.57/11.94/11.28 at
#    400/1000/2000). qat.run reports a MAXIMUM over epochs/eval_every
#    checkpoints, so if max-picking were inflating val, the gap would grow with
#    the number of draws. It does not. The metric artifact is small and the gap
#    is genuine distribution shift.
#  * aug_band is actively harmful -- the worst row of 36 in three of four tasks
#    (clap test -3.64, babycry -3.92, dogbark -1.32). The "per-band jitter
#    stands in for a different microphone's frequency response" story was
#    wrong. Not swept here.
#
# WHY THE GAP IS REAL (train/tasks.py, train/humbug.py): splits use each
# corpus' official split. FSD50K eval is uploader-disjoint from dev and val is
# carved out of dev, so val shares a pool with train that test does not.
# HumBugDB is starker: test is Tanzania-field and UK-culture -- different
# countries, sites and devices -- while val comes from the remaining train
# sites. Mosquito's +22.70 is the detector reading site and device signature,
# which val structurally cannot see.
#
# WHAT IS LEFT TO TEST, and it is what the archive pointed at rather than what
# was assumed going in:
#  A  epochs, both directions, since 2000 beat 1000 and nobody has gone past it
#  B  aug_time -- the best single knob in the archive (catmeow gap -2.13,
#     test +2.51 at aug_time=2), never swept past 2
#  C  the regularisers that moved test, not the ones that moved val
#
# All three at pdm_gain=2.0, which every archive row above predates, and on
# four tasks spanning the whole range so a mechanism has to explain the spread:
#     mosquito  +22.70   different sites/devices  (extreme; never in round 6)
#     vad       +16.61   large, differently composed test (never in round 6)
#     clap       +9.05   middling
#     dogbark    -2.87   test already beats val  (control)
# A change that helps only where the gap is large is a domain-shift fix; one
# that moves dogbark too is plain regularisation.
#
# Selection discipline unchanged: validation ranks, test is recorded and never
# chooses. Geometry frozen at the shipped 1x1 configuration -- every knob here
# is a `free` field in qat.Cfg, so nothing can cost area. Tasks run in
# parallel: the GPU is kernel-launch bound on this model (round6.sh).
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
BASE='{"nframe": 8, "nphase": 2, "H": 4, "accw": 6, "epochs": 1000}'

SWEEP=(
  epochs=250,2000,4000
  aug_time=1,2,3
  wd=0.0001,0.001
  label_smooth=0.05
  leak=0.2
  lr_w1=0.04,0.16
  sched=cos_warm
)

for t in mosquito vad clap dogbark; do
  $PY train/optim/grid.py --tag "${t}_g20" --seeds 6 --note vt-gap \
      --base "$BASE" --sweep "${SWEEP[@]}" \
      > "artifacts/logs/vt_$t.log" 2>&1 &
done
wait
echo "=== valtest done ==="
