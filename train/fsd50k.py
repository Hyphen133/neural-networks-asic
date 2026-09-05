"""FSD50K loader -- the negative class for almost every detector here.

    https://zenodo.org/records/4060432   (Fonseca et al., TASLP 2022)

51 197 Freesound clips, 0.3-30 s, 44.1 kHz mono, labelled with 200 classes of
the AudioSet ontology, each clip carrying its own CC licence. Two properties
make it the right backbone for an "is X happening" chip:

* **Labels are smeared up the ontology.** A clip tagged ``Bark`` also carries
  ``Dog``, ``Domestic_animals_and_pets``, ``Animal``. So "everything that is
  definitely not a bark" is a set difference, not a guess.
* **The official split is uploader-disjoint between dev and eval**, which is
  the split that matters: eval becomes the test set and no uploader's
  recording chain appears on both sides. Inside dev the paper only
  *minimises* train/val uploader contamination, so :func:`index` prunes it --
  a val clip whose uploader also appears in train is moved to train.

The negative class is drawn from the whole corpus (music, traffic, tools,
speech, animals), which is what the chip will actually be exposed to. Classes
that are neither clearly the target nor clearly not it -- ``Applause`` for a
clap detector, ``Growling`` for a bark detector -- are declared *ambiguous*
and used as neither.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipset  # noqa: E402
from clipset import TEST, TRAIN, VAL, Item  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "artifacts", "data", "fsd50k")
GT = os.path.join(ROOT, "FSD50K.ground_truth")
META = os.path.join(ROOT, "FSD50K.metadata")


def _uploaders(part: str) -> dict[str, str]:
    path = os.path.join(META, f"{part}_clips_info_FSD50K.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {k: v.get("uploader", "?") for k, v in json.load(f).items()}


def rows() -> list[dict]:
    """Every clip: fname, path, label set, official split, uploader."""
    if not os.path.exists(GT):
        raise SystemExit(f"FSD50K ground truth missing at {GT}; run "
                         "scripts/fetch_audio_datasets.py fsd50k")
    out = []
    for part, audio, fixed in (("dev", "FSD50K.dev_audio", None),
                               ("eval", "FSD50K.eval_audio", TEST)):
        up = _uploaders(part)
        with open(os.path.join(GT, f"{part}.csv")) as f:
            for r in csv.DictReader(f):
                split = fixed if fixed is not None else (
                    VAL if r.get("split") == "val" else TRAIN)
                out.append(dict(fname=r["fname"],
                                path=os.path.join(ROOT, audio, r["fname"] + ".wav"),
                                labels=set(r["labels"].split(",")),
                                split=split,
                                uploader=up.get(r["fname"], f"unknown-{r['fname']}")))
    return out


def check_vocabulary(*label_sets) -> None:
    """Fail loudly on a typo rather than silently training on zero positives."""
    with open(os.path.join(GT, "vocabulary.csv")) as f:
        vocab = {r[1] for r in csv.reader(f)}
    bad = sorted({l for s in label_sets for l in s} - vocab)
    if bad:
        raise SystemExit(f"not FSD50K classes: {bad}")


def _disjoint_rows() -> list[dict]:
    """All rows, with dev val made strictly uploader-disjoint from dev train."""
    all_rows = rows()
    train_up = {r["uploader"] for r in all_rows if r["split"] == TRAIN}
    pruned = 0
    for r in all_rows:
        if r["split"] == VAL and r["uploader"] in train_up:
            r["split"], pruned = TRAIN, pruned + 1
    if pruned:
        print(f"  fsd50k: {pruned} val clips moved to train for uploader "
              f"disjointness")
    return all_rows


def _item(r: dict, label: int, tag: str = "fsd") -> Item:
    return Item(path=r["path"], label=label, split=r["split"],
                group=f"{tag}:{r['uploader']}")


def split_rows(positive: set[str], ambiguous=frozenset()):
    """(positives, usable negatives) -- ambiguous rows belong to neither."""
    check_vocabulary(positive, ambiguous)
    pos, neg_pool = [], []
    for r in _disjoint_rows():
        if r["labels"] & positive:
            pos.append(r)
        elif not (r["labels"] & ambiguous):
            neg_pool.append(r)
    return pos, neg_pool


def negatives(positive: set[str], ambiguous, want: dict) -> list[Item]:
    """Up to ``want[split]`` negative recordings, deterministically chosen.

    Taking negatives in hash order rather than corpus order matters: FSD50K is
    stored grouped by class, so the first N rows of the pool would be a handful
    of classes rather than a cross-section of the corpus.
    """
    _, pool = split_rows(positive, ambiguous)
    have, out = Counter(), []
    for r in sorted(pool, key=lambda r: clipset.group_hash(r["fname"])):
        if have[r["split"]] < want.get(r["split"], 0):
            out.append(_item(r, 0))
            have[r["split"]] += 1
    print(f"  fsd50k: {len(out)} negative recordings from a pool of {len(pool)} "
          f"(asked for {dict(sorted(want.items()))})")
    return out


def index(positive: set[str], ambiguous: set[str] = frozenset(),
          neg_per_pos: float = 3.0, tag: str = "fsd") -> list[Item]:
    """Positives, plus a sample of everything unambiguously not.

    ``neg_per_pos = 0`` returns positives only, which is how ``tasks.py`` uses
    it when the negatives have to be sized against *every* corpus' positives
    rather than FSD50K's own.

    The count is of *source recordings*, not the 1 s clips they are cut into;
    ``extract_clips.py`` takes more windows from each positive than from each
    negative, so the clip counts end up near balance either way.
    """
    pos, pool = split_rows(positive, ambiguous)
    out = [_item(r, 1, tag) for r in pos]
    if neg_per_pos:
        n_pos = Counter(r["split"] for r in pos)
        want = {s: int(round(neg_per_pos * n_pos[s])) for s in (TRAIN, VAL, TEST)}
        out += negatives(positive, ambiguous, want)
    print(f"  fsd50k: {len(pos)} positive recordings "
          f"(negative pool {len(pool)})")
    return out


__all__ = ["ROOT", "GT", "rows", "index", "negatives", "split_rows",
           "check_vocabulary"]
