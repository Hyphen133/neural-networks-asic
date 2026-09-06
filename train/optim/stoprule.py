"""Which stopping rule should keep the checkpoint?

`qat.run` already early-stops in the only sense that affects accuracy: it keeps
the argmax-validation checkpoint and restores it at the end, so training longer
can never return a worse model than training less. Patience-based termination
would save compute and nothing else.

The open question is not *when to stop* but *what to stop on*, and the curves
say there is real money in it. Recording validation and test AUC at every
checkpoint of one run shows test peaking about half as early as validation:

    mosquito   argmax val ep 105   argmax test ep  45   67.87 vs 69.70 best
    clap       argmax val ep 220   argmax test ep 120   81.18 vs 84.95 best

That is ordinary overfitting -- validation still climbing while test falls --
and it costs 1.8 to 3.8 points. Selecting on the silence-free validation AUC
does not recover any of it: the two criteria correlate 0.87-0.94 across
checkpoints and pick the identical epoch, so the mixture confound never
corrupted selection, it only inflated the reported number.

This evaluates rules that use ONLY validation, against an oracle that is
allowed to see test. A rule is worth adopting only if it beats `argmax_val`
without ever consulting test:

  argmax_val   what we do now
  first_99     earliest checkpoint within 1 % of the best validation AUC,
               which stops earlier whenever the validation curve plateaus
  first_995    the same at 0.5 %
  half_budget  fixed: the checkpoint nearest half the epoch budget
  oracle_test  argmax test -- NOT usable, the ceiling to measure against

    python train/optim/stoprule.py --tags clap_g20 catmeow_g20 --seeds 3

Note the epoch budget also sets the cosine annealing horizon
(`qat.py`: CosineAnnealingLR(opt, c.epochs)), so a rule expressed as a fraction
of the budget is partly a statement about the learning rate. See
docs/val_test_gap.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "train"))
sys.path.insert(0, os.path.join(ROOT, "train", "optim"))
import qat  # noqa: E402


def curves(tag: str, seed: int, epochs: int, device: str) -> dict:
    """val / val_nosil / test / test_nosil AUC at every checkpoint of one run."""
    c = replace(qat.Cfg(tag=tag), nframe=8, nphase=2, H=4, accw=6,
                epochs=epochs, seed=seed)
    d = qat.Data(tag, device)
    vm, tm = d.va & d.real, d.te & d.real
    out = {k: [] for k in ("val", "nosil", "test", "test_real")}
    orig = qat._score_all

    def spy(fwd, data, cfg, starts, centre):
        sv = orig(fwd, data, cfg, starts, centre)
        out["val"].append(qat.auc(sv[d.va], d.is_pos[d.va]))
        out["nosil"].append(qat.auc(sv[vm], d.is_pos[vm]))
        out["test"].append(qat.auc(sv[d.te], d.is_pos[d.te]))
        out["test_real"].append(qat.auc(sv[tm], d.is_pos[tm]))
        return sv

    qat._score_all = spy
    try:
        qat.run(c, device)
    finally:
        qat._score_all = orig
    # run() scores once more after restoring the best checkpoint; that trailing
    # entry is not a training checkpoint, so it is dropped.
    return {k: np.array(v[:-1]) for k, v in out.items()}


RULES = ("argmax_val", "first_99", "first_995", "half_budget", "oracle_test")


def pick(rule: str, val: np.ndarray, test: np.ndarray) -> int:
    if rule == "argmax_val":
        return int(val.argmax())
    if rule == "oracle_test":
        return int(test.argmax())
    if rule == "half_budget":
        return len(val) // 2
    frac = 0.99 if rule == "first_99" else 0.995
    thr = val.max() * frac
    return int(np.argmax(val >= thr))          # earliest index clearing it


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", required=True)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=1000)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    print(f"epochs={args.epochs}  seeds={args.seeds}  "
          f"(test AUC at the checkpoint each rule keeps)\n", flush=True)
    head = f"{'task':14s}" + "".join(f"{r:>13s}" for r in RULES)
    print(head)
    print("-" * len(head), flush=True)
    totals = {r: [] for r in RULES}
    for tag in args.tags:
        per = {r: [] for r in RULES}
        eps = {r: [] for r in RULES}
        for s in range(args.seeds):
            c = curves(tag, s, args.epochs, args.device)
            for r in RULES:
                i = pick(r, c["val"], c["test"])
                per[r].append(c["test"][i] * 100)
                eps[r].append((i + 1) * 5)
        print(f"{tag:14s}" + "".join(f"{np.mean(per[r]):13.2f}" for r in RULES))
        print(f"{'  ep':14s}" + "".join(f"{np.mean(eps[r]):13.0f}" for r in RULES),
              flush=True)
        for r in RULES:
            totals[r].append(np.mean(per[r]) - np.mean(per["argmax_val"]))
    print("-" * len(head))
    print(f"{'mean vs now':14s}" + "".join(f"{np.mean(totals[r]):+13.2f}"
                                           for r in RULES))


if __name__ == "__main__":
    main()
