#!/usr/bin/env bash
# Export the pdm_gain=2.0 weights for the two shipped parts, then harden them.
#
# The gain fix is the only change in this project that moved the parts that
# actually tape out, and it moved them a long way at zero area:
#
#   drone   val 93.87 -> 99.02   test 94.48 -> 98.76   (+4.28)
#   sheila  val 91.57 -> 93.37   test 93.59 -> 95.35   (+1.76)
#
# Same geometry, same header widths, no RTL change -- only the microphone drive
# and therefore the trained weights differ. Until now those weights existed
# only in the grid; src/ww_weights*.svh still held the gain=0.5 headers, so
# every hardening run so far measured the old part.
#
# finalise.py trains the winning configuration over many seeds, picks the seed
# by VALIDATION AUC (test is never consulted), emits the header, and re-scores
# that header through the independent integer model in train/eval_header.py. If
# the two AUCs disagree the export is wrong -- that check is the reason to go
# through finalise.py rather than emitting from the trainer directly.
#
# --out deliberately targets the tape-out headers, which finalise.py otherwise
# refuses to touch by defaulting to a scratch path. Both are tracked, so
# `git checkout src/ww_weights.svh src/ww_weights_drone.svh` restores the
# gain=0.5 headers exactly.
#
# Seed budgets differ because the corpora do: sheila is 13k clips (~60 s/seed),
# dads is 114k (~460 s/seed), so 16 and 8 seeds cost about the same wall time.
#
# Known: arch 2 (sheila) failed detailed placement on the gain=0.5 weights
# (DPL-0036, 300 hold buffers, 82.13 % utilisation against sheila_nb6's clean
# 81.15 %). Different weights fold into a different constant ROM and so a
# different area, and the frame-mean feature costs 127 um^2 even disabled
# (measured: 1266 cells before it, 1275 after), so this may or may not clear.
# The run reports either way rather than retrying silently.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

SP=/tmp/claude-1000/-home-yenj-PycharmProjects-tinytapeout-mnist-nn-asic/a8f65644-9cc8-4e93-9edd-4ca7960f5bbb/scratchpad
export TT_TOOLS=${TT_TOOLS:-$SP/tt-support-tools}
[ -d "$TT_TOOLS/tech/ihp-sg13g2" ] || {
  echo "FATAL: TT_TOOLS=$TT_TOOLS has no tech/ihp-sg13g2; re-clone" >&2; exit 1; }

echo "=== export sheila @ pdm_gain 2.0 -> src/ww_weights.svh ==="
$PY train/optim/finalise.py --tag sheila_g20 --name sheila \
    --set nframe=8 --set nphase=2 --set H=4 --set accw=6 --set epochs=1000 \
    --seeds 16 --out src/ww_weights.svh 2>&1 | tail -20

echo
echo "=== export drone @ pdm_gain 2.0 -> src/ww_weights_drone.svh ==="
$PY train/optim/finalise.py --tag dads_g20 --name drone \
    --set nframe=16 --set nphase=2 --set H=4 --set accw=6 --set epochs=1000 \
    --seeds 8 --out src/ww_weights_drone.svh 2>&1 | tail -20

echo
echo "=== harden arch 2: sheila @ gain 2.0 ==="
TAG=sheila_g20h WEIGHTS=sheila ./harden_local.sh || true

echo
echo "=== harden arch 3: drone @ gain 2.0 ==="
TAG=drone_g20h WEIGHTS=drone ./harden_local.sh || true

echo
echo "=== summary ==="
for t in sheila_g20h drone_g20h; do
  echo "--- $t ---"
  grep -E "Chip area|Utilization|hold buffers|Flow complete|DPL-0036|ERROR" \
    "runs/$t.log" 2>/dev/null | tail -6 || echo "  no log"
done
echo "=== finalise_g20 done ==="
