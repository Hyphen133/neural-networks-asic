"""Resumable grid driver over train/optim/qat.py.

Two search shapes, both anchored on a *base* configuration:

* **coordinate** (default) -- vary one field at a time around the base. Cheap,
  and it is what tells you which dimension is worth spending on.
* **cross** (``--cross``) -- full product of the listed fields, for the two or
  three dimensions that turned out to interact.

Every configuration is run over several seeds and ranked by the **mean**
validation AUC. Test AUC is recorded but never used to choose anything; with
~1 300 validation clips a single run moves about a point on seed noise alone,
so a best-of-N figure mostly measures N.

    python train/optim/grid.py --tag sheila_hw --seeds 4 \
        --sweep epochs=250,500,1000 pool=lse,max,topk
    python train/optim/grid.py --tag sheila_hw --report

Results append to ``artifacts/optim/<tag>.jsonl``; re-running skips any
(config, seed-set) already in the file, so the search survives a restart.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from dataclasses import replace, fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qat  # noqa: E402

OUT = os.path.join(qat.ART, "optim")


def _coerce(name: str, text: str):
    """Parse a sweep value using the dataclass field's declared type."""
    ftype = {f.name: f.type for f in fields(qat.Cfg)}[name]
    if ftype in ("int", int):
        return int(text)
    if ftype in ("float", float):
        return float(text)
    return text


def parse_sweep(items: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    names = {f.name for f in fields(qat.Cfg)}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--sweep expects name=v1,v2,...; got {it!r}")
        name, vals = it.split("=", 1)
        if name not in names:
            raise SystemExit(f"unknown Cfg field {name!r}")
        out[name] = [_coerce(name, v) for v in vals.split(",")]
    return out


def configs(base: qat.Cfg, sweep: dict[str, list], cross: bool) -> list[qat.Cfg]:
    if not sweep:
        return [base]
    if cross:
        keys = list(sweep)
        return [replace(base, **dict(zip(keys, combo)))
                for combo in itertools.product(*(sweep[k] for k in keys))]
    seen, out = set(), []
    for name, vals in sweep.items():
        for v in vals:
            c = replace(base, **{name: v})
            if c.key() not in seen:
                seen.add(c.key())
                out.append(c)
    if base.key() not in seen:
        out.insert(0, base)
    return out


def load(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    done = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            done[f"{r['key']}|{r['seeds']}"] = r
    return done


def report(path: str, top: int, sort: str) -> None:
    rows = list(load(path).values())
    if not rows:
        print(f"no results in {path}")
        return
    rows.sort(key=lambda r: -r[sort])
    w = max(len(r["label"]) for r in rows[:top])
    print(f"{'config':{w}}  {'n':>2}  {'val mean':>8}  {'val sd':>6}  "
          f"{'test mean':>9}  {'test@best':>9}")
    for r in rows[:top]:
        print(f"{r['label']:{w}}  {r['n']:>2}  {r['val_mean']*100:7.2f}%  "
              f"{r['val_std']*100:5.2f}%  {r['test_mean']*100:8.2f}%  "
              f"{r['test_at_best_val']*100:8.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="sheila_hw")
    ap.add_argument("--base", default="{}", help="JSON overrides of Cfg defaults")
    ap.add_argument("--sweep", nargs="*", default=[], help="field=v1,v2,...")
    ap.add_argument("--cross", action="store_true", help="product, not coordinate")
    ap.add_argument("--seeds", default="4", help="count, or an explicit 0,1,2 list")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--out", default="")
    ap.add_argument("--note", default="", help="stage label stored with each row")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--sort", default="val_mean",
                    choices=["val_mean", "val_max", "test_mean", "test_at_best_val"])
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    path = args.out or os.path.join(OUT, f"{args.tag}.jsonl")
    if args.report:
        report(path, args.top, args.sort)
        return

    seeds = ([int(s) for s in args.seeds.split(",")] if "," in args.seeds
             else list(range(int(args.seeds))))
    base = replace(qat.Cfg(tag=args.tag), **json.loads(args.base))
    cfgs = configs(base, parse_sweep(args.sweep), args.cross)
    done = load(path)

    todo = [c for c in cfgs if f"{c.key()}|{seeds}" not in done]
    print(f"[grid] tag={args.tag} seeds={seeds} configs={len(cfgs)} "
          f"todo={len(todo)} out={os.path.relpath(path)}", flush=True)
    t0 = time.time()
    for i, c in enumerate(todo, 1):
        t = time.time()
        r = qat.run_seeds(c, seeds, args.device)
        r["note"] = args.note
        r["secs"] = round(time.time() - t, 1)
        with open(path, "a") as f:
            f.write(json.dumps(r) + "\n")
        print(f"[{i:3d}/{len(todo)}] {r['label'][:78]:78}  "
              f"val {r['val_mean']*100:6.2f}+-{r['val_std']*100:4.2f}  "
              f"test {r['test_mean']*100:6.2f}  ({r['secs']:.0f}s)", flush=True)
    print(f"[grid] done in {(time.time()-t0)/60:.1f} min", flush=True)
    report(path, args.top, args.sort)


if __name__ == "__main__":
    main()
