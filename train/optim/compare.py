"""What a sweep actually bought, measured against the right baseline.

`grid.py --report` ranks rows by one metric. That answers "which config scored
best", not "what did this knob change", and the difference matters: every
number in a coordinate sweep is a *delta* against the run's base, so picking
the wrong base silently rewrites every result.

That is not hypothetical. `qat.Cfg.key()` diffs a config against the dataclass
defaults rather than against `--base`, so sweeping `epochs=250` under
`--base '{"epochs": 1000}'` produces the key `{"nframe": 8}` -- exactly what a
baseline that never set `epochs` would produce. If the real baseline was also
skipped as already-done, nothing in the file is the baseline, and an analysis
that assumes "shortest key wins" compares every variant against `epochs=250`.
Doing that turned an `aug_time=3` result worth +0.93 into +2.65, and made nine
of ten variants look like improvements when most were inside the noise.

So this tool never guesses. It resolves the baseline in order:

1. `base_key` stamped on the rows by `grid.py` (authoritative),
2. an explicit `--base-key`,
3. a full-cfg match against `--base` JSON,

and if none resolves it says so and stops rather than picking a row.

    python train/optim/compare.py --tag clap_g20
    python train/optim/compare.py --tag clap_g20 --note vt-gap --sort gap

Columns are the ones this project learned to care about: the delta on test, the
delta on the val-test gap, and the same pair with the synthetic room-tone
negatives dropped (`real`), which is the honest comparison wherever those
negatives crowd the smaller split. See docs/val_test_gap.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "train", "optim"))
OUT = os.path.join(ROOT, "artifacts", "optim")


def rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def cfg_key_of(base_json: str) -> str:
    """The Cfg.key() a --base JSON string would produce."""
    from dataclasses import replace
    import qat
    return replace(qat.Cfg(tag="x"), **json.loads(base_json)).key()


def resolve_base(rs: list[dict], want):
    """The row every other row should be compared against. Never a guess."""
    if want is None:
        keys = {r["base_key"] for r in rs if r.get("base_key")}
        if len(keys) > 1:
            sys.exit(f"rows carry {len(keys)} different base_key values; pass "
                     "--note to pick one stage, or --base-key explicitly:\n  "
                     + "\n  ".join(sorted(keys)))
        if not keys:
            sys.exit(
                "no base_key on these rows (grid.py writes it only since the\n"
                "baseline-identity fix) and no --base-key/--base given.\n"
                "Refusing to guess: comparing against the wrong baseline is the\n"
                "exact failure this tool exists to prevent. Pass --base with the\n"
                "same JSON the sweep used, e.g.\n"
                "  --base '{\"nframe\": 8, \"nphase\": 2, \"H\": 4, \"accw\": 6, "
                "\"epochs\": 1000}'")
        want = keys.pop()
    hit = [r for r in rs if r["key"] == want]
    if not hit:
        sys.exit(f"baseline {want} is not in this file. It was probably skipped "
                 "as already-done under a different --note; re-run that one "
                 "config, or point --tag at the file that holds it.")
    return max(hit, key=lambda r: r.get("n", 0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--note", default="", help="only rows from this stage")
    ap.add_argument("--base-key", default=None, help="Cfg.key() of the baseline")
    ap.add_argument("--base", default=None,
                    help="the sweep's --base JSON; converted to a key")
    ap.add_argument("--sort", default="test",
                    choices=["test", "gap", "val", "test_real", "gap_real"])
    ap.add_argument("--min-seeds", type=int, default=1)
    args = ap.parse_args()

    every = [r for r in rows(os.path.join(OUT, f"{args.tag}.jsonl"))
             if r.get("n", 0) >= args.min_seeds]
    # The baseline is resolved over the WHOLE file, never over the --note
    # subset. grid.py skips any (config, seed-set) it has already run, so a
    # sweep's baseline is routinely absent from that sweep's own rows and sits
    # under whichever note first ran it. Filtering before resolving would hide
    # it and leave nothing to compare against.
    want = args.base_key or (cfg_key_of(args.base) if args.base else None)
    b = resolve_base(every, want)
    rs = [r for r in every if r.get("note") == args.note] if args.note else every
    if not rs:
        sys.exit("no rows after filtering")
    bv, bt = b["val_mean"] * 100, b["test_mean"] * 100
    bvr, btr = b.get("val_nosil_mean"), b.get("test_nosil_mean")
    honest = bvr is not None and btr is not None

    print(f"tag {args.tag}" + (f"  note {args.note}" if args.note else ""))
    print(f"baseline  {b['label'][:56]}")
    tail = (f"  |  val real {bvr*100:.2f}  test real {btr*100:.2f}  "
            f"gap real {(bvr-btr)*100:+.2f}" if honest
            else "  |  no real-audio columns on this row")
    print(f"          n={b['n']}  val {bv:.2f}  test {bt:.2f}  gap {bv-bt:+.2f}{tail}")
    if not honest:
        print("          (val_nosil/test_nosil postdate this row; re-run the "
              "baseline to get the honest pair)")
    print()

    out = []
    for r in rs:
        if r is b:
            continue
        v, t = r["val_mean"] * 100, r["test_mean"] * 100
        vr, tr = r.get("val_nosil_mean"), r.get("test_nosil_mean")
        pair = honest and vr is not None and tr is not None
        out.append(dict(
            label=r["label"], n=r["n"],
            val=v - bv, test=t - bt, gap=(v - t) - (bv - bt),
            test_real=(tr - btr) * 100 if pair else None,
            gap_real=((vr - tr) - (bvr - btr)) * 100 if pair else None,
            best_ep=r.get("best_epoch_mean")))

    # Sorting on a delta: more test AUC is better, but a *smaller* gap is, so
    # the gap columns sort ascending.
    rev = args.sort not in ("gap", "gap_real")
    out = [d for d in out if d[args.sort] is not None]
    out.sort(key=lambda d: d[args.sort], reverse=rev)

    w = max((len(d["label"]) for d in out), default=10)
    head = f"{'config':{w}}  {'n':>2}  {'dtest':>6}  {'dval':>6}  {'dgap':>6}"
    if honest:
        head += f"  {'dtest_real':>10}  {'dgap_real':>9}"
    print(head + f"  {'best_ep':>7}")
    for d in out:
        line = (f"{d['label']:{w}}  {d['n']:>2}  {d['test']:+6.2f}  "
                f"{d['val']:+6.2f}  {d['gap']:+6.2f}")
        if honest:
            line += (f"  {d['test_real']:+10.2f}" if d["test_real"] is not None
                     else f"  {'--':>10}")
            line += (f"  {d['gap_real']:+9.2f}" if d["gap_real"] is not None
                     else f"  {'--':>9}")
        line += (f"  {d['best_ep']:7.0f}" if d["best_ep"] is not None
                 else f"  {'--':>7}")
        print(line)


if __name__ == "__main__":
    main()
