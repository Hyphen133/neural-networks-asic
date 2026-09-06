"""ESC-50 loader -- a third recording channel, small but cleanly split.

    https://github.com/karolpiczak/ESC-50   (Piczak, ACM-MM 2015)

2 000 five-second clips, 50 balanced classes of 40 clips, 44.1 kHz mono, cut
from Freesound recordings. It is far too small to train on alone, and that is
not what it is for: the 5 prearranged folds keep every clip from one source
recording in one fold, and its classes cover most of the detectors here (dog,
cat, crying_baby, clapping, siren, water_drops, pouring_water, insects).

Adding it alongside FSD50K and UrbanSound8K widens the *channel* distribution
on both sides of the label, which is the thing that stops a detector from
learning which corpus a clip came from.

Folds 1-3 are train, 4 is validation, 5 is test.

Licence: CC BY-NC 3.0 (the ESC-10 subset is CC BY) -- research use.
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
                    "..", "artifacts", "data", "esc50", "ESC-50-master")
VAL_FOLD, TEST_FOLD = 4, 5


def _split(fold: int) -> int:
    return TEST if fold == TEST_FOLD else (VAL if fold == VAL_FOLD else TRAIN)


def rows() -> list[dict]:
    meta = os.path.join(ROOT, "meta", "esc50.csv")
    if not os.path.exists(meta):
        raise SystemExit(f"ESC-50 missing at {meta}; run "
                         "scripts/fetch_audio_datasets.py esc50")
    out = []
    with open(meta) as f:
        for r in csv.DictReader(f):
            fold = int(r["fold"])
            out.append(dict(cls=r["category"], fold=fold, split=_split(fold),
                            group=f"esc:{r['src_file']}",
                            path=os.path.join(ROOT, "audio", r["filename"])))
    return out


def categories() -> set[str]:
    return {r["cls"] for r in rows()}


def index(positive, ambiguous=frozenset(), neg_per_pos: float = 3.0) -> list[Item]:
    bad = (set(positive) | set(ambiguous)) - categories()
    if bad:
        raise SystemExit(f"not ESC-50 categories: {sorted(bad)}")
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

    print(f"  esc50: {len(pos)} positive / {len(neg)} negative clips "
          f"(negative pool {len(pool)})")
    return [item(r, 1) for r in pos] + [item(r, 0) for r in neg]


__all__ = ["ROOT", "rows", "categories", "index"]
