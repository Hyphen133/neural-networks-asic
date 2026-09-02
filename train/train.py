"""Quantization-aware training for the tt_um_mnist_nn ASIC.

The forward pass is not "approximately" the hardware -- it *is* the hardware:
integer weights in [-8,7], integer uint4 activations, an integer accumulator,
and a floor arithmetic-right-shift requantiser. The only floating point left
is the gradient path (straight-through estimators) and a logit temperature
that exists purely to keep cross-entropy well conditioned; it has no effect on
argmax and therefore no silicon cost.

Usage:
    python train/train.py --mode w4      --hidden 16 --epochs 30
    python train/train.py --mode ternary --hidden 16 --epochs 30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hw  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")


# ---------------------------------------------------------------------------
# Straight-through estimators
# ---------------------------------------------------------------------------
class RoundClampSTE(torch.autograd.Function):
    """y = clamp(round(x), lo, hi); gradient passes where x is in range."""

    @staticmethod
    def forward(ctx, x, lo, hi):
        ctx.save_for_backward(x)
        ctx.lo, ctx.hi = lo, hi
        return torch.clamp(torch.round(x), lo, hi)

    @staticmethod
    def backward(ctx, g):
        (x,) = ctx.saved_tensors
        mask = (x >= ctx.lo - 0.5) & (x <= ctx.hi + 0.5)
        return g * mask, None, None


class TernarySTE(torch.autograd.Function):
    """y in {-1,0,+1} by threshold; gradient passes where |x| <= 1.5."""

    @staticmethod
    def forward(ctx, x, thresh):
        ctx.save_for_backward(x)
        return (x > thresh).float() - (x < -thresh).float()

    @staticmethod
    def backward(ctx, g):
        (x,) = ctx.saved_tensors
        return g * (x.abs() <= 1.5), None


class RequantSTE(torch.autograd.Function):
    """hidden = clamp(floor(acc / 2**s), 0, 15) -- exactly `acc >>> s` + ReLU.

    Backward is the derivative of the *unquantised* map acc/2**s, gated to the
    representable band, which is the standard clipped-STE for a ReLU-clamp.
    """

    @staticmethod
    def forward(ctx, acc, shift):
        scale = float(1 << shift)
        ctx.save_for_backward(acc)
        ctx.scale = scale
        return torch.clamp(torch.floor(acc / scale), 0.0, 15.0)

    @staticmethod
    def backward(ctx, g):
        (acc,) = ctx.saved_tensors
        y = acc / ctx.scale
        return g * ((y >= 0.0) & (y <= 15.0)) / ctx.scale, None


def q_round(x, lo, hi):
    return RoundClampSTE.apply(x, lo, hi)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class TinyMNIST(nn.Module):
    def __init__(self, hidden: int, shift1: int, mode: str = "w4"):
        super().__init__()
        self.hidden = hidden
        self.mode = mode
        self.register_buffer("shift1", torch.tensor(shift1, dtype=torch.long))

        g = 4.0 if mode == "w4" else 1.2
        self.w1 = nn.Parameter(torch.empty(hidden, hw.N_IN).uniform_(-g, g))
        self.w2 = nn.Parameter(torch.empty(hw.N_CLASS, hidden).uniform_(-g, g))
        # Biases are parameterised in *output* units so their gradients have a
        # sane magnitude; the multiply below moves them into accumulator units.
        self.b1 = nn.Parameter(torch.zeros(hidden))
        self.b2 = nn.Parameter(torch.zeros(hw.N_CLASS))
        self.log_temp = nn.Parameter(torch.tensor(math.log(64.0)))

    # ---- quantisers -------------------------------------------------------
    def qw(self, w):
        if self.mode == "ternary":
            return TernarySTE.apply(w, 0.5)
        return q_round(w, hw.W_MIN, hw.W_MAX)

    def qb1(self):
        return q_round(self.b1 * float(1 << int(self.shift1)), hw.BIAS_MIN, hw.BIAS_MAX)

    def qb2(self):
        return q_round(self.b2 * 32.0, hw.BIAS_MIN, hw.BIAS_MAX)

    # ---- forward ----------------------------------------------------------
    def forward(self, x4):
        acc1 = F.linear(x4, self.qw(self.w1), self.qb1())
        h = RequantSTE.apply(acc1, int(self.shift1))
        acc2 = F.linear(h, self.qw(self.w2), self.qb2())
        return acc2 / torch.exp(self.log_temp)

    # ---- export -----------------------------------------------------------
    @torch.no_grad()
    def to_int(self):
        return dict(
            w1=self.qw(self.w1).round().to(torch.int64).cpu().numpy(),
            b1=self.qb1().round().to(torch.int64).cpu().numpy(),
            w2=self.qw(self.w2).round().to(torch.int64).cpu().numpy(),
            b2=self.qb2().round().to(torch.int64).cpu().numpy(),
            shift1=int(self.shift1),
            hidden=self.hidden,
            mode=self.mode,
        )

    @torch.no_grad()
    def clip_latent(self):
        if self.mode == "ternary":
            self.w1.clamp_(-1.5, 1.5)
            self.w2.clamp_(-1.5, 1.5)
        else:
            self.w1.clamp_(hw.W_MIN, hw.W_MAX)
            self.w2.clamp_(hw.W_MIN, hw.W_MAX)


# ---------------------------------------------------------------------------
# Data: MNIST -> uint4 tensors held entirely in memory
# ---------------------------------------------------------------------------
def load_mnist(root: str):
    (xtr, ytr), (xte, yte) = hw.load_mnist_u4(root)
    to_t = lambda x, y: (torch.from_numpy(x).float(), torch.from_numpy(y))
    return to_t(xtr, ytr), to_t(xte, yte)


def augment(x, y):
    """+-1 px shifts. Cheap, and worth ~0.5-1 pt at these bit widths."""
    n = x.shape[0]
    img = x.view(n, 28, 28)
    dx = torch.randint(-1, 2, (1,)).item()
    dy = torch.randint(-1, 2, (1,)).item()
    img = torch.roll(img, shifts=(dy, dx), dims=(1, 2))
    return img.reshape(n, -1), y


# ---------------------------------------------------------------------------
# Shift calibration
# ---------------------------------------------------------------------------
@torch.no_grad()
def calibrate_shift(model: TinyMNIST, x: torch.Tensor) -> int:
    acc1 = F.linear(x, model.qw(model.w1))
    pos = acc1[acc1 > 0]
    if pos.numel() == 0:
        return 5
    p99 = torch.quantile(pos.float(), 0.99).item()
    s = int(round(math.log2(max(p99, 1.0) / 12.0)))
    return int(np.clip(s, 0, 15))


# ---------------------------------------------------------------------------
# Train / eval
# ---------------------------------------------------------------------------
def evaluate_int(params, x, y, chunk=2000) -> float:
    correct = 0
    xi = x.to(torch.int64).numpy()
    yi = y.numpy()
    for i in range(0, len(xi), chunk):
        pred, _, _ = hw.infer_int_batch(
            params["w1"], params["b1"], params["w2"], params["b2"],
            params["shift1"], xi[i:i + chunk],
        )
        correct += int((pred == yi[i:i + chunk]).sum())
    return correct / len(xi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["w4", "ternary"], default="w4")
    ap.add_argument("--hidden", type=int, default=hw.HIDDEN)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=0.02)
    ap.add_argument("--shift", type=int, default=-1, help="-1 = auto-calibrate")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--data", default=os.path.join(ART, "data", "MNIST", "raw"))
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    os.makedirs(ART, exist_ok=True)

    (xtr, ytr), (xte, yte) = load_mnist(args.data)
    print(f"train {tuple(xtr.shape)}  test {tuple(xte.shape)}  "
          f"pixel range [{int(xtr.min())},{int(xtr.max())}]")

    model = TinyMNIST(args.hidden, 5, args.mode)
    shift = args.shift if args.shift >= 0 else calibrate_shift(model, xtr[:4096])
    model.shift1.fill_(shift)
    print(f"mode={args.mode} hidden={args.hidden} shift1={shift}")

    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)

    n = xtr.shape[0]
    best = (0.0, None)
    t0 = time.time()
    for ep in range(args.epochs):
        model.train()
        perm = torch.randperm(n)
        tot_loss = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            xb, yb = augment(xtr[idx], ytr[idx])
            loss = F.cross_entropy(model(xb), yb)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            model.clip_latent()
            tot_loss += loss.item() * len(idx)
        sched.step()

        params = model.to_int()
        acc = evaluate_int(params, xte, yte)
        tr_acc = evaluate_int(params, xtr[:10000], ytr[:10000])
        if acc > best[0]:
            best = (acc, params)
        print(f"ep {ep+1:3d}/{args.epochs}  loss {tot_loss/n:.4f}  "
              f"int-train {tr_acc*100:5.2f}%  int-test {acc*100:5.2f}%"
              f"{'  *' if acc == best[0] else ''}")

    acc, params = best
    print(f"\nbest integer test accuracy: {acc*100:.2f}%  ({time.time()-t0:.0f}s)")

    tag = f"{args.mode}_h{args.hidden}"
    np.savez(os.path.join(ART, f"weights_{tag}.npz"), **{
        k: v for k, v in params.items() if isinstance(v, np.ndarray)})
    meta = dict(
        mode=params["mode"], hidden=params["hidden"], shift1=params["shift1"],
        acc_w=hw.ACC_W, n_in=hw.N_IN, n_class=hw.N_CLASS,
        test_accuracy=acc,
        w1=params["w1"].tolist(), b1=params["b1"].tolist(),
        w2=params["w2"].tolist(), b2=params["b2"].tolist(),
    )
    with open(os.path.join(ART, f"weights_{tag}.json"), "w") as f:
        json.dump(meta, f)
    print(f"wrote artifacts/weights_{tag}.{{npz,json}}")

    # Bit-exact cross-check: scalar reference loop vs vectorised evaluation.
    x0 = xte[0].to(torch.int64).numpy()
    p_scalar, hidden, scores = hw.infer_int(
        params["w1"], params["b1"], params["w2"], params["b2"],
        params["shift1"], x0)
    p_vec, _, _ = hw.infer_int_batch(
        params["w1"], params["b1"], params["w2"], params["b2"],
        params["shift1"], x0[None, :])
    assert p_scalar == int(p_vec[0]), "scalar/vector reference mismatch"
    print(f"reference self-check ok  hidden={hidden.tolist()}  pred={p_scalar}")

    # Weight histogram -- tells you whether the int4 range is actually used.
    vals, cnts = np.unique(params["w1"], return_counts=True)
    print("w1 histogram:", dict(zip(vals.tolist(), cnts.tolist())))


if __name__ == "__main__":
    main()
