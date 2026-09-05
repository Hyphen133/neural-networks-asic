"""donateacry loader -- infant cries from many parents' own phones.

    https://github.com/gveres/donateacry-corpus   (ODbL 1.0 / DbCL 1.0)

Roughly 1 100 recordings of a crying baby, uploaded through two mobile apps by
unrelated parents in their own homes: 712 Android ``.3gp``, 416 iOS ``.caf``,
and a cleaned, de-duplicated set of 457 ``.wav``. File names carry a stable
per-install UUID, so recordings stay session-disjoint across splits.

Why this and not CryCeleb2023, which is forty times larger: every CryCeleb clip
is one Samsung A10 held 10-15 cm from the mouth in one hospital protocol.
Against FSD50K negatives a detector would only have to recognise that
microphone. This corpus has ~260 distinct devices and rooms, which is the
property that matters when positives and negatives cannot come from the same
corpus.

It supplies **no negatives** -- no cry corpus does -- so the babycry task pairs
it with FSD50K and ESC-50, which contribute cries *and* negatives of their own.

``.3gp`` (AMR) and ``.caf`` are outside libsndfile, so :func:`prepare`
transcodes everything once to 16 kHz mono WAV under ``wav16/``.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipset  # noqa: E402
from clipset import Item  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "artifacts", "data", "donateacry")
SRC = os.path.join(ROOT, "donateacry-corpus-master")
WAV16 = os.path.join(ROOT, "wav16")

# <uuid>-<epoch_ms>-<appversion>-<gender>-<age_months>-<reason>.<ext>
_NAME = re.compile(r"^(?P<uuid>[0-9a-fA-F-]{36})-(?P<ts>\d+)-")


def _sources() -> list[str]:
    pats = ["donateacry_corpus_cleaned_and_updated_data/**/*.wav",
            "donateacry-ios-upload-bucket/*.caf",
            "donateacry-android-upload-bucket/*.3gp"]
    return [p for pat in pats
            for p in sorted(glob.glob(os.path.join(SRC, pat), recursive=True))]


def _key(path: str):
    """(uuid, timestamp) -- the same recording uploaded both raw and cleaned."""
    m = _NAME.match(os.path.basename(path))
    return (m.group("uuid").lower(), m.group("ts")) if m else None


def prepare(jobs: int = 12) -> None:
    """Transcode every unique recording to 16 kHz mono WAV. Idempotent."""
    if not os.path.isdir(SRC):
        raise SystemExit(f"donateacry missing at {SRC}; run "
                         "scripts/fetch_audio_datasets.py donateacry")
    os.makedirs(WAV16, exist_ok=True)
    todo = []
    for path in _sources():                 # cleaned .wav is listed first and wins
        k = _key(path)
        if k is None:
            continue
        dest = os.path.join(WAV16, f"{k[0]}_{k[1]}.wav")
        if not os.path.exists(dest):
            todo.append((path, dest))
    if not todo:
        return
    print(f"  donateacry: transcoding {len(todo)} recordings to 16 kHz mono")

    def one(job):
        src, dest = job
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src,
                        "-ac", "1", "-ar", "16000", dest],
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(one, todo))
    made = len(glob.glob(os.path.join(WAV16, "*.wav")))
    print(f"  donateacry: {made} usable recordings under wav16/")


def index() -> list[Item]:
    """All cries, split 80/10/10 by upload UUID (no official split exists).

    Sessions differ in size -- one parent uploaded dozens of clips -- so the
    split is stratified over recordings per session rather than hashed per
    session, which would leave the val share several points off target.
    """
    prepare()
    files = [p for p in sorted(glob.glob(os.path.join(WAV16, "*.wav")))
             if os.path.getsize(p) >= 4096]   # a transcode that produced nothing
    members: dict[str, dict] = {}
    for p in files:
        g = "cry:" + os.path.basename(p).split("_")[0]
        members.setdefault(g, {1: 0})[1] += 1
    where = clipset.stratified_split(members)
    items = [Item(path=p, label=1,
                  group="cry:" + os.path.basename(p).split("_")[0],
                  split=where["cry:" + os.path.basename(p).split("_")[0]])
             for p in files]
    print(f"  donateacry: {len(items)} cry recordings, "
          f"{len(members)} upload sessions")
    return items


__all__ = ["ROOT", "WAV16", "prepare", "index"]
