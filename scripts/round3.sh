#!/usr/bin/env bash
# Round 3: which front-end change raises the headroom?
#
# Round 1 put the fp32 headroom on the current features at 63-84 %. Nothing the
# classifier does reaches 90 %, so the question is which axis of the front end
# is starving these detectors. Five candidates, each extracted from the *same*
# decoded clip cache (--cache-tag, so no corpus is decoded twice) and probed
# with the same validation-selected ladder:
#
#   k3     K_SHIFT 2->3   moves the six octaves from 243 Hz-15.5 kHz down to
#                         121 Hz-7.8 kHz. The corpora are 16 kHz sources, so
#                         today's top band is mostly empty above 8 kHz.
#   nb7    NBAND 6->7     keeps all six and adds 121-243 Hz underneath
#                         (needs NSTAGE 9->10).
#   m2     MANT 1->2      1.5 dB log steps instead of 3 dB (FEAT_W 4->5).
#   fl15   FRAME_LOG2 15  21 ms frames: twice today's time resolution.
#   fl17   FRAME_LOG2 17  84 ms frames: half the resolution, twice the span.
#
# Plus a free control: probe the *unchanged* features over a longer window
# (NFRAME 16 and 24), which costs only weight ROM.
#
# Four pilot tasks spanning the range: catmeow (best), siren and dogbark (mid),
# water (low but with headroom). If an axis pays here it is broadened to all
# eight in a later round.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
TASKS="catmeow siren dogbark water"
J=18

probe() {  # tag nframe note
  $PY train/optim/probe.py --tag "$1" --nframe "$2" --seeds 2 --epochs 40 \
      --ladder mlp32,mlp64 --note "$3" 2>&1 | grep -E "^tag=|^  |^HEADROOM"
}

echo "=== 3a: window length on the unchanged front end (weight ROM only) ==="
for t in $TASKS; do
  for nf in 16 24; do probe "$t" "$nf" "r3-nframe$nf"; done
done

echo
echo "=== 3b: front-end variants ==="
for t in $TASKS; do
  echo "--- $t k3"
  $PY train/extract_clips.py --task "$t" --tag "${t}_k3" --cache-tag "$t" \
      --k-shift 3 --jobs $J 2>&1 | tail -2
  probe "${t}_k3" 8 r3-k3

  echo "--- $t nb7"
  $PY train/extract_clips.py --task "$t" --tag "${t}_nb7" --cache-tag "$t" \
      --nband 7 --nstage 10 --jobs $J 2>&1 | tail -2
  probe "${t}_nb7" 8 r3-nb7

  echo "--- $t m2"
  $PY train/extract_clips.py --task "$t" --tag "${t}_m2" --cache-tag "$t" \
      --mant 2 --feat-w 5 --jobs $J 2>&1 | tail -2
  probe "${t}_m2" 8 r3-m2

  echo "--- $t fl15"
  $PY train/extract_clips.py --task "$t" --tag "${t}_fl15" --cache-tag "$t" \
      --frame-log2 15 --jobs $J 2>&1 | tail -2
  probe "${t}_fl15" 8  r3-fl15-nf8
  probe "${t}_fl15" 16 r3-fl15-nf16

  echo "--- $t fl17"
  $PY train/extract_clips.py --task "$t" --tag "${t}_fl17" --cache-tag "$t" \
      --frame-log2 17 --jobs $J 2>&1 | tail -2
  probe "${t}_fl17" 8 r3-fl17
done
echo "=== round 3 done ==="
