#!/usr/bin/env bash
# The two detectors that actually tape out, at the corrected mic drive.
#
# sheila and drone are the shipped parts (docs/DESIGN.md, docs/DRONE.md) and
# they come from different extractors than the eight tasks -- extract.py reads
# Speech Commands, extract_dads.py reads the DADS parquet -- so --pdm-gain had
# to be plumbed into both. If the +4.6 babycry got is really a property of the
# microphone model rather than of one corpus, these two should move as well,
# and they are where it would matter most.
#
# Geometry is each part's own, read off its cached cfg and left alone:
#   sheila  TAP0=3 NBAND=6 STATE_W=10 NFRAME=8  NPHASE=2   (runs/sheila_nb6)
#   drone   TAP0=4 NBAND=5 STATE_W=10 NFRAME=16 NPHASE=2   (runs/drone)
# Only the mic drive changes, so nothing about the silicon changes: same header
# widths, same area verdicts, no re-gate.
#
# The drone starts with a regression -- re-extract at the default gain and diff
# against the shipped features -- so any later difference can only be the drive.
# The shipped npz files are read, never written.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "=== drone (DADS) ==="
$PY train/extract_dads.py --tag dads_regress --cache-tag dads --jobs 18 2>&1 | tail -1
$PY -c "
import numpy as np
a=np.load('artifacts/ww_feats_dads_regress.npz')['feats']
b=np.load('artifacts/ww_feats_dads.npz')['feats']
print('DRONE REGRESSION', 'identical' if a.shape==b.shape and (a==b).all() else 'DIFFER')"
[ -f artifacts/ww_feats_dads_g20.npz ] || \
  $PY train/extract_dads.py --tag dads_g20 --cache-tag dads --pdm-gain 2.0 \
     --jobs 18 2>&1 | tail -1
for tag in dads dads_g20; do
  $PY train/optim/grid.py --tag "$tag" --seeds 6 --note g20-drone \
     --base '{"nframe": 16, "nphase": 2, "H": 4, "accw": 6, "epochs": 1000}' \
     2>&1 | grep -E "^\[ +1/" | sed "s|$|  [$tag]|"
done

echo
echo "=== sheila (Speech Commands) ==="
# Same regression the drone gets: re-extract at the DEFAULT drive with these
# flags and diff against the shipped features. If this says identical, the only
# thing separating sheila_nb6 from sheila_g20 is --pdm-gain.
$PY train/extract.py --targets sheila --tag sheila_regress \
   --aug 4 --neg-per-word 150 --nstage 9 --state-w 10 \
   --nband 6 --tap0 3 --mant 1 --feat-w 4 2>&1 | tail -1
$PY -c "
import numpy as np
a=np.load('artifacts/ww_feats_sheila_regress.npz')['feats']
b=np.load('artifacts/ww_feats_sheila_nb6.npz')['feats']
print('SHEILA REGRESSION', 'identical' if a.shape==b.shape and (a==b).all() else
      f'DIFFER {a.shape} vs {b.shape}')"
# Every front-end flag has to be restated. extract.py's defaults are wwhw's
# (nstage=11, state_w=15, feat_w=5), NOT the shipped sheila part's, and --aug /
# --neg-per-word decide the corpus itself -- a first attempt that passed only
# --nband/--tap0 produced a 12186-clip set at state_w=15 and was thrown away.
# These are the flags from docs/DESIGN.md that built runs/sheila_nb6.
[ -f artifacts/ww_feats_sheila_g20.npz ] || \
  $PY train/extract.py --targets sheila --tag sheila_g20 \
     --aug 4 --neg-per-word 150 --nstage 9 --state-w 10 \
     --nband 6 --tap0 3 --mant 1 --feat-w 4 \
     --pdm-gain 2.0 2>&1 | tail -2
for tag in sheila_nb6 sheila_g20; do
  $PY train/optim/grid.py --tag "$tag" --seeds 6 --note g20-sheila \
     --base '{"nframe": 8, "nphase": 2, "H": 4, "accw": 6, "epochs": 1000}' \
     2>&1 | grep -E "^\[ +1/" | sed "s|$|  [$tag]|"
done
echo "=== gain20_ship done ==="
