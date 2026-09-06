"""UrbanSound8K loader -- the siren and dog-bark corpus.

    https://zenodo.org/records/1203745   (Salamon et al., ACM-MM 2014)

8 732 slices of at most 4 s from 10 urban classes, cut from Freesound field
recordings. Two reasons it is here rather than FSD50K alone:

* it holds **929 siren** and **1 000 dog_bark** slices against FSD50K's 132 and
  536 -- for those two detectors it is the larger and more varied source;
* its **10 folds are prearranged so that every slice of one source recording
  stays in one fold**, which is exactly the split discipline the drone work had
  to build by hand for DADS.

Folds 1-8 are train, 9 is validation, 10 is test. The remaining eight classes
(air conditioner, car horn, children playing, drilling, engine idling, gunshot,
jackhammer, street music) supply negatives, so positives and negatives share
the corpus and a detector cannot win by recognising the recording chain.

Licence: CC BY-NC 4.0 on the Zenodo record -- research use.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipset  # noqa: E402
from clipset import TEST, TRAIN, VAL, Item  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "artifacts", "data", "urbansound8k", "UrbanSound8K")
CLASSES = ("air_conditioner", "car_horn", "children_playing", "dog_bark",
           "drilling", "engine_idling", "gun_shot", "jackhammer", "siren",
           "street_music")
VAL_FOLD, TEST_FOLD = 9, 10


def _split(fold: int) -> int:
    return TEST if fold == TEST_FOLD else (VAL if fold == VAL_FOLD else TRAIN)


def rows() -> list[dict]:
    meta = os.path.join(ROOT, "metadata", "UrbanSound8K.csv")
    if not os.path.exists(meta):
        raise SystemExit(f"UrbanSound8K missing at {meta}; run "
                         "scripts/fetch_audio_datasets.py urbansound8k")
    out = []
    with open(meta) as f:
        for r in csv.DictReader(f):
            fold = int(r["fold"])
            out.append(dict(cls=r["class"], fold=fold, split=_split(fold),
                            group=f"us8k:{r['fsID']}",
                            path=os.path.join(ROOT, "audio", f"fold{fold}",
                                              r["slice_file_name"])))
    return out


def index(positive: set[str], ambiguous=frozenset(),
          neg_per_pos: float = 3.0) -> list[Item]:
    bad = (set(positive) | set(ambiguous)) - set(CLASSES)
    if bad:
        raise SystemExit(f"not UrbanSound8K classes: {sorted(bad)}")
    all_rows = rows()
    pos = [r for r in all_rows if r["cls"] in positive]
    pool = [r for r in all_rows
            if r["cls"] not in positive and r["cls"] not in ambiguous]

    n_pos = Counter(r["split"] for r in pos)
    want = {s: int(round(neg_per_pos * n_pos[s])) for s in (TRAIN, VAL, TEST)}
    have, neg = Counter(), []
    for r in sorted(pool, key=lambda r: clipset.group_hash(r["path"])):
        if have[r["split"]] < want[r["split"]]:
            neg.append(r)
            have[r["split"]] += 1

    def item(r, label):
        return Item(path=r["path"], label=label, split=r["split"], group=r["group"])

    print(f"  urbansound8k: {len(pos)} positive / {len(neg)} negative slices "
          f"(negative pool {len(pool)})")
    return [item(r, 1) for r in pos] + [item(r, 0) for r in neg]


__all__ = ["ROOT", "CLASSES", "rows", "index"]
