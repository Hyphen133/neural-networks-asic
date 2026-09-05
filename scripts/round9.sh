#!/usr/bin/env bash
# Round 9: score every front end that actually fits, on every task.
#
# Round 7's frontier says 35 of 72 points fit, and that the reachable menu is
# narrower than it looks: NBAND=7 and MANT=2 never fit together, NBAND=7 needs
# both NFRAME<=4 and STATE_W=9, and MANT=2 needs one of the two. So there are
# three front ends worth measuring against the current one:
#
#   st9      NBAND=6 MANT=1 STATE_W=9   the cheap end; funds everything else
#   m2st9    NBAND=6 MANT=2 STATE_W=9   1.5 dB log steps
#   nb7st9   NBAND=7 MANT=1 STATE_W=9   an extra octave down to 121 Hz
#
# all at FRAME_LOG2=16, because round 3 found FRAME_LOG2 15 and 17 worth well
# under a point either way on every task that was not already better served by
# a different window length.
#
# Each is probed at NFRAME 2, 4 and 8. That matters: NFRAME is what pays for
# the other two, and round 3's control found NFRAME=4 scoring as well as 8 on
# catmeow and babycry at half the weight ROM.
#
# All eight tasks, so the per-task choice is made on evidence rather than on
# the four pilots. Extraction is skipped where round 4 already produced the
# feature file.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="babycry catmeow clap dogbark mosquito siren vad water"
J=18

probe() {  # tag nframe note
  $PY train/optim/probe.py --tag "$1" --nframe "$2" --seeds 2 --epochs 40 \
      --ladder mlp32,mlp64 --note "$3" 2>&1 | grep -E "^HEADROOM" \
      | sed "s|$|  [$3 nf$2]|"
}

for t in $TASKS; do
  for nf in 2 4 8; do probe "$t" $nf "r9-base"; done

  for v in "st9 --state-w 9" \
           "m2st9 --mant 2 --feat-w 5 --state-w 9" \
           "nb7st9 --nband 7 --nstage 10 --state-w 9"; do
    set -- $v
    name=$1; shift
    if [ ! -f "artifacts/ww_feats_${t}_${name}.npz" ]; then
      $PY train/extract_clips.py --task "$t" --tag "${t}_${name}" \
          --cache-tag "$t" --jobs $J "$@" 2>&1 | tail -1
    fi
    for nf in 2 4 8; do probe "${t}_${name}" $nf "r9-${name}"; done
  done
done
echo "=== round 9 done ==="
