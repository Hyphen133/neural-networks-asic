#!/usr/bin/env bash
# Round 12: six bands of max, or four bands of max-and-average?
#
# Round 11 gated the NSTAT=2 RTL. The leaky average is not cheap -- a
# FEAT_W+AVG_SHIFT accumulator per band plus twice the adder tree -- and only
# four-band builds fit:
#
#   NBAND=4 NFRAME=2 STATE_W=9   21 427   TIGHT
#   NBAND=4 NFRAME=4 STATE_W=9   21 975   TIGHT
#   NBAND=4 NFRAME=2 STATE_W=10  22 128   TIGHT
#   NBAND=5, any                 23 949+  FAIL
#   NBAND=6, any                 26 146+  FAIL
#
# So the second statistic costs two bands. Round 5 measured six bands of
# max+mean, which does not fit; this measures what does.
#
# Which two bands are lost is a per-task choice, so TAP0 is swept with it:
#
#   TAP0=3  bands 3..6   971 Hz - 15.5 kHz   (drops the two lowest)
#   TAP0=4  bands 4..7   486 Hz - 7.8 kHz
#   TAP0=5  bands 5..8   243 Hz - 3.9 kHz    (drops the two highest)
#
# The control that decides the round is `max` alone on the same four bands: if
# 4-band max+ema beats 6-band max but not 4-band max, the gain is the band
# layout and not the average.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="babycry catmeow clap dogbark mosquito siren vad water"
J=18

for t in $TASKS; do
  for tap in 3 4 5; do
    tag="${t}_nb4t${tap}"
    ns=$((tap + 4))
    [ -f "artifacts/ww_feats_${tag}.npz" ] || \
      $PY train/optim/fe_stats.py --task "$t" --tag "$tag" \
          --stats max,ema2,ema3 --nband 4 --tap0 $tap --nstage $ns \
          --state-w 9 --frames 24 --jobs $J 2>&1 | tail -1
    for k in max max,ema2 max,ema3; do
      for nf in 2 4; do
        $PY train/optim/probe.py --tag "$tag" --nframe $nf --seeds 2 --epochs 40 \
            --ladder mlp32,mlp64 --keep-stats "$k" --note "r12-t${tap}-$k" 2>&1 \
            | grep -E "^HEADROOM" | sed "s|$|  [tap$tap $k nf$nf]|"
      done
    done
  done
done
echo "=== round 12 done ==="
