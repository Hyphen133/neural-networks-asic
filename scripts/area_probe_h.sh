#!/usr/bin/env bash
# Round 2 area probes: what classifier capacity fits at the NFRAME=8 geometry?
#
# The eight-task headroom map says the ternary H=4 template sits 2-6 AUC below
# an fp32 MLP-32 on the *same* features, and that a bare linear read-out is
# 7-12 below that MLP. Hidden width is therefore the biggest classifier lever
# left. NHID=8 was measured FAIL once before -- but at NFRAME=16, where the
# weight ROM is twice this size. Re-measure at the geometry actually in use.
#
# NHID must be a power of two (c_hd = slot[HD_W-1:0]), so 4 -> 8 is the only
# step available.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
G="--set TAP0=3 --set NBAND=6 --set NFRAME=8"

run() { $PY train/optim/area_gate.py --label "$1" "${@:2}" || true; }

run r2_h4_ref      $G                                     # the current design
run r2_h8          $G --set NHID=8
run r2_h8_dbg0     $G --set NHID=8 --set DEBUG_PINS=0
run r2_h8_nb5      --set TAP0=3 --set NBAND=5 --set NFRAME=8 --set NHID=8
run r2_h8_nf4      $G --set NHID=8 --set NFRAME=4
run r2_h8_sw9      $G --set NHID=8 --set SCORE_W=9
run r2_h8_sw9_dbg0 $G --set NHID=8 --set SCORE_W=9 --set DEBUG_PINS=0
run r2_nphase4     $G --set NPHASE=4
run r2_h8_st9      $G --set NHID=8 --set STATE_W=9
echo "=== area probes done ==="
