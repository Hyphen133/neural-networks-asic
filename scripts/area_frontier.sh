#!/usr/bin/env bash
# The affordable design space, enumerated once.
#
# Round 3 gated one axis at a time against NFRAME=8 and concluded that MANT=2
# needs STATE_W=9 and that NBAND=7 is unreachable. Both conclusions were an
# artefact of the window length: the probe control at NFRAME=4 scores as well
# as NFRAME=8 on catmeow and babycry while halving the weight ROM, and
# NFRAME=4 alone is worth 577 um^2.
#
# So enumerate the box properly instead of walking it one axis at a time:
#
#   NFRAME      2, 4, 8         weight ROM, and the window the chip scores
#   NBAND       6, 7            an extra octave underneath (NSTAGE 9 -> 10)
#   MANT        1, 2            3 dB vs 1.5 dB log steps (FEAT_W 4 -> 5)
#   STATE_W     9, 10           cascade precision; 9 frees ~880 um^2
#   FRAME_LOG2  15, 16, 17      21 / 41.9 / 84 ms frames
#
# 72 points. Every one is a synthetic header at the shipped 74 % weight
# density, which runs 250-700 um^2 heavier than the real headers the eight
# tasks emit, so a TIGHT here is comfortable in practice and a FAIL is real.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
OUT=artifacts/optim/area.jsonl

for nf in 2 4 8; do
 for nb in 6 7; do
  for mant in 1 2; do
   for sw in 10 9; do
    for fl in 15 16 17; do
      fw=$((mant == 2 ? 5 : 4))
      ns=$((nb == 7 ? 10 : 9))
      $PY train/optim/area_gate.py \
          --label "fr_nf${nf}_nb${nb}_m${mant}_sw${sw}_fl${fl}" \
          --set TAP0=3 --set NFRAME=$nf --set NBAND=$nb --set NSTAGE=$ns \
          --set MANT=$mant --set FEAT_W=$fw --set STATE_W=$sw \
          --set FRAME_LOG2=$fl --out $OUT || true
    done
   done
  done
 done
done
echo "=== area frontier done ==="
