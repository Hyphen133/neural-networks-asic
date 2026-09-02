#!/usr/bin/env bash
# Harden tt_um_wakeword locally with the same LibreLane flow the TinyTapeout
# GitHub action runs, using the librelane Docker image. Mirrors
# tt-support-tools `tt_tool.py --create-user-config --harden`.
#
#   ./harden_local.sh                 # -> runs/wokwi
#   TAG=try2 EXTRA='"MAX_FANOUT_CONSTRAINT": 24' ./harden_local.sh
#
# Needs: docker, a checkout of TinyTapeout/tt-support-tools (TT_TOOLS), and a
# PDK root directory (PDK_ROOT, default ~/.ciel; the ihp-sg13g2 PDK is fetched
# on first use, ~300 MB).
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=${IMAGE:-ghcr.io/librelane/librelane:3.0.6}
TT_TOOLS=${TT_TOOLS:-$HOME/tt-support-tools}
PDK_ROOT=${PDK_ROOT:-$HOME/.ciel}
TAG=${TAG:-wokwi}
EXTRA=${EXTRA:-}

[ -d "$TT_TOOLS/tech/ihp-sg13g2" ] || {
  echo "TT_TOOLS=$TT_TOOLS does not look like tt-support-tools" >&2; exit 1; }

# What tt_tool.py --create-user-config writes for a 1x1 IHP tile.
cat > src/user_config.json <<EOF
{
  "DESIGN_NAME": "tt_um_wakeword",
  "VERILOG_FILES": ["dir::tt_um_wakeword.sv"],
  "DIE_AREA": "0 0 202.08 154.98",
  "FP_DEF_TEMPLATE": "dir::../tt/tech/ihp-sg13g2/def/tt_block_1x1_pgvdd.def",
  "VDD_PIN": "VPWR",
  "GND_PIN": "VGND",
  "RT_MAX_LAYER": "TopMetal1"${EXTRA:+,
  $EXTRA}
}
EOF
python3 - <<'EOF'
import json
cfg = json.load(open("src/config.json")); cfg.update(json.load(open("src/user_config.json")))
json.dump(cfg, open("src/config_merged.json", "w"), indent=2)
EOF

mkdir -p "runs/$TAG"
docker run --rm -v "$PWD":/work -v "$TT_TOOLS":/work/tt -v "$PDK_ROOT":/pdk \
  -w /work -e CI=1 "$IMAGE" \
  python3 -m librelane --pdk ihp-sg13g2 --pdk-root /pdk \
    --run-tag "$TAG" --force-run-dir "runs/$TAG" --hide-progress-bar \
    src/config_merged.json 2>&1 | tee "runs/$TAG.log" | \
  grep -E "Chip area|GPL-0019|fanout violations|hold buffers|DPL-00|ERROR|Flow complete|Saving" || true
