#!/usr/bin/env bash
# Synthesise tt_um_wakeword against the IHP sg13g2 liberty and report
# utilisation of the 202.08 x 154.98 um (31_318 um^2) 1x1 tile.
set -euo pipefail
cd "$(dirname "$0")/.."
LIB=artifacts/sg13g2.lib
TILE=31318
printf "%-40s %7s %7s %10s %9s\n" "config" "cells" "flops" "area/um2" "tile-util"
run() {
  local name=$1; shift
  local out
  out=$(yosys -p "read_verilog -sv -I wakeword/src wakeword/src/tt_um_wakeword.sv
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
run "as specified (8 band, 2 word, 4 phase)" ""
run "1 keyword"                  "-set NWORD 1"
run "2 phases"                   "-set NPHASE 2"
run "1 keyword, 2 phases"        "-set NWORD 1 -set NPHASE 2"
run "6 bands, 1 keyword, 2 phase" "-set NBAND 6 -set NWORD 1 -set NPHASE 2"
run "state 12b, 1 word, 2 phase" "-set STATE_W 12 -set NWORD 1 -set NPHASE 2"
