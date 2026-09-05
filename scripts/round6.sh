#!/usr/bin/env bash
# Round 6: the eight detectors all use sheila's training recipe unchanged.
#
# docs/new_tasks.md 6 says so explicitly -- "no hyper-parameter search was run
# for any task". These corpora differ from Speech Commands in every way that
# matters to an optimiser: 4 625 to 43 499 clips, 8 % to 78 % positive,
# 92 to 3 330 validation positives. A recipe tuned on a balanced 105 000-clip
# wake-word set has no claim to being right here.
#
# Hypothesis: per-task tuning of the *free* knobs -- the ones that change no
# silicon at all -- is worth a point or more per task, at zero area.
#
# Coordinate sweep (one field at a time around the shipped recipe), 4 seeds,
# ranked by mean validation AUC. Test is recorded and never used to choose.
# Tasks run in parallel: the GPU is kernel-launch bound on this model, so
# concurrency costs almost nothing.
#
#   scripts/round6.sh catmeow siren dogbark water
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

SWEEP=(
  pool=max,topk,mean
  warmup=0.2,0.6
  epochs=400,2000
  aug_level=0.25,0.5
  aug_band=0.25
  aug_time=1,2
  label_smooth=0.05
  pos_weight=2.0
  leak=0.05,0.2
  lr_w1=0.04,0.16
  lr_b1=0.25,1.0
  wd=0.0001
  ema=0.99
  sched=cos_warm,const
  shift=0,2
  topk=3
)

for t in "$@"; do
  $PY train/optim/grid.py --tag "$t" --seeds 4 --note r6-recipe \
      --base '{"nframe": 8, "epochs": 1000}' --sweep "${SWEEP[@]}" \
      > "artifacts/logs/r6_$t.log" 2>&1 &
done
wait
echo "=== round 6 done ==="
