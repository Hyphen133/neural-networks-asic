#!/usr/bin/env python3
"""Round 23: finalise each detector at its own winning design, and gate it.

Reads ``artifacts/optim/round22.jsonl``, picks each task's design by **mean
validation AUC** (test is recorded throughout and never used to choose), then
for each task:

  1. trains N seeds and selects the seed on validation      (finalise.py)
  2. emits the weight header
  3. re-scores that header through the independent integer chip model
     (eval_header.py) -- if the two AUCs disagree the export is wrong and the
     header must not be used, whatever it scores
  4. synthesises the REAL header at the geometry it was trained for, with
     FEAT_OFF set to the centre the trainer actually used

Step 4 is the one that decides. A candidate that does not fit the 1x1 tile is
not an improvement.

Headers land in ``artifacts/headers/``; ``src/ww_weights*.svh`` is not touched,
so nothing here changes what tapes out.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
ART = os.path.join(ROOT, "artifacts")
R22 = os.path.join(ART, "optim", "round22.jsonl")
OUT = os.path.join(ART, "optim", "round23.jsonl")

# design -> the RTL parameters that geometry implies. Everything else is the
# common shape in COMMON.
RTL = {
    "A": dict(AVG_N=0, NFRAME=8, NPHASE=2, NHID=4, HACC_W=6),
    "B": dict(AVG_N=0, NFRAME=4, NPHASE=1, NHID=8, HACC_W=5),
    "C": dict(AVG_N=3, NFRAME=2, NPHASE=1, NHID=4, HACC_W=5),
    "D": dict(AVG_N=3, NFRAME=4, NPHASE=1, NHID=4, HACC_W=5),
    "E": dict(AVG_N=2, NFRAME=2, NPHASE=1, NHID=8, HACC_W=5),
}
COMMON = dict(TAP0=3, NBAND=6, NSTAGE=9, STATE_W=9, AVG_SHIFT=6)


def best_per_task() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with open(R22) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            cur = rows.get(r["task"])
            if cur is None or r["val_mean"] > cur["val_mean"]:
                rows[r["task"]] = r
    return rows


def sh(cmd: list[str]) -> str:
    print("$ " + " ".join(os.path.relpath(c, ROOT) if c.startswith(ROOT) else c
                          for c in cmd), flush=True)
    p = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    print(p.stdout, flush=True)
    if p.returncode:
        raise RuntimeError(f"exit {p.returncode}")
    return p.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--tasks", nargs="*", default=[])
    args = ap.parse_args()

    picks = best_per_task()
    names = args.tasks or sorted(picks)
    os.makedirs(os.path.join(ART, "headers"), exist_ok=True)

    for task in names:
        r = picks[task]
        d = RTL[r["design"]]
        hdr = os.path.join(ART, "headers", f"ww_weights_{task}.svh")
        print(f"\n=== {task}: design {r['design']} "
              f"({r['stats']}, nframe={r['nframe']}, nphase={r['nphase']}, "
              f"H={r['H']}, accw={r['accw']})  val {r['val_mean']*100:.2f}", flush=True)
        t0 = time.time()
        out = sh([PY, "train/optim/finalise.py", "--tag", f"{task}_fin",
                  "--name", task, "--seeds", str(args.seeds), "--device", args.device,
                  "--out", hdr, "--set-fpr", "0.05",
                  "--set", f"stats={r['stats']}", "--set", f"nframe={r['nframe']}",
                  "--set", f"nphase={r['nphase']}", "--set", f"H={r['H']}",
                  "--set", f"accw={r['accw']}", "--set", "epochs=1000"])

        def grab(pat, cast=float):
            m = re.search(pat, out)
            return cast(m.group(1)) if m else None

        row = dict(task=task, design=r["design"], stats=r["stats"],
                   nframe=r["nframe"], nphase=r["nphase"], H=r["H"], accw=r["accw"],
                   val=grab(r"selected seed \d+ on val ([\d.]+)"),
                   val_mean=grab(r"\[finalise\] val ([\d.]+) "),
                   val_std=grab(r"val [\d.]+ \+- ([\d.]+)"),
                   test=grab(r"test at that seed ([\d.]+)%"),
                   test_mean=grab(r"test mean over seeds ([\d.]+)%"),
                   header_auc=grab(r"vs header ([\d.]+)%"),
                   export_ok=("->  OK" in out or "-> OK" in out),
                   centre=grab(r"centre=(\d+)", int), header=os.path.relpath(hdr, ROOT))

        # The trainer centres features on the training-set mean; the RTL
        # subtracts the same constant, so the gate must synthesise with it.
        params = dict(COMMON, **d)
        if row["centre"] is not None:
            params["FEAT_OFF"] = row["centre"]
        gate = [PY, "train/optim/area_gate.py", "--label", f"final_{task}",
                "--header", hdr]
        for k, v in params.items():
            gate += ["--set", f"{k}={v}"]
        g = subprocess.run(gate, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        print(g.stdout, flush=True)
        m = re.search(r"synth\s+(\d+)\s+est flow\s+\d+\s+\(\s*([\d.]+)% core\)\s+(\w+)",
                      g.stdout)
        if m:
            row["area_um2"], row["core_pct"], row["verdict"] = \
                int(m.group(1)), float(m.group(2)), m.group(3)
        row["secs"] = round(time.time() - t0, 1)
        with open(OUT, "a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[r23] {task}: test {row['test']}  export "
              f"{'OK' if row['export_ok'] else 'MISMATCH'}  "
              f"area {row.get('area_um2')} {row.get('verdict')}", flush=True)
    print("\n=== round 23 done ===", flush=True)


if __name__ == "__main__":
    main()
