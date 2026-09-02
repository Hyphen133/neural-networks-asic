#!/usr/bin/env bash
# Synthesise tt_um_kws against the real IHP sg13g2 standard-cell library and
# report cell area vs. the TinyTapeout 1x1 tile budget.
#
#   ./area_check_kws.sh              # sweep the interesting configurations
#
# Tile geometry (ttihp): 202.08 x 154.98 um = 31_318 um^2 of tile.
# OpenLane places at 60% density by default -> ~18_800 um^2 of usable cell
# area; the template notes ~80% (~25_000 um^2) has sometimes been made to work.
#
# Configurations whose dimensions do not match src/kws_weights.svh synthesise
# against deterministic LFSR filler weights, which is what makes an untrained
# configuration's area meaningful rather than optimised away to nothing.

set -euo pipefail
cd "$(dirname "$0")"

LIB=artifacts/sg13g2.lib
URL=https://raw.githubusercontent.com/IHP-GmbH/IHP-Open-PDK/main/ihp-sg13g2/libs.ref/sg13g2_stdcell/lib/sg13g2_stdcell_typ_1p20V_25C.lib

mkdir -p artifacts
[ -f "$LIB" ] || curl -sSfL -o "$LIB" "$URL"

TILE_AREA=31318          # 202.08 x 154.98 um
DFF_AREA=52.6176         # sg13g2_dfrbp_1, for the flop-share column

printf "%-40s %7s %7s %9s %9s %9s\n" \
       "config (bands x slots, feat_w)" "cells" "flops" "area/um2" "tile-util" "flop-share"

run() {  # run <nbank> <nslot> <feat_w> <state_w> <env_w>
  local nb=$1 ns=$2 fw=$3 sw=${4:-14} ew=${5:-12} out cells area flops util share bands
  bands=$((nb - 1))
  out=$(yosys -p "
    read_verilog -sv -I src src/tt_um_kws.sv
    chparam -set NBANK $nb -set NSLOT $ns -set FEAT_W $fw \
            -set STATE_W $sw -set ENV_W $ew tt_um_kws
    synth -top tt_um_kws -flatten
    dfflibmap -liberty $LIB
    abc -liberty $LIB
    stat -liberty $LIB
  " 2>/dev/null)

  # The per-cell lines are "<count> <area> <cellname>", so a flop count means
  # column 1 of the matching rows -- not column 2, which is their total area.
  cells=$(echo "$out" | awk '/^ *Number of cells:/ {c=$4} END {print c+0}')
  area=$(echo "$out"  | awk '/Chip area for module/ {a=$NF} END {printf "%.0f", a+0}')
  flops=$(echo "$out" | awk '/^ +[0-9]+ +[0-9.E+]+ +sg13g2_(s?dfrbpq?|dlhq)/ {n+=$1} END {print n+0}')
  util=$(awk -v a="$area" -v t="$TILE_AREA" 'BEGIN{printf "%.1f%%", 100*a/t}')
  share=$(awk -v f="$flops" -v d="$DFF_AREA" -v a="$area" \
              'BEGIN{if(a>0) printf "%.0f%%", 100*f*d/a; else print "-"}')

  printf "%-40s %7s %7s %9s %9s %9s\n" \
         "${bands} bands x ${ns} slots, fw=${fw}, sw=${sw}" \
         "$cells" "$flops" "$area" "$util" "$share"
}

# --- the configurations that matter, cheapest first
run 5 4 4        # 4 bands, 341 ms context -- the smallest useful build
run 5 4 5
run 5 6 4
run 7 4 4        # 6 bands
run 7 4 5
run 7 6 4        # 6 bands x 6 slots -- the most accurate build measured
run 7 6 4 12     # ...with a narrower filterbank register
run 8 6 4 12     # 7 bands
