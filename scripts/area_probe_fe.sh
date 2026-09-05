#!/usr/bin/env bash
# Round 3 area probes: what does each front-end axis cost?
#
# Runs alongside round3.sh -- yosys is CPU/docker, the headroom probes are GPU,
# so the accuracy answer and the fit answer arrive together.
#
# Baseline for all of these is the geometry the eight tasks ship at:
# TAP0=3 NBAND=6 NFRAME=8 NSTAGE=9 K_SHIFT=2 STATE_W=10 MANT=1 FEAT_W=4
# FRAME_LOG2=16. Only 536 um^2 is free above the heaviest of the eight real
# headers, so STATE_W=9 (worth 883 um^2 in round 2) is tried as the funding
# source for anything that does not fit on its own.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
G="--set TAP0=3 --set NBAND=6 --set NFRAME=8"
G7="--set TAP0=3 --set NBAND=7 --set NFRAME=8 --set NSTAGE=10"
M2="--set MANT=2 --set FEAT_W=5"

run() { $PY train/optim/area_gate.py --label "$1" "${@:2}" || true; }

run r3_ref        $G
run r3_st9        $G --set STATE_W=9
run r3_k3         $G --set K_SHIFT=3
run r3_m2         $G $M2
run r3_fl15       $G --set FRAME_LOG2=15
run r3_fl17       $G --set FRAME_LOG2=17
run r3_nf16       $G --set NFRAME=16
run r3_nb7        $G7
run r3_nb7_st9    $G7 --set STATE_W=9
run r3_m2_st9     $G $M2 --set STATE_W=9
run r3_nb7_m2     $G7 $M2
run r3_nb7_m2_st9 $G7 $M2 --set STATE_W=9
run r3_nf16_st9   $G --set NFRAME=16 --set STATE_W=9
echo "=== front-end area probes done ==="
