"""Turn a winning configuration into a weight header, and prove it.

The grid measures; this commits. It trains one configuration over many seeds,
picks the seed by **validation** AUC, emits the header, and re-scores that
header through the independent integer chip model in ``train/eval_header.py``.
If the two AUCs disagree the export is wrong, not the model -- which is why the
check lives here rather than at the end of the day.

    python train/optim/finalise.py --tag sheila_nb6 --name sheila \
        --set nframe=8 --set epochs=1000 --seeds 16 --out src/ww_weights.svh

``--out`` defaults to a scratch path, so the tape-out headers are never
overwritten by a stray run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qat  # noqa: E402

TRAIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--name", default="sheila", help="label written into the header")
    ap.add_argument("--out", default=os.path.join(qat.ART, "optim", "candidate.svh"))
    ap.add_argument("--seeds", type=int, default=16)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                    help="Cfg override, e.g. --set nframe=8 --set epochs=1000")
    ap.add_argument("--set-fpr", type=float, default=0.0,
                    help="hand through to eval_header.py to place the threshold")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    over = {}
    for s in args.set:
        if "=" not in s:
            raise SystemExit(f"--set expects FIELD=VALUE, got {s!r}")
        k, v = s.split("=", 1)
        cur = getattr(qat.Cfg(), k)
        over[k] = type(cur)(v)
    cfg = replace(qat.Cfg(tag=args.tag), **over)

    print(f"[finalise] {args.tag}  {cfg.label()}  over {args.seeds} seeds", flush=True)
    runs = []
    for s in range(args.seeds):
        r = qat.run(replace(cfg, seed=s), args.device)
        runs.append(r)
        print(f"  seed {s:2d}  val {r['val_auc']*100:6.2f}  test {r['test_auc']*100:6.2f}",
              flush=True)

    val = np.array([r["val_auc"] for r in runs])
    test = np.array([r["test_auc"] for r in runs])
    best = runs[int(val.argmax())]
    print(f"[finalise] val {val.mean()*100:.2f} +- {val.std()*100:.2f}   "
          f"selected seed {best['seed']} on val {best['val_auc']*100:.2f}")
    print(f"[finalise] test at that seed {best['test_auc']*100:.2f}%   "
          f"(test mean over seeds {test.mean()*100:.2f}%)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    qat.emit(best, args.out, args.name)
    print(f"[finalise] wrote {os.path.relpath(args.out)}")

    row = dict(tag=args.tag, label=cfg.label(), seeds=args.seeds,
               best_seed=int(best["seed"]), val=float(best["val_auc"]),
               val_mean=float(val.mean()), val_std=float(val.std()),
               test_at_best_val=float(best["test_auc"]),
               test_mean=float(test.mean()), header=args.out,
               nframe=cfg.nframe, nphase=cfg.nphase, H=cfg.H, accw=cfg.accw,
               shift=cfg.shift, centre=best["centre"], thr=best["thr"])

    if not args.skip_verify:
        # The trainer centres features on the training-set mean, which is 6 for
        # sheila and drone but not for every corpus; the chip model has to
        # subtract the same constant or the comparison measures the offset.
        cmd = [sys.executable, os.path.join(TRAIN_DIR, "eval_header.py"),
               "--header", args.out, "--tag", args.tag,
               "--nframe", str(cfg.nframe), "--nphase", str(cfg.nphase),
               "--feat-off", str(best["centre"])]
        if args.set_fpr:
            cmd += ["--set-fpr", str(args.set_fpr)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(res.stdout or res.stderr)
        for line in res.stdout.splitlines():
            if line.startswith("test:") and "AUC" in line:
                hdr = float(line.split("AUC")[1].strip().rstrip("%")) / 100
                gap = abs(hdr - best["test_auc"])
                row["header_auc"], row["export_gap"] = hdr, gap
                ok = gap < 5e-4
                print(f"[finalise] training {best['test_auc']*100:.2f}% vs header "
                      f"{hdr*100:.2f}%  ->  {'OK' if ok else 'MISMATCH'}")
                if not ok:
                    print("[finalise] the export disagrees with the model; "
                          "do not ship this header", file=sys.stderr)

    with open(os.path.join(qat.ART, "optim", "finalists.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
