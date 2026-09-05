#!/usr/bin/env python3
"""Round 22: score every fitting design through the chip model, not a proxy.

The fp32 probe ranks feature sets but cannot rank ``NHID``, ``HACC_W`` or the
saturating accumulator -- it has none of them. Five designs survived the area
gate and three of them differ in exactly those parameters, so the comparison
has to be run through ``train/optim/qat.py``, which *is* the chip.

Every candidate is ``STATE_W=9 TAP0=3 NBAND=6``, trained on the same
``ww_feats_<task>_fin.npz`` extraction (6 bands at 9-bit state, per-frame max
and subsampled frame mean), and every one of them fits:

    A  max only              NFRAME=8 NPHASE=2 NHID=4 HACC_W=6  20 993  FIT
    B  max only              NFRAME=4 NPHASE=1 NHID=8 HACC_W=6  21 064  FIT
    C  max + mean of 3 bands NFRAME=2 NPHASE=1 NHID=4 HACC_W=6  21 801  TIGHT
    D  max + mean of 3 bands NFRAME=4 NPHASE=1 NHID=4 HACC_W=5  21 604  TIGHT
    E  max + mean of 2 bands NFRAME=2 NPHASE=1 NHID=8 HACC_W=5  22 020  TIGHT

Each design's HACC_W is the widest that fits *that* geometry, measured, not a
constant carried across: B and C were first written with HACC_W=5 while their
area had been gated at 6, which would have handicapped exactly the designs
under test. D cannot have 6 (22 622, FAIL) and keeps 5.

Selection is on validation only; test is recorded and never used to choose.
Results append to ``artifacts/optim/round22.jsonl`` and a re-run resumes.

    setsid nohup .venv/bin/python scripts/round22.py > artifacts/logs/round22.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "train", "optim"))
sys.path.insert(0, os.path.join(ROOT, "train"))
import qat  # noqa: E402

# name -> (stats, nframe, nphase, NHID, HACC_W, area_um2, verdict)
DESIGNS = {
    "A": ("max", 8, 2, 4, 6, 20993, "FIT"),
    "B": ("max", 4, 1, 8, 6, 21064, "FIT"),
    "C": ("max,smean6@3-5", 2, 1, 4, 6, 21801, "TIGHT"),
    "D": ("max,smean6@3-5", 4, 1, 4, 5, 21604, "TIGHT"),
    "E": ("max,smean6@4-5", 2, 1, 8, 5, 22020, "TIGHT"),
}
TASKS = ["babycry", "catmeow", "clap", "dogbark", "mosquito", "siren", "vad", "water"]
OUT = os.path.join(ROOT, "artifacts", "optim", "round22.jsonl")


def done_keys() -> set[str]:
    if not os.path.exists(OUT):
        return set()
    out = set()
    with open(OUT) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out.add(f"{r['task']}|{r['design']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--epochs", type=int, default=1000)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--tasks", nargs="*", default=TASKS)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    have = done_keys()
    for task in args.tasks:
        feats = os.path.join(qat.ART, f"ww_feats_{task}_fin.npz")
        if not os.path.exists(feats):
            print(f"[r22] {task}: no {os.path.basename(feats)} yet, skipping", flush=True)
            continue
        for name, (stats, nf, nph, H, accw, area, verdict) in DESIGNS.items():
            if f"{task}|{name}" in have:
                continue
            cfg = qat.Cfg(tag=f"{task}_fin", stats=stats, nframe=nf, nphase=nph,
                          H=H, accw=accw, epochs=args.epochs)
            t0 = time.time()
            r = qat.run_seeds(cfg, list(range(args.seeds)), args.device)
            row = dict(task=task, design=name, stats=stats, nframe=nf, nphase=nph,
                       H=H, accw=accw, area_um2=area, verdict=verdict,
                       val_mean=r["val_mean"], val_std=r["val_std"],
                       test_mean=r["test_mean"],
                       test_at_best_val=r["test_at_best_val"],
                       secs=round(time.time() - t0, 1))
            with open(OUT, "a") as f:
                f.write(json.dumps(row) + "\n")
            print(f"[r22] {task:9s} {name}  val {r['val_mean']*100:6.2f}"
                  f"+-{r['val_std']*100:4.2f}  test {r['test_mean']*100:6.2f}  "
                  f"({row['secs']:.0f}s)", flush=True)
    print("=== round 22 done ===", flush=True)


if __name__ == "__main__":
    main()
