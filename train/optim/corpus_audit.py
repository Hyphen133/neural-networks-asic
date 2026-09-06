"""Is a detector hearing the sound, or recognising the corpus?

``train/tasks.py`` states the rule in its own docstring: every corpus that
supplies positives should also supply negatives, or "a detector could score
well by recognising the corpus instead of the sound". ``babycry`` cannot obey
it -- 85 % of its positives are ``donateacry``, which is 100 % cry and ships no
negatives, while essentially all of its negatives are FSD50K and ESC-50.

So its headline AUC mixes two questions. This separates them by rebuilding the
group hashes ``clipset.group_hash`` wrote into the npz, mapping every cached
clip back to the corpus it came from, and scoring:

* **matched**    positives and negatives from the same corpus. The honest
                 number: no corpus cue is available.
* **crossed**    positives from the cry-only corpus against everyone else's
                 negatives. Inflated by whatever corpus cue exists.
* **overall**    what the pipeline reports today.

    python train/optim/corpus_audit.py --task babycry --scores scores.npy

A large matched/crossed gap means the reported number is not measuring what it
claims, and chasing it with architecture is chasing an artefact.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "train"))
import clipset  # noqa: E402
import tasks  # noqa: E402
from train_sheila import auc  # noqa: E402

ART = os.path.join(ROOT, "artifacts")


def corpus_of(path: str) -> str:
    p = path.lower()
    for key, name in (("donateacry", "donateacry"), ("fsd50k", "fsd50k"),
                      ("esc-50", "esc50"), ("esc50", "esc50"),
                      ("urbansound", "urbansound8k"), ("musan", "musan"),
                      ("humbug", "humbug")):
        if key in p:
            return name
    return "other"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="babycry")
    ap.add_argument("--tag", default="", help="feature tag; default <task>_fin")
    ap.add_argument("--scores", default="",
                    help="npy of per-clip scores over the whole npz; without it "
                         "only the corpus composition is printed")
    ap.add_argument("--split", default="test", choices=["train", "val", "test"])
    args = ap.parse_args()

    tag = args.tag or f"{args.task}_fin"
    d = np.load(os.path.join(ART, f"ww_feats_{tag}.npz"), allow_pickle=True)
    labels, splits, index = d["labels"], d["splits"], d["index"]

    # Rebuild group -> corpus from the task definition, then hash the groups the
    # same way clipset did when it wrote `index`.
    items = tasks.TASKS[args.task].build()
    g2c: dict[int, str] = {}
    for it in items:
        g2c[clipset.group_hash(it.group)] = corpus_of(it.path)
    corp = np.array([g2c.get(int(i), "?") for i in index])

    m = splits == {"train": 0, "val": 1, "test": 2}[args.split]
    pos, neg = m & (labels > 0), m & (labels == 0)
    print(f"{args.task} / {args.split}: {m.sum()} clips, "
          f"{pos.sum()} positive, {neg.sum()} negative")
    print(f"\n{'corpus':12s} {'pos clips':>10s} {'neg clips':>10s}")
    for c in sorted(set(corp[m].tolist())):
        print(f"{c:12s} {(pos & (corp == c)).sum():10d} {(neg & (corp == c)).sum():10d}")

    if not args.scores:
        print("\nno --scores given; composition only")
        return
    sc = np.load(args.scores)
    y = labels > 0
    print(f"\n{'comparison':36s} {'n_pos':>6s} {'n_neg':>6s} {'AUC':>7s}")

    def row(name, pm, nm):
        if pm.sum() < 5 or nm.sum() < 5:
            print(f"{name:36s} {pm.sum():6d} {nm.sum():6d} {'--':>7s}")
            return
        sel = pm | nm
        print(f"{name:36s} {pm.sum():6d} {nm.sum():6d} {auc(sc[sel], y[sel])*100:7.2f}")

    row("overall (what we report)", pos, neg)
    for c in sorted(set(corp[m].tolist())):
        row(f"matched: {c} pos vs {c} neg", pos & (corp == c), neg & (corp == c))
    cry_only = corp == "donateacry"
    row("crossed: donateacry pos vs others", pos & cry_only, neg & ~cry_only)
    row("no-donateacry: others only", pos & ~cry_only, neg & ~cry_only)


if __name__ == "__main__":
    main()
