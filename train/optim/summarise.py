"""Best fitting front end per task, from everything measured so far.

Joins the two sinks the search writes -- ``artifacts/optim/probe.jsonl`` (what
each front end scores) and ``artifacts/optim/area.jsonl`` (what each geometry
costs) -- and prints, for every task, the candidates ranked by **validation**
AUC with their test AUC alongside and unused for ranking.

    python train/optim/summarise.py                 # every task, top 6
    python train/optim/summarise.py --task siren --top 15

Nothing here decides anything: it is a view over the jsonl files, so a claim
made from it can be checked against the logs that produced them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict

ART = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "artifacts")

# Baselines: the eight detectors as they stand in docs/new_tasks.md, on the
# chip. The probe is an fp32 upper reference on the same features, so these are
# not directly comparable -- they are here to say which way a row moved.
SHIPPED = dict(catmeow=79.74, siren=76.24, dogbark=75.74, babycry=72.36,
               clap=72.32, water=68.92, vad=63.72, mosquito=62.96)
TASKS = sorted(SHIPPED)


def task_of(tag: str) -> str:
    """ww_feats tag -> which of the eight it belongs to."""
    for t in TASKS:
        if tag == t or tag.startswith(t + "_"):
            return t
    return ""


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="", help="default: all eight")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--note", default="", help="only rows whose note matches this regex")
    args = ap.parse_args()

    rows = defaultdict(list)
    for r in load(os.path.join(ART, "optim", "probe.jsonl")):
        t = task_of(r["tag"])
        if not t:
            continue
        if args.note and not re.search(args.note, r.get("note", "")):
            continue
        variant = r["tag"][len(t) + 1:] or "base"
        desc = f"{variant} nf{r['nframe']}"
        if r.get("keep_stats"):
            desc += f" [{r['keep_stats']}]"
        if r.get("norm", "none") != "none":
            desc += f" norm={r['norm']}{r.get('norm_shift', '')}"
        rows[t].append((r["best_val"], r["best_test"], desc, r.get("note", "")))

    names = [args.task] if args.task else TASKS
    for t in names:
        rs = sorted(rows.get(t, []), key=lambda x: -x[0])
        print(f"\n=== {t}   (chip today {SHIPPED[t]:.2f})   "
              f"{len(rows.get(t, []))} probes")
        if not rs:
            print("  nothing measured")
            continue
        print(f"  {'val':>6} {'test':>6}  configuration")
        for v, te, desc, note in rs[:args.top]:
            print(f"  {v:6.2f} {te:6.2f}  {desc:44} {note}")


if __name__ == "__main__":
    main()
