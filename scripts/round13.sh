#!/usr/bin/env bash
# Round 13: buy the fifth band back with NPHASE=1 and HACC_W=5.
#
# Round 11 found the leaky average costs two bands at NPHASE=2. Round 12's
# four-band builds are the fallback, but two more savings were still untried:
#
#   NPHASE 2 -> 1   halves the hidden accumulator ring (NSLOT = NPHASE*NHID)
#   HACC_W 6 -> 5   narrows every one of them
#
# Together they make five bands fit with the average:
#
#   NSTAT=2 NBAND=5 NFRAME=2 NPHASE=1 HACC_W=5 STATE_W=9   20 979   FIT
#   NSTAT=2 NBAND=5 NFRAME=4 NPHASE=1 HACC_W=5 STATE_W=9   21 340   TIGHT
#   ... and NBAND=6 still fails at 23 370, so five is the ceiling.
#
# Neither saving is free in accuracy. NPHASE=1 makes the scored windows stop
# overlapping -- the multiple-instance bag gets coarser, and it must be the bag
# the hardware scores, so it is probed here rather than assumed. HACC_W=5 is a
# trainer-side parameter the fp32 probe cannot see at all; it is measured
# separately with qat.py.
#
# This round answers only: at NPHASE=1, is five bands with the average better
# than four bands with it, and better than six bands without it?
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="babycry catmeow clap dogbark mosquito siren vad water"
J=18

for t in $TASKS; do
  for tap in 3 4; do
    tag="${t}_nb5t${tap}"
    ns=$((tap + 5))
    [ -f "artifacts/ww_feats_${tag}.npz" ] || \
      $PY train/optim/fe_stats.py --task "$t" --tag "$tag" \
          --stats max,ema2,ema3 --nband 5 --tap0 $tap --nstage $ns \
          --state-w 9 --frames 24 --jobs $J 2>&1 | tail -1
    for k in max max,ema2 max,ema3; do
      for nf in 2 4; do
        $PY train/optim/probe.py --tag "$tag" --nframe $nf --nphase 1 --seeds 2 \
            --epochs 40 --ladder mlp32,mlp64 --keep-stats "$k" \
            --note "r13-nb5t${tap}-p1-$k" 2>&1 \
            | grep -E "^HEADROOM" | sed "s|$|  [nb5 tap$tap p1 $k nf$nf]|"
      done
    done
  done
done
echo "=== round 13 done ==="
