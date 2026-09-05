"""HumBugDB loader -- mosquito wingbeat presence, with the official split.

    https://zenodo.org/records/4904800   (Kiskin et al., NeurIPS D&B 2021)

9 295 recordings: 18.3 h of mosquito flight tone and 14.6 h of background,
captured in the same cups, huts and field sites with the same phones and
budget mics. That last property is why this corpus and not Wingbeats or Abuzz:
those are species corpora with **no negatives at all**, so a presence detector
built on them has to borrow negatives from an unrelated corpus and can win by
recognising the microphone.

The official split, from the reference notebook, is by *site*, not by clip:

    Test A = country == Tanzania and location_type == field
    Test B = country == UK       and location_type == culture
    Train  = everything else

Test A is a deliberate domain shift and is nearly all positive (1 153 mosquito
against 10 background recordings); Test B is small but balanced. Both are kept
as the test split, and validation is carved out of train by *experiment* --
the tuple of country, place, location type and device -- so no site appears on
both sides.

``sound_type == 'audio'`` (600 files of uncurated ambient device audio) is used
by neither class: it is not labelled for mosquito presence.

Wingbeat fundamentals are 300-900 Hz, comfortably inside the 8 kHz recordings
and inside the chip's 243-486 and 486-971 Hz bands.

Licence: CC BY 4.0.
"""

from __future__ import annotations

import csv
import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipset  # noqa: E402
from clipset import TEST, TRAIN, VAL, Item  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "artifacts", "data", "humbug")
META = "neurips_2021_zenodo_0_0_1.csv"
VAL_PCT = 12                       # of the train sites, by experiment hash


def _audio_index() -> dict[str, str]:
    """id -> path. The Zenodo zips extract to a couple of different layouts."""
    out = {}
    for p in glob.glob(os.path.join(ROOT, "**", "*.wav"), recursive=True):
        out[os.path.splitext(os.path.basename(p))[0]] = p
    return out


def experiment(r: dict) -> str:
    """The source recording, qualified by where and on what it was captured.

    ``name`` is the file the clip was cut from -- 3 237 of them across 9 295
    clips, so consecutive ``id`` values routinely share one recording. Grouping
    by site alone gives only eight groups, and a validation split drawn from
    eight groups lands with essentially no background recordings in it; that is
    the same failure DADS had when consecutive slices of one recording were
    split at random, in the other direction.
    """
    return "hum:" + "|".join((r.get(k) or "?") for k in
                             ("country", "place", "location_type", "device_type",
                              "name"))


def rows() -> list[dict]:
    meta = os.path.join(ROOT, META)
    if not os.path.exists(meta):
        raise SystemExit(f"HumBugDB missing at {meta}; run "
                         "scripts/fetch_audio_datasets.py humbug")
    audio = _audio_index()
    out, missing = [], 0
    with open(meta) as f:
        for r in csv.DictReader(f):
            path = audio.get(r["id"])
            if path is None:
                missing += 1
                continue
            out.append(dict(r, path=path, group=experiment(r)))
    if missing:
        print(f"  humbug: {missing} metadata rows have no audio file")
    return out


def _official_split(r: dict):
    """Test A / Test B as the reference notebook defines them; None = train."""
    if r.get("country") == "Tanzania" and r.get("location_type") == "field":
        return TEST
    if r.get("country") == "UK" and r.get("location_type") == "culture":
        return TEST
    return None


def index(neg_per_pos: float = 0.0) -> list[Item]:
    """Every mosquito and background recording, split by site.

    ``neg_per_pos`` is ignored: the corpus is already close to balanced in
    *hours*, and background recordings are long, so the segment counts even out
    at extraction time. The argument exists so every corpus module has the same
    signature.
    """
    keep = [r for r in rows() if r.get("sound_type") in ("mosquito", "background")]
    #                                     ^ 'audio' is uncurated ambient capture

    # The test sites are the official ones and are left exactly as they are.
    # Validation is carved out of the remaining sites, stratified over
    # recordings per class: sites are wildly class-imbalanced, so hashing them
    # would hand validation a mosquito share nothing like the training one.
    pool: dict[str, dict] = {}
    for r in keep:
        if _official_split(r) is None:
            lab = int(r["sound_type"] == "mosquito")
            pool.setdefault(r["group"], {}).setdefault(lab, 0)
            pool[r["group"]][lab] += 1
    where = clipset.stratified_split(pool, val_pct=VAL_PCT, test_pct=0,
                                     splits=(TRAIN, VAL))

    items, kept = [], Counter()
    for r in keep:
        label = int(r["sound_type"] == "mosquito")
        split = _official_split(r)
        if split is None:
            split = where[r["group"]]
        items.append(Item(path=r["path"], label=label, split=split, group=r["group"]))
        kept[(split, label)] += 1
    for s, name in enumerate(("train", "val", "test")):
        print(f"  humbug {name:5s}: mosquito {kept[(s, 1)]:5d}  "
              f"background {kept[(s, 0)]:5d}")
    return items


__all__ = ["ROOT", "rows", "index", "experiment"]
