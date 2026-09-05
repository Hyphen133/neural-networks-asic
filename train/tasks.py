"""The eight binary detectors, each defined as corpora plus label sets.

One task = one ``ww_feats_<task>.npz`` = one weight header = one chip build.
Everything task-specific lives here; ``extract_clips.py`` and the trainer stay
generic.

Two rules shape every entry, both learned on sheila and DADS:

* **Every corpus that supplies positives also supplies negatives** wherever it
  can. If speech came only from MUSAN and non-speech only from FSD50K, a
  detector could score well by recognising the corpus instead of the sound.
  The exception is donateacry, which is 100 % cry -- no infant-cry corpus ships
  matched negatives -- so the babycry task draws cries from two further corpora
  that *do* also supply its negatives.
* **Splits are disjoint at the recording level**, using each corpus' own
  official split where it has one: FSD50K dev/eval (uploader-disjoint),
  UrbanSound8K's 10 folds, ESC-50's 5 folds, HumBugDB's Tanzania-field and
  UK-culture test sites.

A third category, ``ambiguous``, is used by neither class: sounds where the
label is genuinely unclear (``Applause`` for a clap detector, ``Dog`` without
``Bark``, ``Singing`` for a speech detector). Putting them in the negatives
would teach the detector something false.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import donateacry  # noqa: E402
import esc50  # noqa: E402
import fsd50k  # noqa: E402
import humbug  # noqa: E402
import musan  # noqa: E402
import urbansound8k  # noqa: E402
from clipset import Item  # noqa: E402


@dataclass(frozen=True)
class TaskSpec:
    name: str
    question: str                       # what the chip's LED means
    build: Callable[[], list[Item]]
    corpora: tuple[str, ...] = ()
    caveat: str = ""

    def summarise(self, items: list[Item]) -> str:
        by = Counter((i.group.split(":")[0], i.label) for i in items)
        lines = [f"task {self.name}: {self.question}",
                 f"  corpora: {', '.join(self.corpora)}"]
        for corpus in sorted({k[0] for k in by}):
            lines.append(f"    {corpus:12s} positive {by[(corpus, 1)]:6d}  "
                         f"negative {by[(corpus, 0)]:6d}")
        n_pos = sum(1 for i in items if i.label)
        lines.append(f"  total {len(items)} recordings, {n_pos} positive, "
                     f"{len({i.group for i in items})} groups")
        if self.caveat:
            lines.append(f"  CAVEAT: {self.caveat}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Label sets
# ---------------------------------------------------------------------------
# FSD50K smears labels up the AudioSet ontology, so a parent class appearing
# without its child ("Dog" but not "Bark") is precisely the ambiguous case.
FSD = {
    "babycry": (
        {"Crying_and_sobbing"},
        {"Screaming", "Yell", "Shout", "Child_speech_and_kid_speaking", "Laughter",
         "Giggle", "Chuckle_and_chortle", "Human_voice", "Sigh", "Gasp",
         "Whispering", "Chatter", "Crowd", "Cheering", "Human_group_actions"}),
    "clap": (
        {"Clapping"},
        {"Applause", "Hands", "Finger_snapping", "Cheering", "Crowd",
         "Human_group_actions", "Slam", "Knock", "Thump_and_thud", "Crack"}),
    "dogbark": (
        {"Bark"},
        {"Dog", "Growling"}),
    "catmeow": (
        {"Meow"},
        {"Cat", "Purr", "Hiss", "Growling"}),
    "vad": (
        {"Speech", "Male_speech_and_man_speaking", "Female_speech_and_woman_speaking",
         "Child_speech_and_kid_speaking", "Conversation", "Chatter"},
        {"Speech_synthesizer", "Singing", "Male_singing", "Female_singing", "Crowd",
         "Cheering", "Shout", "Yell", "Screaming", "Whispering", "Laughter",
         "Giggle", "Chuckle_and_chortle", "Human_voice", "Human_group_actions"}),
    "siren": (
        {"Siren"},
        {"Alarm", "Vehicle_horn_and_car_horn_and_honking", "Ringtone", "Doorbell",
         "Bell", "Church_bell", "Chime", "Screech"}),
    "water": (
        {"Water_tap_and_faucet", "Trickle_and_dribble", "Gurgling", "Drip",
         "Sink_(filling_or_washing)"},
        {"Water", "Liquid", "Pour", "Fill_(with_liquid)",
         "Bathtub_(filling_or_washing)", "Splash_and_splatter", "Stream",
         "Waves_and_surf", "Ocean", "Rain", "Raindrop", "Toilet_flush", "Boiling",
         "Hiss", "Frying_(food)", "Tap"}),
}

ESC = {
    "babycry": ({"crying_baby"},
                {"laughing", "sneezing", "coughing", "breathing", "snoring"}),
    "clap": ({"clapping"}, {"door_wood_knock", "footsteps"}),
    "dogbark": ({"dog"}, set()),
    "catmeow": ({"cat"}, set()),
    "siren": ({"siren"}, {"car_horn", "clock_alarm", "church_bells"}),
    "water": ({"water_drops", "pouring_water"},
              {"toilet_flush", "washing_machine", "brushing_teeth",
               "drinking_sipping", "rain", "sea_waves", "thunderstorm"}),
}


NEG_PER_POS = 3.0          # negative *recordings* per positive, in every split


def _fsd(task: str, neg_per_pos: float = 0.0) -> list[Item]:
    pos, amb = FSD[task]
    return fsd50k.index(pos, amb, neg_per_pos=neg_per_pos)


def _esc(task: str, neg_per_pos: float) -> list[Item]:
    pos, amb = ESC[task]
    return esc50.index(pos, amb, neg_per_pos=neg_per_pos)


def _top_up(task: str, items: list[Item], ratio: float = NEG_PER_POS) -> list[Item]:
    """Fill the negative class from FSD50K until every split hits `ratio`.

    Each corpus sizes its own negatives against its own positives, which leaves
    the *combined* ratio drifting between splits whenever the corpora
    contribute positives in different proportions -- donateacry's cries are 80 %
    train, FSD50K's are spread over dev and eval. Topping up from the widest
    corpus last makes the positive share of train, val and test the same
    number by construction, which is what makes the three AUCs comparable.
    """
    pos, neg = Counter(), Counter()
    for i in items:
        (pos if i.label else neg)[i.split] += 1
    want = {s: max(0, int(round(ratio * pos[s])) - neg[s]) for s in (0, 1, 2)}
    p, a = FSD[task]
    return items + fsd50k.negatives(p, a, want)


# ---------------------------------------------------------------------------
# The eight tasks
# ---------------------------------------------------------------------------
def _babycry() -> list[Item]:
    # donateacry has no negatives; FSD50K and ESC-50 carry both classes, and
    # FSD50K then supplies the bulk of the negatives for all three sources.
    return _top_up("babycry",
                   donateacry.index() + _fsd("babycry") + _esc("babycry", 8.0))


def _mosquito() -> list[Item]:
    # HumBugDB only. Its background recordings come from the same cups, huts and
    # phones as its mosquito ones, which no cross-corpus negative can match;
    # mixing FSD50K in would let the model separate corpora instead of sounds.
    return humbug.index()


def _clap() -> list[Item]:
    return _top_up("clap", _fsd("clap") + _esc("clap", 3.0))


def _dogbark() -> list[Item]:
    return _top_up("dogbark",
                   _fsd("dogbark") + _esc("dogbark", 3.0)
                   + urbansound8k.index({"dog_bark"}, neg_per_pos=2.0))


def _catmeow() -> list[Item]:
    return _top_up("catmeow", _fsd("catmeow") + _esc("catmeow", 3.0))


def _vad() -> list[Item]:
    # MUSAN is 21 % speech, so it arrives with a surplus of negatives; the
    # top-up only has to cover FSD50K's own speech.
    return _top_up("vad", _fsd("vad") + musan.index())


def _siren() -> list[Item]:
    return _top_up("siren",
                   urbansound8k.index({"siren"}, {"car_horn"}, neg_per_pos=2.0)
                   + _fsd("siren") + _esc("siren", 3.0))


def _water() -> list[Item]:
    return _top_up("water", _fsd("water") + _esc("water", 3.0))


TASKS: dict[str, TaskSpec] = {
    "babycry": TaskSpec(
        "babycry", "an infant is crying nearby", _babycry,
        ("donateacry", "fsd50k", "esc50"),
        "donateacry supplies positives only; its ~260 phone recordings are the "
        "channel-diverse core, FSD50K and ESC-50 supply cries *and* negatives"),
    "mosquito": TaskSpec(
        "mosquito", "a mosquito is flying within earshot", _mosquito,
        ("humbug",),
        "the official Test A (Tanzania, field) is a domain shift and is almost "
        "all positive; Test B (UK, culture) is balanced but small"),
    "clap": TaskSpec(
        "clap", "someone clapped", _clap, ("fsd50k", "esc50"),
        "a clap is a sub-100 ms transient and the chip's window is 335 ms with "
        "a 168 ms hop, so alignment, not timbre, is the hard part"),
    "dogbark": TaskSpec("dogbark", "a dog is barking", _dogbark,
                        ("fsd50k", "esc50", "urbansound8k")),
    "catmeow": TaskSpec("catmeow", "a cat is meowing", _catmeow,
                        ("fsd50k", "esc50")),
    "vad": TaskSpec("vad", "somebody is speaking", _vad, ("fsd50k", "musan"),
                    "singing, shouting, laughter and whispering are ambiguous "
                    "and used by neither class"),
    "siren": TaskSpec("siren", "an emergency siren is sounding", _siren,
                      ("urbansound8k", "fsd50k", "esc50")),
    "water": TaskSpec(
        "water", "water is running, dripping or trickling", _water,
        ("fsd50k", "esc50"),
        "PROXY. No public corpus records real pipe leaks through a microphone "
        "in air -- the acoustic leak datasets are hydrophone and accelerometer "
        "signals at 4-8 kHz with no leak/no-leak labels in their file names. "
        "This detector answers 'is water running', the closest question the "
        "available data supports"),
}


__all__ = ["TASKS", "TaskSpec", "FSD", "ESC"]
