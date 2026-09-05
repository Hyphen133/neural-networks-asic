#!/usr/bin/env bash
# Re-run all eight detectors with the microphone model driven properly.
#
# docs/babycry.md: the sigma-delta mic model is fed
# peak-normalise(0.7) x random-attenuation(0.25..1) x gain(0.5), so a typical
# clip drives it at ~0.18 of its usable +-1 range and about 11 dB of SNR is
# thrown away before the cascade sees anything. At --pdm-gain 2.0 typical peaks
# land near 0.7, which is how a real microphone's sensitivity would be set.
#
# On babycry that is worth +2.06 validation / +4.6 test on the *real ternary
# model*, not just an fp32 probe -- larger than anything the 25-round
# architecture search found for that task, and it costs no area at all: no RTL
# change, no re-gate, only re-extraction and retraining.
#
# It is a property of the mic model, not of babycry, so it should apply to all
# eight. This measures that.
#
# Two feature sets per task where they differ:
#   _g20     STATE_W=9  -- the geometry the four adopters ship (design A)
#   _g20s10  STATE_W=10 -- the geometry catmeow/clap/water/mosquito kept,
#                          because STATE_W=9 made those four worse
# The trainer config is identical in both cases and identical to round 0, so
# the only variable is the features.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
ALL="babycry catmeow clap dogbark mosquito siren vad water"
KEPT_R0="catmeow clap water mosquito"
CFG='{"nframe": 8, "nphase": 2, "H": 4, "accw": 6, "epochs": 1000}'

echo "=== extract at pdm_gain=2.0 ==="
for t in $ALL; do
  [ -f "artifacts/ww_feats_${t}_g20.npz" ] || \
    $PY train/extract_clips.py --task "$t" --tag "${t}_g20" --cache-tag "$t" \
        --nband 6 --tap0 3 --nstage 9 --state-w 9 --pdm-gain 2.0 --jobs 18 \
        2>&1 | tail -1
done
for t in $KEPT_R0; do
  [ -f "artifacts/ww_feats_${t}_g20s10.npz" ] || \
    $PY train/extract_clips.py --task "$t" --tag "${t}_g20s10" --cache-tag "$t" \
        --nband 6 --tap0 3 --nstage 9 --state-w 10 --pdm-gain 2.0 --jobs 18 \
        2>&1 | tail -1
done

echo
echo "=== chip model, 6 seeds, selection on validation ==="
for t in $ALL; do
  $PY train/optim/grid.py --tag "${t}_g20" --seeds 6 --note g20 --base "$CFG" \
      2>&1 | grep -E "^\[ +1/" | sed "s|$|  [$t STATE_W=9]|"
done
for t in $KEPT_R0; do
  $PY train/optim/grid.py --tag "${t}_g20s10" --seeds 6 --note g20s10 --base "$CFG" \
      2>&1 | grep -E "^\[ +1/" | sed "s|$|  [$t STATE_W=10]|"
done
echo "=== gain20 done ==="
