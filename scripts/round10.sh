#!/usr/bin/env bash
# Round 10: does the second statistic survive being buildable?
#
# Round 5 is the largest result in this search: adding a per-frame *mean*
# beside the max is worth +2.4 to +9.0 test AUC, far more than any cascade
# change, and adding a per-frame *min* instead is worth less than nothing. So
# it is the average energy the max throws away that these detectors want.
#
# But `mean` as measured is not buildable. It is an exact per-frame average,
# and each band ticks a different number of times per frame
# (2^(FRAME_LOG2-b)), so an exact mean needs a per-band divisor. The buildable
# version is a leaky integrator carried across frame boundaries:
#
#     m += (f - m) >> K
#
# one accumulator and one shift per band, the same arithmetic the cascade
# already does NSTAGE times per tick. K sets the time constant: at band 8
# (243 Hz, 256 ticks per frame) K=2 averages over ~4 ticks and K=4 over ~16,
# so K trades "almost the last sample" against "almost a frame".
#
# Hypothesis: emaK reproduces mean's gain. If it does not, the gain is real but
# unreachable and round 5 has to be reported as a negative.
#
# One extraction per task emits max, mean and all three emaK together -- the
# cost is the per-tick cascade, which is shared -- and probe --keep-stats
# scores each pairing from it. `max` alone is the control and must reproduce
# the round 1 baseline.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="babycry catmeow clap dogbark mosquito siren vad water"
J=18

for t in $TASKS; do
  f="artifacts/ww_feats_${t}_ema.npz"
  [ -f "$f" ] || $PY train/optim/fe_stats.py --task "$t" --tag "${t}_ema" \
      --stats max,mean,ema2,ema3,ema4 --frames 24 --jobs $J 2>&1 | tail -1
  for k in max max,mean max,ema2 max,ema3 max,ema4 ema3; do
    for nf in 4 8; do
      $PY train/optim/probe.py --tag "${t}_ema" --nframe $nf --seeds 2 --epochs 40 \
          --ladder mlp32,mlp64 --keep-stats "$k" --note "r10-$k" 2>&1 \
          | grep -E "^HEADROOM" | sed "s|$|  [$k nf$nf]|"
    done
  done
done
echo "=== round 10 done ==="
