"""MUSAN loader -- the second corpus for the speech / non-speech detector.

    https://openslr.org/17   (Snyder, Chen, Povey 2015)

109 hours already at 16 kHz mono, in three partitions:

  speech  426 files -- LibriVox readers and US government hearings, 12 languages
  music   660 files -- several genres and production eras
  noise   930 files -- technical and ambient noise, largely from Freesound

It is here for one reason. A VAD trained with speech from corpus A and
everything else from corpus B can reach a high AUC by recognising corpus A's
recording chain. MUSAN and FSD50K each contribute **both** classes, so the
corpus carries no information about the label and that shortcut is closed.

Files are long (minutes), so extraction takes several windows from each; splits
are 80/10/10 by file, and each MUSAN file is a distinct source recording.
"""

from __future__ import annotations

import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipset  # noqa: E402
from clipset import Item  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "artifacts", "data", "musan", "musan")
PARTS = ("speech", "music", "noise")


def files(part: str) -> list[str]:
    return sorted(glob.glob(os.path.join(ROOT, part, "**", "*.wav"), recursive=True))


def index(positive_parts=("speech",), negative_parts=("music", "noise"),
          limit_per_part: int = 0) -> list[Item]:
    if not os.path.isdir(ROOT):
        raise SystemExit(f"MUSAN missing at {ROOT}; run "
                         "scripts/fetch_audio_datasets.py musan")
    chosen, kept = [], Counter()
    for part in PARTS:
        if part not in positive_parts and part not in negative_parts:
            continue
        label = int(part in positive_parts)
        fs = files(part)
        if limit_per_part:
            fs = sorted(fs, key=clipset.group_hash)[:limit_per_part]
        for p in fs:
            chosen.append((p, label, "musan:" + os.path.splitext(os.path.basename(p))[0]))
            kept[part] += 1
    # One file is one recording, so the group *is* the file; stratifying keeps
    # the speech share of val and test equal to the share of the corpus.
    members = {g: {label: 1} for _, label, g in chosen}
    where = clipset.stratified_split(members)
    print("  musan: " + "  ".join(f"{k} {v}" for k, v in kept.items()))
    return [Item(path=p, label=lab, group=g, split=where[g]) for p, lab, g in chosen]


__all__ = ["ROOT", "PARTS", "files", "index"]
