#!/usr/bin/env bash
# Harden the two shipped architectures, both of which are stale on disk.
#
#   arch 2  sheila  TAP0=3 NBAND=6 STATE_W=10 NFRAME=8  NPHASE=2  (no define)
#   arch 3  drone   TAP0=4 NBAND=5 STATE_W=10 NFRAME=16 NPHASE=2  (-DWW_WEIGHTS_DRONE)
#
# Why re-harden rather than trust runs/sheila_nb6 and runs/drone:
#   * runs/drone predates moving TAP0/NBAND/NFRAME/FRAME_LOG2 under the
#     WW_WEIGHTS_DRONE ifdef, so the drone has never been hardened in the
#     two-geometry RTL it now ships in.
#   * runs/sheila_nb6 predates the AVG_N/AVG_SHIFT parameters added to
#     tt_um_wakeword.sv. Those are proved equivalent to the pre-change RTL at
#     AVG_N=0 by a bounded sequential SAT miter, and AVG_N ships at 0, so the
#     netlist should be identical -- but "should be" is not a flow result, and
#     the point of hardening is to stop guessing about area.
#
# These use the weights CURRENTLY in src/ww_weights*.svh, which are the
# pdm_gain=0.5 headers. The +4.28 (drone) and +1.76 (sheila) from
# --pdm-gain 2.0 are NOT in them; exporting those would overwrite the tape-out
# headers and needs an explicit decision. Weight values do move area a little
# (the ROM folds into constants), so treat these figures as the geometry's
# rather than as the final part's.
#
# Sequential, not parallel: two LibreLane flows on one machine contend for
# every core, and what matters here is area and timing, not wall-clock.
# `|| true` so a failure in the first still lets the second run and both logs
# survive.
set -u
cd "$(dirname "$0")/.."

SP=/tmp/claude-1000/-home-yenj-PycharmProjects-tinytapeout-mnist-nn-asic/a8f65644-9cc8-4e93-9edd-4ca7960f5bbb/scratchpad
export TT_TOOLS=${TT_TOOLS:-$SP/tt-support-tools}
[ -d "$TT_TOOLS/tech/ihp-sg13g2" ] || {
  echo "FATAL: TT_TOOLS=$TT_TOOLS has no tech/ihp-sg13g2; re-clone" >&2; exit 1; }

echo "=== arch 2: sheila (TAP0=3 NBAND=6 STATE_W=10 NFRAME=8) ==="
TAG=sheila_arch2 WEIGHTS=sheila ./harden_local.sh || true

echo
echo "=== arch 3: drone (TAP0=4 NBAND=5 STATE_W=10 NFRAME=16) ==="
TAG=drone_arch3 WEIGHTS=drone ./harden_local.sh || true

echo
echo "=== summary ==="
for t in sheila_arch2 drone_arch3; do
  echo "--- $t ---"
  grep -E "Chip area|Flow complete|ERROR" "runs/$t.log" 2>/dev/null | tail -5 \
    || echo "  no log"
done
echo "=== harden_arch23 done ==="
