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
    rows = [r for r in rows if r.get(sort) is not None]
    rows.sort(key=lambda r: -r[sort])
    w = max(len(r["label"]) for r in rows[:top])
    # val_nosil / test_nosil drop the synthetic room-tone negatives, which are a
    # fixed 2000 clips hash-split ~10/10/80 and so take a far larger share of
    # the smaller validation split. They are the honest pair; `gap` between them
    # is the honest gap. docs/val_test_gap.md.
    honest = any(r.get("val_nosil_mean") is not None for r in rows[:top])
    # nz is the surviving fraction of the template: the area a row asks for.
    dense = any(r.get("nz_mean") is not None for r in rows[:top])
    head = (f"{'config':{w}}  {'n':>2}  {'val mean':>8}  {'val sd':>6}  "
            f"{'test mean':>9}  {'gap':>6}")
    if dense:
        head += f"  {'nz':>5}"
    if honest:
        head += f"  {'val real':>8}  {'test real':>9}  {'gap real':>8}"
    print(head)
    for r in rows[:top]:
        line = (f"{r['label']:{w}}  {r['n']:>2}  {r['val_mean']*100:7.2f}%  "
                f"{r['val_std']*100:5.2f}%  {r['test_mean']*100:8.2f}%  "
                f"{(r['val_mean']-r['test_mean'])*100:+6.2f}")
        if dense:
            nzm = r.get("nz_mean")
            line += f"  {nzm*100:4.1f}%" if nzm is not None else f"  {'--':>5}"
        if honest:
            vn, tn = r.get("val_nosil_mean"), r.get("test_nosil_mean")
            line += (f"  {vn*100:7.2f}%  {tn*100:8.2f}%  {(vn-tn)*100:+8.2f}"
                     if vn is not None and tn is not None
                     else f"  {'--':>8}  {'--':>9}  {'--':>8}")
        print(line)


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
                    choices=["val_mean", "val_max", "test_mean",
                             "test_at_best_val", "val_nosil_mean"],
                    help="val_nosil_mean ranks on validation with the synthetic "
                         "negatives dropped, which is the honest selection "
                         "signal where they crowd the split -- two thirds of "
                         "catmeow's validation negatives are room tone")
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

    # Which row is this run's baseline. Cfg.key() diffs against the *dataclass
    # defaults*, not against --base, so a swept value that happens to equal a
    # default produces the key a defaulted baseline would: sweeping epochs=250
    # under --base '{"epochs": 1000}' yields key '{"nframe": 8}', which is
    # indistinguishable from a baseline that never set epochs. If the real
    # baseline was additionally skipped as already-done, the file then holds no
    # row that is this run's baseline, and any "compare against the smallest
    # key" analysis silently compares against the wrong config -- which is how
    # an aug_time result worth +0.93 was first read as +2.65. Stamping the base
    # identity on every row makes the comparison unambiguous, and
    # train/optim/compare.py refuses to guess without it.
    base_key = base.key()
    if f"{base_key}|{seeds}" in done:
        b = done[f"{base_key}|{seeds}"]
        print(f"[grid] baseline already on file: {b['label'][:56]}  "
              f"val {b['val_mean']*100:.2f}  test {b['test_mean']*100:.2f}  "
              f"note={b.get('note','')!r} -- not re-run; compare against this",
              flush=True)
    elif not any(c.key() == base_key for c in todo):
        print(f"[grid] WARNING: base {base_key} is neither queued nor on file; "
              "no row in this run is the baseline", flush=True)
    t0 = time.time()
    for i, c in enumerate(todo, 1):
        t = time.time()
        r = qat.run_seeds(c, seeds, args.device)
        r["note"] = args.note
        r["base_key"] = base_key          # what this row should be compared to
        r["secs"] = round(time.time() - t, 1)
        with open(path, "a") as f:
            f.write(json.dumps(r) + "\n")
        print(f"[{i:3d}/{len(todo)}] {r['label'][:78]:78}  "
              f"val {r['val_mean']*100:6.2f}+-{r['val_std']*100:4.2f}  "
              f"test {r['test_mean']*100:6.2f}  nz {r.get('nz_mean', 0)*100:5.1f}%"
              f"  ({r['secs']:.0f}s)", flush=True)
    print(f"[grid] done in {(time.time()-t0)/60:.1f} min", flush=True)
    report(path, args.top, args.sort)


if __name__ == "__main__":
    main()
