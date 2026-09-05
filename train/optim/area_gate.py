"""Does this configuration still fit the 1x1 tile?

A candidate only counts as an improvement if it fits, so accuracy and area are
measured together rather than area being checked once at the end.

``area_check.sh`` cannot answer this on its own: ``NFRAME``, ``NBAND`` and
``NHID`` all change the required width of ``WW_ROW`` and ``WW_HBIAS`` in
``src/ww_weights.svh``, and yosys would happily read past the end of the
shipped 640-bit constant and report an area for a design that cannot exist. So
this writes a throwaway header of the correct width -- at the shipped 74 %
non-zero density, or a real emitted header if one is given -- and synthesises
against that.

    python train/optim/area_gate.py --label shipped
    python train/optim/area_gate.py --label nframe24 --set NFRAME=24
    python train/optim/area_gate.py --label nb6 --set NBAND=6 --set TAP0=3

The budget
----------
Shipped synthesises to 21 242 um^2 and the LibreLane flow turns that into
27 744 um^2 at 95.9 % core utilisation (``docs/DESIGN.md`` 7.2) -- a 1.306x
multiplier, almost all of it hold-repair buffers on flip-flop self-loops. 100 %
of the core is 28 930 um^2, so the synthesised budget is 28 930 / 1.306 =
**22 150 um^2**: about 900 um^2 over shipped, or twelve flip-flops.

The multiplier is not uniform -- it acts on flip-flop self-loops, so a change
that adds combinational area only is cheaper than this estimate says and a
change that adds flops is dearer. ``flops > shipped`` therefore downgrades a
verdict to TIGHT even when the area passes, and TIGHT means a real
``harden_local.sh`` run has to confirm it before anything ships.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ART = os.path.join(ROOT, "artifacts")

TILE_UM2 = 31318          # 202.08 x 154.98 um, the 1x1 IHP tile
SHIPPED_SYNTH = 21242     # measured: this file's --label shipped
SHIPPED_FLOPS = 205
SHIPPED_FLOW = 27744      # runs/wokwi6 after hold repair
CORE_UM2 = round(SHIPPED_FLOW / 0.959)      # 28 930: 100 % of the core
FLOW_MULT = SHIPPED_FLOW / SHIPPED_SYNTH    # 1.306
BUDGET_UM2 = round(CORE_UM2 / FLOW_MULT)    # 22 150
DENSITY = 0.74            # shipped hidden template: 237/320 non-zero

IMAGE = os.environ.get("LIBRELANE_IMAGE", "ghcr.io/librelane/librelane:3.0.6")
DEFAULTS = dict(NSTAGE=9, K_SHIFT=2, STATE_W=10, TAP0=4, NBAND=5, MANT=1,
                FEAT_W=4, FRAME_LOG2=16, NFRAME=16, NPHASE=2, NHID=4, HACC_W=6,
                HSHIFT=1, FEAT_OFF=6, SCORE_W=10, DEBUG_PINS=1,
                AVG_N=0, AVG_SHIFT=6)


def synth_header(p: dict, seed: int = 0) -> str:
    """A header of the right widths, at the shipped weight density.

    An all-zero template would let yosys delete the whole adder tree, so the
    density matters: zeros really do drop out of the tree in this design.
    """
    rng = random.Random(seed)
    H, NF, hacc = p["NHID"], p["NFRAME"], p["HACC_W"]
    # One weight per *feature*, not per band: the template reads every band's
    # frame maximum plus the frame mean of the AVG_N lowest bands, so the row
    # -- and the adder tree yosys builds from it -- is that much wider.
    NB = p["NBAND"] + p["AVG_N"]
    code = {0: 0b00, 1: 0b01, -1: 0b11}

    rows = []
    for _ in range(H * NF):
        r = 0
        for b in range(NB):
            w = rng.choice([1, -1]) if rng.random() < DENSITY else 0
            r |= code[w] << (2 * b)
        rows.append(r)
    w1w, w1v = len(rows) * 2 * NB, 0
    for i, r in enumerate(rows):
        w1v |= r << (2 * NB * i)

    lim = (1 << (hacc - 1)) - 1
    hbw, hbv = H * hacc, 0
    for h in range(H):
        hbv |= (rng.randint(-lim - 1, lim) & ((1 << hacc) - 1)) << (hacc * h)

    w2v = 0
    for h in range(H):
        w2v |= code[rng.choice([1, -1])] << (2 * h)

    sw = p["SCORE_W"]
    return (f"// synthetic, train/optim/area_gate.py -- area only, never trained\n"
            f"localparam [{w1w-1}:0] WW_ROW      = {w1w}'h{w1v:0{max(w1w//4,1)}x};\n"
            f"localparam [{hbw-1}:0] WW_HBIAS    = {hbw}'h{hbv:0{max(hbw//4,1)}x};\n"
            f"localparam [{2*H-1}:0] WW_W2       = {2*H}'h{w2v:0{max(2*H//4,1)}x};\n"
            f"localparam [{sw-1}:0] WW_THRESH_PK = {sw}'h"
            f"{13 & ((1 << sw) - 1):0{max(sw//4,1)}x};\n")


def yosys(p: dict, header: str | None, defines: str) -> tuple[int, int, float]:
    """Synthesise one configuration; returns (cells, flops, area_um2)."""
    with tempfile.TemporaryDirectory(dir=ART) as td:
        for name, seed in (("ww_weights.svh", 0), ("ww_weights_drone.svh", 1)):
            text = open(header).read() if header else synth_header(p, seed)
            with open(os.path.join(td, name), "w") as f:
                f.write(text)
        # The RTL includes "ww_weights.svh" by bare name and yosys resolves that
        # against the *including file's* directory before any -I path, so src/
        # would always win. Synthesise a copy of the RTL that sits next to the
        # generated header instead.
        rtl = os.path.join(td, "tt_um_wakeword.sv")
        with open(os.path.join(ROOT, "src", "tt_um_wakeword.sv")) as f:
            src_text = f.read()
        with open(rtl, "w") as f:
            f.write(src_text)
        rel = os.path.relpath(rtl, ROOT)
        chp = " ".join(f"-set {k} {v}" for k, v in p.items())
        script = (f"read_verilog -sv {defines} {rel}\n"
                  f"chparam {chp} tt_um_wakeword\n"
                  "synth -top tt_um_wakeword -flatten\n"
                  "dfflibmap -liberty artifacts/sg13g2.lib\n"
                  "abc -liberty artifacts/sg13g2.lib\n"
                  "opt_clean -purge\n"
                  "stat -liberty artifacts/sg13g2.lib\n")
        cmd = ["docker", "run", "--rm", "-v", f"{ROOT}:/w", "-w", "/w", IMAGE,
               "yosys", "-p", script]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = r.stdout

    # yosys 0.62 `stat` prints "<count> <area> cells" and one
    # "<count> <area>   <cellname>" line per cell type.
    cells, flops, area = 0, 0, 0.0
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+\S+\s+cells\s*$", line)
        if m:
            cells = int(m.group(1))
        m = re.match(r"\s*(\d+)\s+\S+\s+sg13g2_(?:dfrbpq?|sdfbbp)\S*\s*$", line)
        if m:
            flops += int(m.group(1))
        m = re.search(r"Chip area for module.*:\s*([\d.]+)", line)
        if m:
            area = float(m.group(1))
    if not area or not cells:
        sys.stderr.write((out or r.stderr)[-3000:])
        raise SystemExit("yosys produced no usable stat -- see output above")
    return cells, flops, area


def verdict(area: float, flops: int) -> str:
    """FIT / TIGHT / FAIL against the flow-derived synthesised budget."""
    if area > BUDGET_UM2:
        return "FAIL"
    if area > SHIPPED_SYNTH or flops > SHIPPED_FLOPS:
        return "TIGHT"
    return "FIT"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="PARAM=VALUE",
                    help="RTL parameter override, e.g. --set NFRAME=24")
    ap.add_argument("--header", default="", help="real header instead of a synthetic one")
    ap.add_argument("--defines", default="", help="e.g. -DWW_WEIGHTS_DRONE")
    ap.add_argument("--out", default=os.path.join(ART, "optim", "area.jsonl"))
    args = ap.parse_args()

    p = dict(DEFAULTS)
    for s in args.set:
        if "=" not in s:
            raise SystemExit(f"--set expects PARAM=VALUE, got {s!r}")
        k, v = s.split("=", 1)
        if k not in DEFAULTS:
            raise SystemExit(f"unknown RTL parameter {k!r}; known: {sorted(DEFAULTS)}")
        p[k] = int(v)

    cells, flops, area = yosys(p, args.header or None, args.defines)
    v = verdict(area, flops)
    changed = {k: val for k, val in p.items() if val != DEFAULTS[k]} or "shipped"
    row = dict(label=args.label, params=p, changed=changed, cells=cells, flops=flops,
               area_um2=round(area), tile_util=round(area / TILE_UM2, 4),
               est_flow_um2=round(area * FLOW_MULT),
               est_core_util=round(area * FLOW_MULT / CORE_UM2, 4),
               budget_um2=BUDGET_UM2, verdict=v)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"{args.label:22} {str(changed)[:34]:34} cells {cells:5d} flops {flops:4d} "
          f"synth {area:8.0f}  est flow {area*FLOW_MULT:8.0f} "
          f"({100*area*FLOW_MULT/CORE_UM2:5.1f}% core)  {v}")
    sys.exit(0 if v != "FAIL" else 2)


if __name__ == "__main__":
    main()
