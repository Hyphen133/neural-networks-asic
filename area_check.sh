#!/usr/bin/env bash
# Synthesise tt_um_wakeword against the IHP sg13g2 liberty and report
# utilisation of the 202.08 x 154.98 um (31_318 um^2) 1x1 tile.
#
#   ./area_check.sh                 # host yosys
#   YOSYS="docker run --rm -v $PWD:/w -w /w ghcr.io/librelane/librelane:3.0.6 yosys" ./area_check.sh
#
# Needs artifacts/sg13g2.lib (gitignored): copy sg13g2_stdcell_typ_1p20V_25C.lib
# from the ihp-sg13g2 PDK.
set -euo pipefail
cd "$(dirname "$0")"
YOSYS=${YOSYS:-yosys}
LIB=${LIB:-artifacts/sg13g2.lib}
TILE=31318
printf "%-40s %7s %7s %10s %9s\n" "config" "cells" "flops" "area/um2" "tile-util"
run() {
  local name=$1; shift
  local out
  out=$($YOSYS -p "read_verilog -sv -I src src/tt_um_wakeword.sv
    chparam $* tt_um_wakeword
    synth -top tt_um_wakeword -flatten
    dfflibmap -liberty $LIB
    abc -liberty $LIB
    opt_clean -purge
    stat -liberty $LIB" 2>/dev/null | tail -70)
  local c a f
  c=$(awk '$3=="cells"{print $1}' <<<"$out"|tail -1)
  a=$(awk -F: '/Chip area for module/{gsub(/ /,"",$2);print $2}' <<<"$out"|tail -1)
  f=$(awk '$3 ~ /^sg13g2_(dfrbpq|dfrbp|sdfbbp)/{s+=$1} END{print s+0}' <<<"$out")
  awk -v n="$name" -v c="$c" -v a="$a" -v f="$f" -v t=$TILE \
    'BEGIN{printf "%-40s %7d %7d %10.0f %8.1f%%\n",n,c,f,a,100*a/t}'
}
run "shipped (H=4, 7b acc, 2 phase, 5 band)" ""
run "fallback: STATE_W=9"                    "-set STATE_W 9"
run "stretch: H=8, 8b acc (needs 2x1)"       "-set NHID 8 -set HACC_W 8"
