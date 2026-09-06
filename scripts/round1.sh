#!/usr/bin/env bash
# Round 1: does the shipped eight-task geometry fit, and where is the headroom?
#
#   1a  area_gate.py on each real emitted header at its own geometry
#       (TAP0=3 NBAND=6 NFRAME=8 -- area_gate's DEFAULTS are the *drone*
#       geometry and chparam overrides the ifdef, so --set is mandatory)
#   1b  probe.py headroom ladder on each task's cached features
#
# Detached, one log, resumable: both loops skip nothing, they are cheap enough
# to just re-run.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="babycry catmeow clap dogbark mosquito siren vad water"
GEOM="--set TAP0=3 --set NBAND=6 --set NFRAME=8"

echo "=== 1a: area gate, real headers, sheila_nb6 geometry ==="
for t in $TASKS; do
  $PY train/optim/area_gate.py --label "r1_$t" $GEOM \
      --header "artifacts/headers/ww_weights_$t.svh" || echo "  $t: gate said FAIL"
done

echo
echo "=== 1b: headroom ladder (val-selected, 3 seeds) ==="
for t in $TASKS; do
  $PY train/optim/probe.py --tag "$t" --nframe 8 --seeds 3 --epochs 40 \
      --note r1-baseline 2>&1 | grep -E "^tag=|^  |^HEADROOM"
done
echo "=== round 1 done ==="
