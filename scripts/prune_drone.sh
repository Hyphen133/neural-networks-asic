#!/usr/bin/env bash
# Can drone_2 be pruned? -- area first, accuracy second.
#
# drone_2 (commit 184d7b7) ships at 98.92 % test AUC on a part that synthesises
# to 21 164 um^2, 205 flip-flops, an estimated 95.5 % of the core. It is the
# most accurate detector in this repo and the tightest-fitting one, so the
# question is not "can it be made better" but "what does it cost to make it
# smaller". Every earlier search asked the opposite question and only ever
# measured configurations that grow.
#
# Three kinds of pruning, in increasing order of what they touch:
#
#   1. TEMPLATE SPARSITY. A ternary zero drops its term out of the adder tree
#      (src/tt_um_wakeword.sv:28), so a sparser template is a smaller chip with
#      identical geometry, identical flip-flop count and identical timing. The
#      knobs are qat.py's `wt` (quantiser dead zone) and `l1`.
#   2. CLASSIFIER GEOMETRY. NHID, NPHASE, NFRAME, HACC_W. NHID and NPHASE are
#      flip-flops (NSLOT = NPHASE x NHID accumulators of HACC_W bits); NFRAME
#      is weight ROM and adder tree only. All three must stay powers of two
#      (scripts/area_probe_h.sh: c_hd = slot[HD_W-1:0]).
#   3. FRONT END. NBAND, STATE_W, MANT. These change the features, so each one
#      needs its own extraction pass before it can be trained.
#
# Stage 1 (area) is ~1.3 s per configuration and needs no training, so it runs
# first and decides what is worth training at all. Stage 2 trains the
# classifier-side prunes at 250 epochs -- a quarter of the shipped recipe, and
# a fair ranking because every row including the baseline pays the same price.
# Stage 3 extracts and trains the front-end prunes. scripts/prune_finalise.sh
# then re-runs the survivors at the shipped 1000 epochs and prices the real
# headers they emit.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
STAGE="${1:-all}"

AREA=artifacts/optim/area_prune.jsonl
area() { $PY train/optim/area_gate.py --label "$1" --out "$AREA" "${@:2}" || true; }
# The shipped drone recipe, at a quarter of its epochs. area_gate.py's DEFAULTS
# are already this geometry, so an --set-free area row is drone_2's own.
base() { echo "{\"nframe\": 16, \"nphase\": 2, \"H\": 4, \"accw\": 6, \"epochs\": 250}"; }

# ---------------------------------------------------------------------------
if [ "$STAGE" = all ] || [ "$STAGE" = area ]; then
echo "=== 1a  what template sparsity is worth (drone geometry, synthetic headers) ==="
# drone_2's template is 259/320 non-zero = 81 %. Below that is what pruning
# would buy; above it is what a denser template costs.
for d in 1.0 0.9 0.81 0.7 0.6 0.5 0.4 0.3 0.2 0.1; do
  area "dens_$d" --density "$d"
done

echo
echo "=== 1b  classifier geometry, one parameter at a time ==="
area "geom_shipped"                                      # the reference row
area "geom_h2"   --set NHID=2
area "geom_h1"   --set NHID=1
area "geom_p1"   --set NPHASE=1
area "geom_nf8"  --set NFRAME=8
area "geom_nf4"  --set NFRAME=4
area "geom_nf2"  --set NFRAME=2
area "geom_acc5" --set HACC_W=5
area "geom_acc4" --set HACC_W=4
area "geom_sw9"  --set SCORE_W=9
area "geom_sw8"  --set SCORE_W=8

echo
echo "=== 1c  front end ==="
# Dropping the TOP band keeps the rotor hum: TAP0=5 NBAND=4 spans stages 5..8,
# so the cascade still needs all nine stages and only the fmax register, the
# ring slot and the adder tree go. Dropping the BOTTOM band instead
# (TAP0=4 NBAND=4, NSTAGE=8) also shortens the cascade -- more area back, but
# it is the band the drone lives in. Both are measured; one is expected usable.
area "fe_b4_hi"  --set TAP0=5 --set NBAND=4
area "fe_b3_hi"  --set TAP0=6 --set NBAND=3
area "fe_b4_lo"  --set NBAND=4 --set NSTAGE=8
area "fe_st9"    --set STATE_W=9
area "fe_st8"    --set STATE_W=8
area "fe_m0"     --set MANT=0 --set FEAT_W=3

echo
echo "=== 1d  combinations ==="
area "cmb_h2_p1"      --set NHID=2 --set NPHASE=1
area "cmb_h2_nf8"     --set NHID=2 --set NFRAME=8
area "cmb_p1_nf8"     --set NPHASE=1 --set NFRAME=8
area "cmb_h2_p1_nf8"  --set NHID=2 --set NPHASE=1 --set NFRAME=8
area "cmb_h2_p1_acc5" --set NHID=2 --set NPHASE=1 --set HACC_W=5
area "cmb_b4_h2_p1"   --set TAP0=5 --set NBAND=4 --set NHID=2 --set NPHASE=1
area "cmb_b4_st9"     --set TAP0=5 --set NBAND=4 --set STATE_W=9
area "cmb_b4_st9_p1"  --set TAP0=5 --set NBAND=4 --set STATE_W=9 --set NPHASE=1
area "cmb_all"        --set TAP0=5 --set NBAND=4 --set STATE_W=9 \
                      --set NPHASE=1 --set NHID=2 --set NFRAME=8
# The same shapes at half the template density: sparsity and geometry are
# independent, and this says whether they add up.
area "dens50_shipped" --density 0.5
area "dens50_p1_nf8"  --density 0.5 --set NPHASE=1 --set NFRAME=8
area "dens50_all"     --density 0.5 --set TAP0=5 --set NBAND=4 --set STATE_W=9 \
                      --set NPHASE=1 --set NHID=2 --set NFRAME=8
fi

# ---------------------------------------------------------------------------
if [ "$STAGE" = all ] || [ "$STAGE" = train ]; then
echo
echo "=== 2a  accuracy cost of every classifier-side prune (250 epochs, 4 seeds) ==="
# Stage 1a priced template sparsity at 300 um^2 between 81 % and 30 % density,
# inside the +-1000 um^2 a re-drawn synthetic template moves on its own: in
# this design the adder tree is not where the area is. wt and l1 stay in the
# sweep, but as a check that sparsity is free rather than as a lever.
$PY train/optim/grid.py --tag dads_g20 --seeds 4 --note prune-cls \
    --out artifacts/optim/dads_g20_prune.jsonl --base "$(base)" \
    --sweep H=1,2 nphase=1 nframe=2,4,8 accw=4,5 wt=0.9,1.1 l1=0.01

echo
echo "=== 2b  and the combinations, because coordinates do not compose ==="
# Every one of these is a FIT with room to spare: cmb_h2_p1_nf8 synthesises to
# 17 974 um^2 against the shipped 21 527. Rows already run above are skipped.
$PY train/optim/grid.py --tag dads_g20 --seeds 4 --note prune-cls-x --cross \
    --out artifacts/optim/dads_g20_prune.jsonl --base "$(base)" \
    --sweep H=2,4 nphase=1,2 nframe=8,16
fi

# ---------------------------------------------------------------------------
if [ "$STAGE" = all ] || [ "$STAGE" = fe-extract ]; then
echo
echo "=== 3a  front-end prunes: extract (CPU; runs alongside stage 2 on the GPU) ==="
# --cache-tag dads reuses the decoded int16 clip cache, so each of these is a
# cascade re-simulation, not a re-decode. pdm-gain 2.0 is what drone_2 ships at
# (scripts/gain20_ship.sh).
ex() {
  local tag=$1; shift
  [ -f "artifacts/ww_feats_${tag}.npz" ] || \
    $PY train/extract_dads.py --tag "$tag" --cache-tag dads --pdm-gain 2.0 \
        --jobs 18 "$@" 2>&1 | tail -1
}
ex dads_g20b4    --tap0 5 --nband 4
ex dads_g20b3    --tap0 6 --nband 3
ex dads_g20st9   --state-w 9
ex dads_g20m0    --mant 0 --feat-w 3
ex dads_g20b4st9 --tap0 5 --nband 4 --state-w 9
fi

if [ "$STAGE" = all ] || [ "$STAGE" = fe-train ]; then
echo
echo "=== 3b  front-end prunes: same recipe, one tag per feature set ==="
for t in dads_g20b4 dads_g20b3 dads_g20st9 dads_g20m0 dads_g20b4st9; do
  $PY train/optim/grid.py --tag "$t" --seeds 4 --note prune-fe \
      --out "artifacts/optim/${t}_prune.jsonl" --base "$(base)" \
      2>&1 | grep -E "^\[ +[0-9]+/|WARNING" | sed "s|\$|  [$t]|"
done
fi
