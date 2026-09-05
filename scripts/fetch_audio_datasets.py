#!/usr/bin/env python3
"""Fetch the audio corpora the new detectors train on. Resumable, unattended.

    setsid nohup .venv/bin/python scripts/fetch_audio_datasets.py fsd50k humbug \
        > artifacts/logs/fetch.log 2>&1 &

Every download is a plain HTTP range request with resume, so an interrupted run
picks up where it stopped; a file whose size already matches the record is
skipped. Zenodo serves FSD50K as a multi-part zip (``.z01``..``.z05`` plus the
final ``.zip``), which Info-ZIP reassembles with ``zip -s 0``.

Corpora:
  fsd50k  Zenodo 4060432 -- 51 197 Freesound clips, 200 AudioSet classes,
          official dev(train/val)/eval split. 24.7 GB. CC-BY / CC0 per clip.
  humbug  Zenodo 4904800 -- HumBugDB, mosquito vs background field recordings
          with an official country/location split. 4.1 GB. CC-BY-4.0.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "artifacts", "data")

ZENODO = "https://zenodo.org/api/records/{rec}/files/{name}/content"

DATASETS = {
    "fsd50k": dict(
        record="4060432",
        files=["FSD50K.dev_audio.zip", "FSD50K.dev_audio.z01", "FSD50K.dev_audio.z02",
               "FSD50K.dev_audio.z03", "FSD50K.dev_audio.z04", "FSD50K.dev_audio.z05",
               "FSD50K.eval_audio.zip", "FSD50K.eval_audio.z01",
               "FSD50K.ground_truth.zip", "FSD50K.metadata.zip", "FSD50K.doc.zip"],
        # (final .zip of the set, name of the reassembled archive)
        split_zips=[("FSD50K.dev_audio.zip", "dev_audio_single.zip"),
                    ("FSD50K.eval_audio.zip", "eval_audio_single.zip")],
        plain_zips=["FSD50K.ground_truth.zip", "FSD50K.metadata.zip", "FSD50K.doc.zip"],
        check="FSD50K.eval_audio",
    ),
    "humbug": dict(
        record="4904800",
        files=[f"humbugdb_neurips_2021_{i}.zip" for i in (1, 2, 3, 4)]
              + ["neurips_2021_zenodo_0_0_1.csv"],
        split_zips=[],
        plain_zips=[f"humbugdb_neurips_2021_{i}.zip" for i in (1, 2, 3, 4)],
        check="",
    ),
    # UrbanSound8K: 8 732 slices, 10 urban classes, 10 prearranged folds that
    # keep slices of one Freesound recording together. The siren (929) and
    # dog_bark (1000) classes dwarf FSD50K's, and its other eight classes are
    # exactly the negatives those two detectors need.
    "urbansound8k": dict(
        record="1203745",
        files=["UrbanSound8K.tar.gz"],
        split_zips=[], plain_zips=[], tarballs=["UrbanSound8K.tar.gz"],
        check="UrbanSound8K/metadata/UrbanSound8K.csv",
    ),
    # ESC-50: 2 000 five-second clips, 50 classes, 5 prearranged folds. Small,
    # but it is a *third* recording channel for several of these tasks and its
    # folds are source-file disjoint. CC BY-NC 3.0 -- research use only.
    # MUSAN: 109 h of speech (LibriVox readers + US government hearings, 12
    # languages), music and noise, already 16 kHz mono. The VAD detector needs
    # a second corpus that supplies *both* classes, or it can pass by learning
    # which corpus a clip came from.
    "musan": dict(
        url_override={"musan.tar.gz": "https://www.openslr.org/resources/17/musan.tar.gz"},
        record="", files=["musan.tar.gz"],
        split_zips=[], plain_zips=[], tarballs=["musan.tar.gz"],
        check="musan/speech",
    ),
    # donateacry: ~1 100 infant cries crowdsourced through two phone apps, 261
    # distinct upload sessions. Small, but it is the only ungated cry corpus
    # with real device diversity -- CryCeleb is one Samsung A10 in one hospital.
    "donateacry": dict(
        url_override={"donateacry-corpus.zip":
                      "https://codeload.github.com/gveres/donateacry-corpus/zip/refs/heads/master"},
        record="", files=["donateacry-corpus.zip"],
        split_zips=[], plain_zips=["donateacry-corpus.zip"],
        check="donateacry-corpus-master",
    ),
    "esc50": dict(
        url_override={"ESC-50-master.zip":
                      "https://github.com/karolpiczak/ESC-50/archive/master.zip"},
        record="", files=["ESC-50-master.zip"],
        split_zips=[], plain_zips=["ESC-50-master.zip"],
        check="ESC-50-master/meta/esc50.csv",
    ),
}

CHUNK = 8 << 20


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def remote_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers.get("Content-Length", 0))


def fetch(url: str, dest: str, tries: int = 6) -> None:
    """Range-resumed download. Returns once the local size matches the remote."""
    total = remote_size(url)
    for attempt in range(tries):
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if total and have == total:
            log(f"  have {os.path.basename(dest)} ({have/1e9:.2f} GB)")
            return
        if have > total:
            os.remove(dest)
            have = 0
        req = urllib.request.Request(url)
        if have:
            req.add_header("Range", f"bytes={have}-")
            log(f"  resuming {os.path.basename(dest)} at {have/1e9:.2f} GB")
        try:
            with urllib.request.urlopen(req, timeout=120) as r, \
                    open(dest, "ab" if have else "wb") as f:
                t0, last = time.time(), have
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    have += len(buf)
                    if have - last >= (512 << 20):
                        rate = (have - last) / max(time.time() - t0, 1e-3) / 1e6
                        log(f"  {os.path.basename(dest)} {have/1e9:6.2f}/"
                            f"{total/1e9:.2f} GB  {rate:.0f} MB/s")
                        t0, last = time.time(), have
        except Exception as e:                       # network flake -> retry
            log(f"  {os.path.basename(dest)}: {type(e).__name__}: {e}; retry {attempt+1}")
            time.sleep(5 * (attempt + 1))
            continue
        got = os.path.getsize(dest)
        if got == total or not total:      # codeload streams without a length
            log(f"  done {os.path.basename(dest)} ({got/1e9:.2f} GB)")
            return
    raise SystemExit(f"could not download {url}")


def run(cmd: list[str], cwd: str) -> None:
    log("  $ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL)


def get(name: str, keep_zips: bool) -> None:
    spec = DATASETS[name]
    out = os.path.join(DATA, name)
    os.makedirs(out, exist_ok=True)
    log(f"=== {name} -> {os.path.relpath(out, ROOT)}")
    if spec["check"] and os.path.exists(os.path.join(out, spec["check"])):
        log("  already extracted, nothing to do")
        return

    over = spec.get("url_override", {})
    for f in spec["files"]:
        url = over.get(f) or ZENODO.format(rec=spec["record"], name=f)
        fetch(url, os.path.join(out, f))

    for final, single in spec["split_zips"]:
        if not os.path.exists(os.path.join(out, single)):
            run(["zip", "-q", "-s", "0", final, "--out", single], out)
        run(["unzip", "-q", "-o", single], out)
        if not keep_zips:
            os.remove(os.path.join(out, single))
            for f in spec["files"]:
                if f.startswith(final[:-4]):
                    os.remove(os.path.join(out, f))

    for t in spec.get("tarballs", []):
        p = os.path.join(out, t)
        if os.path.exists(p):
            run(["tar", "xf", t], out)
            if not keep_zips:
                os.remove(p)

    for z in spec["plain_zips"]:
        p = os.path.join(out, z)
        if os.path.exists(p):
            run(["unzip", "-q", "-o", z], out)
            if not keep_zips and z not in [s[0] for s in spec["split_zips"]]:
                os.remove(p)

    free = shutil.disk_usage(out).free / 1e9
    log(f"=== {name} ready, {free:.0f} GB free")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("datasets", nargs="+", choices=sorted(DATASETS) + ["all"])
    ap.add_argument("--keep-zips", action="store_true",
                    help="do not delete archives after extraction (needs ~50 GB more)")
    args = ap.parse_args()
    names = sorted(DATASETS) if "all" in args.datasets else args.datasets
    for n in names:
        get(n, args.keep_zips)
    log("all requested datasets ready")


if __name__ == "__main__":
    sys.exit(main())
