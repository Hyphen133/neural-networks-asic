#!/usr/bin/env bash
# babycry round 1: where are the 11.5 points to 90 % supposed to come from?
#
# Current: 78.54 test / 80.29 val, design A (STATE_W=9, 6 bands, max, NFRAME=8,
# NPHASE=2, NHID=4). Three things are unmeasured for this task and all three
# are cheap:
#
#   a) the fp32 ceiling on the features it actually uses, at the geometry it
#      actually ships. Round 1 measured it on STATE_W=10 features (76.60) and
#      the chip has since passed that, so the old number says nothing.
#      The ladder includes mlp4, matching NHID=4 -- the rung that predicts what
#      the hardware can use (docs/task_optimization.md round 24).
#
#   b) window length. An infant cry lasts 0.5-2 s; the chip integrates 335 ms.
#      NFRAME 16 and 24 were never tried on babycry at STATE_W=9.
#
#   c) band layout. An infant cry has F0 around 300-600 Hz with harmonics to
#      3-4 kHz. The current bands span 243 Hz - 15.5 kHz, so the top band sits
#      mostly above the content and nothing covers 121-243 Hz. K_SHIFT=3 moves
#      the whole comb down an octave; TAP0 4 and 5 drop the top band instead;
#      TAP0=2 adds one above. None was ever measured on babycry -- round 3 used
#      four other tasks.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "=== a+b) ceiling on the shipped geometry, and window length ==="
for nf in 4 8 16 24; do
  $PY train/optim/probe.py --tag babycry_fin --nframe $nf --nphase 2 --seeds 3 \
      --epochs 40 --ladder mlp4,mlp32,mlp256 --keep-stats max --note "bc1-nf$nf" 2>&1 \
      | grep -E "^  mlp" | sed "s|$|  [base nf$nf p2]|"
done

echo
echo "=== c) band layout, STATE_W=9, probed at the shipped geometry ==="
for v in "k3 --k-shift 3 --tap0 3 --nstage 9" \
         "t4 --tap0 4 --nstage 10" \
         "t5 --tap0 5 --nstage 11" \
         "t2 --tap0 2 --nstage 8"; do
  set -- $v
  name=$1; shift
  tag="babycry_$name"
  [ -f "artifacts/ww_feats_${tag}.npz" ] || \
    $PY train/extract_clips.py --task babycry --tag "$tag" --cache-tag babycry \
        --nband 6 --state-w 9 --jobs 18 "$@" 2>&1 | tail -1
  for nf in 8 16; do
    $PY train/optim/probe.py --tag "$tag" --nframe $nf --nphase 2 --seeds 3 \
        --epochs 40 --ladder mlp4,mlp32,mlp256 --note "bc1-$name-nf$nf" 2>&1 \
        | grep -E "^  mlp" | sed "s|$|  [$name nf$nf]|"
  done
done
echo "=== bc1 done ==="
