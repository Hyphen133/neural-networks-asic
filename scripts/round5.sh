#!/usr/bin/env bash
# Round 5: is a second statistic per band worth more than a seventh band?
#
# Rounds 3-4 bought about a point per axis and NBAND=7 (+1620 um^2) is out of
# reach. The front end keeps one number per band per frame -- the max log
# magnitude over 41.9 ms -- and throws the rest away. A second statistic costs
# one more FEAT_W register and one more comparator per band: no extra cascade
# stage, no extra decimator, no extra 10-bit state.
#
#   max,min   max-min is the band's modulation depth in the frame, which is
#             exactly what separates a transient (clap, dogbark, babycry) from
#             a steady tone
#   max,mean  a rounded running mean: an accumulator and a shift, since the
#             per-band tick count per frame is a power of two
#   max,min,mean
#
# Doubling the features per frame doubles the weight ROM, so each variant is
# probed at NFRAME=4 as well as 8 -- 4 frames x 12 features is the same 384-bit
# ROM the design ships with today, and therefore the version most likely to fit.
#
# train/optim/fe_stats.py's max plane was checked byte-identical to
# extract_clips.py's output on catmeow before this round was run.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="catmeow siren dogbark water clap babycry"
J=18

probe() {  # tag nframe note
  $PY train/optim/probe.py --tag "$1" --nframe "$2" --seeds 2 --epochs 40 \
      --ladder mlp32,mlp64 --note "$3" 2>&1 | grep -E "^tag=|^  |^HEADROOM"
}

for t in $TASKS; do
  for v in "mm max,min" "mmean max,mean" "mmm max,min,mean"; do
    set -- $v
    echo "--- $t $1 ($2)"
    $PY train/optim/fe_stats.py --task "$t" --tag "${t}_$1" --stats "$2" \
        --frames 24 --jobs $J 2>&1 | tail -2
    probe "${t}_$1" 4 "r5-$1-nf4"
    probe "${t}_$1" 8 "r5-$1-nf8"
  done
done
echo "=== round 5 done ==="
