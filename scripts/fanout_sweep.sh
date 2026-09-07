#!/usr/bin/env bash
# Sweep CTS knobs against the 14 max-fanout violations on drone_2.
#
#   nohup scripts/fanout_sweep.sh > artifacts/fanout_sweep.log 2>&1 &
#
# Every config here changes ONLY the clock tree. The RTL, the weight header and
# therefore the netlist logic are byte-identical to the shipped drone_2
# (runs/drone_g20h), so accuracy is unchanged by construction -- there is
# nothing to re-score. What can move is area, hold repair and routing, which is
# what the extractor at the bottom reports.
#
# Why the constraint cannot simply be raised: the violation is against the
# liberty's `default_max_fanout : 8`, and OpenSTA takes min(SDC, liberty), so
# MAX_FANOUT_CONSTRAINT can only ever lower the limit. Config `fo24` proves it.
#
# Serial by necessity: harden_local.sh rewrites src/user_config.json per run.
set -u
cd "$(dirname "$0")/.."

run() {  # run <tag> <extra-json>
  local tag=$1 extra=$2
  if [ -f "runs/fo_$tag/final/metrics.json" ]; then
    echo "=== fo_$tag: already done, skipping"
    return
  fi
  echo "=== fo_$tag: EXTRA=${extra:-<none>}"
  rm -rf "runs/fo_$tag"
  WEIGHTS=drone TAG="fo_$tag" EXTRA="$extra" ./harden_local.sh || echo "!!! fo_$tag FAILED"
}

# 1. control: must reproduce 14 violations / 94.596 % util
run base ''

# 2. prove the liberty limit binds, not the SDC
run fo24 '"MAX_FANOUT_CONSTRAINT": 24'

# 3. explicit cap on sinks per leaf cluster
run scs8  '"CTS_SINK_CLUSTERING_SIZE": 8'
run scs6  '"CTS_SINK_CLUSTERING_SIZE": 6'
run scs4  '"CTS_SINK_CLUSTERING_SIZE": 4'

# 4. clustering is cap-driven (CTS-0090), so squeeze the sink buffer cap budget
run der50 '"CTS_SINK_BUFFER_MAX_CAP_DERATE_PCT": 50'
run der80 '"CTS_SINK_BUFFER_MAX_CAP_DERATE_PCT": 80'

# 5. smaller sink buffer => smaller cap budget => smaller clusters
#    (CTS picks the first entry of CTS_CLK_BUFFERS as the sink buffer)
run buf4 '"CTS_CLK_BUFFERS": ["sg13g2_buf_4", "sg13g2_buf_2"]'
run buf2 '"CTS_CLK_BUFFERS": ["sg13g2_buf_2"]'

# 6. characterisation cap ceiling
run maxcap '"CTS_MAX_CAP": 0.3'

# 7. size cap plus a tight geometric diameter
run scs8d20 '"CTS_SINK_CLUSTERING_SIZE": 8,
  "CTS_SINK_CLUSTERING_MAX_DIAMETER": 20'

# 8. size cap on top of the smaller sink buffer
run buf4scs8 '"CTS_CLK_BUFFERS": ["sg13g2_buf_4", "sg13g2_buf_2"],
  "CTS_SINK_CLUSTERING_SIZE": 8'

echo
echo "=== results"
python3 scripts/fanout_sweep_report.py
