#!/usr/bin/env python3
"""Tabulate the CTS fanout sweep (scripts/fanout_sweep.sh) against drone_2.

Reads runs/fo_*/final/metrics.json plus the CTS-0016 fanout histogram out of
each run's CTS log, and prints one markdown table. Read-only.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

SHIPPED = "runs/drone_g20h"  # the shipped drone_2
CTS_LOG = "35-openroad-cts/openroad-cts.log"
FANOUT_LIMIT = 8  # sg13g2 liberty default_max_fanout


def leaf_hist(run: str) -> str:
    """The CTS-0016 fanout distribution, and how much of it breaks the limit."""
    path = os.path.join(run, CTS_LOG)
    if not os.path.exists(path):
        return "-"
    with open(path) as fh:
        line = next((l for l in fh if "CTS-0016" in l), None)
    if line is None:
        return "-"
    hist = re.search(r"clock = (.*?)\.\.?\s*$", line.strip())
    if hist is None:
        return "-"
    pairs = [p.strip() for p in hist.group(1).split(",") if ":" in p]
    over = sum(int(n) for fo, n in (p.split(":") for p in pairs)
               if int(fo) > FANOUT_LIMIT)
    return f"{hist.group(1)} (>{FANOUT_LIMIT}: {over} nets)"


def cts_max_fanout(run: str) -> int | None:
    """Worst leaf fanout CTS produced, readable even when the flow later died."""
    hist = leaf_hist(run)
    if hist == "-":
        return None
    return max(int(p.split(":")[0]) for p in hist.split(" (")[0].split(",")
               if ":" in p)


def failed_row(run: str) -> dict:
    """A run with no final/metrics.json: either dead, or still in flight.

    LibreLane only writes error.log on a real failure, so an empty or missing
    error.log means the flow has not got there yet -- do not call that a
    failure, or an in-flight run reads as a dead one.
    """
    fo = cts_max_fanout(run)
    err = os.path.join(run, "error.log")
    died = os.path.exists(err) and os.path.getsize(err) > 0
    if not died:
        state = "running"
    else:
        state = "DPL FAIL" if fo is None else f"DPL FAIL (cts max fo {fo})"
    return {
        "config": os.path.basename(run).removeprefix("fo_"),
        "fanout_vio": state,
        "clkbuf": None, "stdcell": None, "hold_buf": None, "util_pct": None,
        "setup_ws": None, "hold_ws": None, "slew_cap_vio": None,
        "drc_lvs": None, "power_mw": None, "leaf_fanouts": leaf_hist(run),
    }


def row(run: str) -> dict | None:
    path = os.path.join(run, "final/metrics.json")
    if not os.path.exists(path):
        return failed_row(run) if os.path.isdir(run) else None
    with open(path) as fh:
        m = json.load(fh)
    return {
        "config": os.path.basename(run).removeprefix("fo_"),
        "fanout_vio": m.get("design__max_fanout_violation__count"),
        "clkbuf": m.get("design__instance__count__class:clock_buffer"),
        "stdcell": m.get("design__instance__count__stdcell"),
        "hold_buf": m.get("design__instance__count__hold_buffer"),
        "util_pct": 100.0 * m.get("design__instance__utilization", 0.0),
        "setup_ws": m.get("timing__setup__ws"),
        "hold_ws": m.get("timing__hold__ws"),
        "slew_cap_vio": (m.get("design__max_slew_violation__count", 0)
                         + m.get("design__max_cap_violation__count", 0)),
        "drc_lvs": (m.get("design__violations", 0)
                    + m.get("design__lvs_error__count", 0)),
        "power_mw": 1000.0 * m.get("power__total", 0.0),
        "leaf_fanouts": leaf_hist(run),
    }


def fmt(v, spec="") -> str:
    if v is None:
        return "?"
    return format(v, spec) if spec else str(v)


def main() -> int:
    runs = [SHIPPED] + sorted(glob.glob("runs/fo_*"))
    rows = [r for r in (row(p) for p in runs) if r is not None]
    if not rows:
        print("no completed runs yet", file=sys.stderr)
        return 1
    rows[0]["config"] = "drone_g20h (shipped)"

    cols = [
        ("config", "config", ""),
        ("fanout_vio", "fanout vio", ""),
        ("clkbuf", "clkbuf", ""),
        ("stdcell", "stdcell", ""),
        ("hold_buf", "hold buf", ""),
        ("util_pct", "util %", ".2f"),
        ("setup_ws", "setup ws", ".2f"),
        ("hold_ws", "hold ws", ".3f"),
        ("slew_cap_vio", "slew+cap", ""),
        ("drc_lvs", "drc+lvs", ""),
        ("power_mw", "power mW", ".2f"),
    ]
    print("| " + " | ".join(h for _, h, _ in cols) + " |")
    print("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        print("| " + " | ".join(fmt(r[k], s) for k, _, s in cols) + " |")

    print("\nLeaf-buffer fanout distribution per config:")
    for r in rows:
        print(f"  {r['config']:22s} {r['leaf_fanouts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
