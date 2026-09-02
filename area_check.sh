#!/usr/bin/env bash
# Synthesise tt_um_mnist_nn against the real IHP sg13g2 standard-cell library
# and report cell area vs. the TinyTapeout 1x1 tile budget.
#
#   ./area_check.sh                # uses artifacts/sg13g2.lib, downloads if absent
#
# Tile geometry (ttihp): 202.08 x 154.98 um = 31_318 um^2 of tile.
# OpenLane places at 60% density by default, so the usable cell area is
# ~18_800 um^2 (the template notes ~80% has sometimes been made to work).

set -euo pipefail
cd "$(dirname "$0")"

LIB=artifacts/sg13g2.lib
URL=https://raw.githubusercontent.com/IHP-GmbH/IHP-Open-PDK/main/ihp-sg13g2/libs.ref/sg13g2_stdcell/lib/sg13g2_stdcell_typ_1p20V_25C.lib

mkdir -p artifacts
[ -f "$LIB" ] || curl -sSfL -o "$LIB" "$URL"

TILE_AREA=31318          # 202.08 x 154.98 um
printf "%-34s %7s %7s %10s %10s\n" "config" "cells" "flops" "area/um2" "tile-util"

run() {  # run <hidden> <ternary> <shift_fixed> <acc_w>
  local h=$1 t=$2 sf=${3:-16} aw=${4:-20} out cells area flops
  out=$(yosys -p "
    read_verilog -sv src/tt_um_mnist_nn.sv
    chparam -set HIDDEN $h -set MULT_TERNARY $t -set SHIFT_FIXED $sf -set ACC_W $aw tt_um_mnist_nn
    synth -top tt_um_mnist_nn -flatten
    dfflibmap -liberty $LIB
    abc -liberty $LIB
    opt_clean -purge
    stat -liberty $LIB
  " 2>/dev/null | tail -60)

  cells=$(awk '$3 == "cells" {print $1}' <<<"$out" | tail -1)
  area=$(awk -F: '/Chip area for module/ {gsub(/ /,"",$2); print $2}' <<<"$out" | tail -1)
  flops=$(awk '$3 ~ /^sg13g2_(dfrbp|dfrbpq|sdfbbp|dllrq|dlhq)/ {s+=$1} END {print s+0}' <<<"$out")

  awk -v c="$cells" -v a="$area" -v f="$flops" -v t="$TILE_AREA" \
      -v n="H=$h tern=$t shift=$sf accw=$aw" \
    'BEGIN {printf "%-34s %7d %7d %10.0f %9.1f%%\n", n, c, f, a, 100*a/t}'
}

for h in 8 16 32; do run "$h" 0 16; done
for h in 8 16 32; do run "$h" 1 16; done
for h in 16 32; do run "$h" 0 6; done
run 16 0 16 16      # narrow accumulator
run 16 0 6  16      # narrow accumulator + hard-wired shift
