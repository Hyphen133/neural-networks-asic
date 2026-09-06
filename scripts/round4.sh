#!/usr/bin/env bash
# Round 4: what does the funding source cost, and do the affordable
# combinations stack?
#
# Round 3's area probes say only three front-end changes are reachable:
#
#   FRAME_LOG2=15   +58 um^2   essentially free
#   MANT=2/FEAT_W=5 +809 um^2  needs STATE_W=9 to fit  (22666 -> 22032)
#   NFRAME=16       +740 um^2  needs STATE_W=9 to fit  (22597 -> 21690)
#
# and that NBAND=7 (+1620) is out of reach even with STATE_W=9, and K_SHIFT=3
# costs catmeow 3.5 points of headroom for +58 um^2.
#
# STATE_W 10->9 pays for the other two, but it narrows the cascade state and
# its accuracy cost has never been measured. That is what this round is for:
# if STATE_W=9 costs more than MANT=2 gains, the whole branch is dead.
#
# Extract st9 on its own, then each affordable stack, and probe every one at
# the window lengths that fit.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="catmeow siren dogbark water"
J=18

probe() {  # tag nframe note
  $PY train/optim/probe.py --tag "$1" --nframe "$2" --seeds 2 --epochs 40 \
      --ladder mlp32,mlp64 --note "$3" 2>&1 | grep -E "^tag=|^  |^HEADROOM"
}
ext() {   # task suffix extra-args...
  $PY train/extract_clips.py --task "$1" --tag "$1_$2" --cache-tag "$1" \
      --jobs $J "${@:3}" 2>&1 | tail -2
}

for t in $TASKS; do
  echo "--- $t st9  (the funding source, alone)"
  ext "$t" st9 --state-w 9
  probe "${t}_st9" 8  r4-st9-nf8
  probe "${t}_st9" 16 r4-st9-nf16

  echo "--- $t m2st9"
  ext "$t" m2st9 --mant 2 --feat-w 5 --state-w 9
  probe "${t}_m2st9" 8 r4-m2st9

  echo "--- $t fl15st9"
  ext "$t" fl15st9 --frame-log2 15 --state-w 9
  probe "${t}_fl15st9" 8  r4-fl15st9-nf8
  probe "${t}_fl15st9" 16 r4-fl15st9-nf16

  echo "--- $t fl15m2st9"
  ext "$t" fl15m2st9 --frame-log2 15 --mant 2 --feat-w 5 --state-w 9
  probe "${t}_fl15m2st9" 8  r4-fl15m2st9-nf8
  probe "${t}_fl15m2st9" 16 r4-fl15m2st9-nf16
done
echo "=== round 4 done ==="
