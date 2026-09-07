#!/usr/bin/env bash
# Train, export, verify and gate one detector at one geometry.
#
#   scripts/finalise_task.sh <feature-tag> <name> <nframe> <nphase> <accw> \
#                            <RTL --set args...>
#
#   scripts/finalise_task.sh siren_nb5t4 siren 4 1 5 \
#       NSTAT=2 AVG_SHIFT=3 TAP0=4 NBAND=5 NSTAGE=9 NFRAME=4 NPHASE=1 \
#       HACC_W=5 STATE_W=9
#
# Four steps, in the order that makes a claim about silicon defensible:
#
#   1. train N seeds and select on VALIDATION only          (finalise.py)
#   2. emit the weight header                               (qat.emit)
#   3. re-score that header through the independent integer chip model
#      (eval_header.py, via finalise.py) -- if the training AUC and the header
#      AUC disagree the export is wrong and the header must not be used,
#      whatever it scores
#   4. synthesise the REAL header at the geometry it was trained for
#      (area_gate.py --header) -- a candidate that does not fit is not an
#      improvement
#
# area_gate.py pushes its whole DEFAULTS dict through chparam, which overrides
# the per-build ifdef in the RTL, so every geometry parameter has to be passed
# explicitly here even when it matches a default.
#
# Headers land in artifacts/headers/, never in src/: nothing here changes what
# tapes out.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

TAG=$1; NAME=$2; NF=$3; NPH=$4; ACCW=$5; shift 5
SEEDS=${SEEDS:-8}
# Anything else the trainer needs, as Cfg fields: EXTRA_SET="H=2 wt=0.9". The
# five positional arguments do not cover H, which the pruning study moves.
EXTRA_SET=${EXTRA_SET:-}
HDR="artifacts/headers/ww_weights_${NAME}.svh"
SETS=""
for kv in "$@"; do SETS="$SETS --set $kv"; done
CFG_SETS=""
for kv in $EXTRA_SET; do CFG_SETS="$CFG_SETS --set $kv"; done

mkdir -p artifacts/headers
echo "=== $NAME: train from $TAG (nframe=$NF nphase=$NPH accw=$ACCW$EXTRA_SET, $SEEDS seeds)"
$PY train/optim/finalise.py --tag "$TAG" --name "$NAME" --seeds "$SEEDS" \
    --out "$HDR" --set-fpr 0.05 \
    --set "nframe=$NF" --set "nphase=$NPH" --set "accw=$ACCW" \
    --set epochs=1000 $CFG_SETS || exit 1

echo "=== $NAME: fit gate on the real header"
$PY train/optim/area_gate.py --label "final_$NAME" --header "$HDR" $SETS
