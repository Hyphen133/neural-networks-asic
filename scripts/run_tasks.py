#!/usr/bin/env python3
"""Build, probe and train every new detector, end to end and unattended.

    setsid nohup .venv/bin/python scripts/run_tasks.py \
        > artifacts/logs/run_tasks.log 2>&1 &

For each task, in order:

  1. ``train/extract_clips.py``  -- corpora to 1 s clips to front-end features
  2. ``train/ceiling_probe.py``  -- fp32 MLP-64 on the same features: what the
     fixed front end can support at all, before any hardware constraint
  3. ``train/optim/finalise.py`` -- the shipped sheila recipe (H=4 ternary
     weights, 6-bit saturating accumulator, NFRAME=8, NPHASE=2) over N seeds,
     selected on validation, emitted as a weight header and re-scored through
     the independent integer chip model in ``train/eval_header.py``

Step 3's header check is the sanity check: if the training AUC and the header
AUC disagree, the export is wrong and the header must not be used.

Results append to ``artifacts/optim/new_tasks.jsonl``; each task also keeps its
full log under ``artifacts/logs/task_<name>.log``, so an interrupted run can be
inspected without repeating anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "train"))
import tasks  # noqa: E402

PY = os.path.join(ROOT, ".venv", "bin", "python")
ART = os.path.join(ROOT, "artifacts")
LOGS = os.path.join(ART, "logs")
HEADERS = os.path.join(ART, "headers")
RESULTS = os.path.join(ART, "optim", "new_tasks.jsonl")

# The configuration that hardened as runs/sheila_nb6 and beat the shipped
# sheila by 4.5 AUC (docs/nn_optimization.md). The front-end half of it
# (TAP0=3, NBAND=6) is baked into the features by extract_clips.py.
RECIPE = ["--set", "nframe=8", "--set", "epochs=1000"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd: list[str], logfile: str) -> str:
    """Run, tee to the task's log, return stdout."""
    log("$ " + " ".join(os.path.relpath(c, ROOT) if c.startswith(ROOT) else c
                        for c in cmd))
    p = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    with open(logfile, "a") as f:
        f.write("\n$ " + " ".join(cmd) + "\n" + (p.stdout or ""))
    if p.returncode:
        tail = "\n".join((p.stdout or "").splitlines()[-15:])
        raise RuntimeError(f"exit {p.returncode}: {' '.join(cmd)}\n{tail}")
    return p.stdout or ""


def grab(pattern: str, text: str, cast=float):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else None


def one(task: str, seeds: int, jobs: int, device: str, redo: bool) -> dict:
    logfile = os.path.join(LOGS, f"task_{task}.log")
    feats = os.path.join(ART, f"ww_feats_{task}.npz")
    spec = tasks.TASKS[task]
    row: dict = dict(task=task, question=spec.question, corpora=list(spec.corpora),
                     feats=os.path.relpath(feats, ROOT))

    # 1. features
    if redo or not os.path.exists(feats):
        out = sh([PY, "train/extract_clips.py", "--task", task, "--jobs", str(jobs)]
                 + (["--redecode"] if redo else []), logfile)
        row["n_clips"] = grab(r"front end over (\d+) clips", out, int)
        row["n_pos"] = grab(r"clips, (\d+) positive", out, int)
        row["n_neg"] = grab(r"positive / (\d+) negative", out, int)
        row["group_leak"] = grab(r"groups shared between splits: (\d+)", out, int)
        row["feat_mean"] = grab(r"feature range \d+\.\.\d+\s+mean ([\d.]+)", out)
    else:
        log(f"{task}: features already at {os.path.relpath(feats, ROOT)}")

    # 2. what the fixed front end can support at all
    out = sh([PY, "train/ceiling_probe.py", "--tag", task, "--hidden", "64",
              "--nframe", "8", "--device", device], logfile)
    row["ceiling_auc"] = grab(r"CEILING fp32 MLP-64: test AUC ([\d.]+)%", out)

    # 3. the hardware-exact model, emitted and verified against the chip model
    header = os.path.join(HEADERS, f"ww_weights_{task}.svh")
    out = sh([PY, "train/optim/finalise.py", "--tag", task, "--name", task,
              "--seeds", str(seeds), "--device", device, "--out", header,
              "--set-fpr", "0.05"] + RECIPE, logfile)
    row["val_auc"] = grab(r"selected seed \d+ on val ([\d.]+)", out)
    row["val_mean"] = grab(r"\[finalise\] val ([\d.]+) ", out)
    row["val_std"] = grab(r"val [\d.]+ \+- ([\d.]+)", out)
    row["test_auc"] = grab(r"test at that seed ([\d.]+)%", out)
    row["test_mean"] = grab(r"test mean over seeds ([\d.]+)%", out)
    row["header_auc"] = grab(r"vs header ([\d.]+)%", out)
    row["export_ok"] = "->  OK" in out or "-> OK" in out
    row["header"] = os.path.relpath(header, ROOT)
    row["centre"] = grab(r"centre=(\d+)", out, int)

    with open(RESULTS, "a") as f:
        f.write(json.dumps(row) + "\n")
    log(f"{task}: ceiling {row['ceiling_auc']}  hardware {row['test_auc']}  "
        f"export {'OK' if row['export_ok'] else 'MISMATCH'}")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", choices=sorted(tasks.TASKS),
                    help="default: every task")
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--redo", action="store_true", help="re-decode and re-extract")
    args = ap.parse_args()

    for d in (LOGS, HEADERS, os.path.dirname(RESULTS)):
        os.makedirs(d, exist_ok=True)
    names = args.tasks or sorted(tasks.TASKS)

    done, failed = [], []
    for t in names:
        try:
            done.append(one(t, args.seeds, args.jobs, args.device, args.redo))
        except Exception as e:              # one bad corpus must not stop the rest
            log(f"{t}: FAILED -- {e}")
            failed.append((t, str(e).splitlines()[0]))

    print("\n" + "=" * 74)
    print(f"{'task':10s} {'clips':>8s} {'pos':>8s} {'ceiling':>8s} {'chip':>8s} "
          f"{'val':>8s} {'export':>9s}")
    for r in done:
        print(f"{r['task']:10s} {r.get('n_clips') or 0:8d} {r.get('n_pos') or 0:8d} "
              f"{r.get('ceiling_auc') or 0:8.2f} {r.get('test_auc') or 0:8.2f} "
              f"{r.get('val_auc') or 0:8.2f} "
              f"{'OK' if r.get('export_ok') else 'MISMATCH':>9s}")
    for t, e in failed:
        print(f"{t:10s} FAILED: {e}")
    print("=" * 74)


if __name__ == "__main__":
    main()
