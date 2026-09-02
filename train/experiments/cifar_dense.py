"""Does the same W4A4 dense architecture do anything useful on CIFAR-10?

Measured answer (40 epochs each, see docs/design-document.html section 09):
    3072-16-10  42.05 %      1024-32-10 grey  37.92 %
    3072-32-10  46.06 %      float 3072-32-10 41.11 %  (weak baseline)
    3072-64-10  48.22 %

Run:  python train/experiments/cifar_dense.py
      (expects artifacts/data/cifar-10-batches-py, from
       https://www.cs.toronto.edu/~kriz/cifar-10-python.tar.gz)

Same arithmetic as the shipped MNIST design: uint4 activations, int4 weights,
one int20 accumulator, floor-shift requantisation. Only the input width changes
(3072 RGB values instead of 784 greyscale).

Accumulator bound: 3072 * 15 * 8 = 368_640 < 2^19-1, so ACC_W=20 still holds.
"""
import os, pickle, sys, time
import numpy as np, torch, torch.nn as nn, torch.nn.functional as F

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "train"))
import hw
from train import RoundClampSTE, TernarySTE, RequantSTE, q_round

CIFAR = os.path.join(ROOT, "artifacts/data/cifar-10-batches-py")


def load_cifar(grayscale=False):
    def rd(f):
        with open(os.path.join(CIFAR, f), "rb") as fh:
            return pickle.load(fh, encoding="bytes")
    tr = [rd(f"data_batch_{i}") for i in range(1, 6)]
    te = rd("test_batch")
    xtr = np.concatenate([b[b"data"] for b in tr]).astype(np.uint8)
    ytr = np.concatenate([np.array(b[b"labels"]) for b in tr]).astype(np.int64)
    xte = te[b"data"].astype(np.uint8)
    yte = np.array(te[b"labels"]).astype(np.int64)
    if grayscale:
        f = lambda x: (x.reshape(-1, 3, 1024).astype(np.float32)
                       * np.array([.299, .587, .114], np.float32)[None, :, None]).sum(1)
        xtr, xte = f(xtr).round().astype(np.uint8), f(xte).round().astype(np.uint8)
    return (hw.quantize_pixels(xtr), ytr), (hw.quantize_pixels(xte), yte)


class Dense(nn.Module):
    def __init__(self, n_in, hidden, shift1, n_class=10, mode="w4"):
        super().__init__()
        self.n_in, self.hidden, self.mode = n_in, hidden, mode
        self.register_buffer("shift1", torch.tensor(shift1))
        g = 4.0 if mode == "w4" else 1.2
        self.w1 = nn.Parameter(torch.empty(hidden, n_in).uniform_(-g, g))
        self.w2 = nn.Parameter(torch.empty(n_class, hidden).uniform_(-g, g))
        self.b1 = nn.Parameter(torch.zeros(hidden))
        self.b2 = nn.Parameter(torch.zeros(n_class))
        self.log_temp = nn.Parameter(torch.tensor(np.log(64.0), dtype=torch.float32))

    def qw(self, w):
        return TernarySTE.apply(w, 0.5) if self.mode == "ternary" else q_round(w, -8, 7)

    def forward(self, x):
        a1 = F.linear(x, self.qw(self.w1),
                      q_round(self.b1 * float(1 << int(self.shift1)), hw.BIAS_MIN, hw.BIAS_MAX))
        h = RequantSTE.apply(a1, int(self.shift1))
        a2 = F.linear(h, self.qw(self.w2), q_round(self.b2 * 32.0, hw.BIAS_MIN, hw.BIAS_MAX))
        return a2 / torch.exp(self.log_temp)

    @torch.no_grad()
    def ints(self):
        return dict(
            w1=self.qw(self.w1).round().long().numpy(),
            b1=q_round(self.b1 * float(1 << int(self.shift1)), hw.BIAS_MIN, hw.BIAS_MAX).round().long().numpy(),
            w2=self.qw(self.w2).round().long().numpy(),
            b2=q_round(self.b2 * 32.0, hw.BIAS_MIN, hw.BIAS_MAX).round().long().numpy(),
            shift1=int(self.shift1))

    @torch.no_grad()
    def clip(self):
        lo, hi = (-1.5, 1.5) if self.mode == "ternary" else (-8, 7)
        self.w1.clamp_(lo, hi); self.w2.clamp_(lo, hi)


def int_acc(p, x, y):
    a1 = x @ p["w1"].T + p["b1"]
    assert abs(a1).max() <= hw.ACC_MAX, f"acc overflow {abs(a1).max()}"
    h = np.clip(a1 >> p["shift1"], 0, 15); h[a1 < 0] = 0
    return float(((h @ p["w2"].T + p["b2"]).argmax(1) == y).mean())


def run(tag, xtr, ytr, xte, yte, hidden, epochs=40, mode="w4", lr=.02, seed=0):
    torch.manual_seed(seed)
    n_in = xtr.shape[1]
    Xtr = torch.from_numpy(xtr).float(); Ytr = torch.from_numpy(ytr)
    m = Dense(n_in, hidden, 5, mode=mode)
    with torch.no_grad():
        a = F.linear(Xtr[:4096], m.qw(m.w1)); pos = a[a > 0]
        s = int(np.clip(round(np.log2(max(torch.quantile(pos, .99).item(), 1.) / 12.)), 0, 15))
    m.shift1.fill_(s)
    opt = torch.optim.Adam(m.parameters(), lr=lr)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    xi_te = xte.astype(np.int64)
    best = 0.0
    for ep in range(epochs):
        perm = torch.randperm(len(Xtr))
        for i in range(0, len(Xtr), 256):
            idx = perm[i:i + 256]
            loss = F.cross_entropy(m(Xtr[idx]), Ytr[idx])
            opt.zero_grad(set_to_none=True); loss.backward(); opt.step(); m.clip()
        sch.step()
        best = max(best, int_acc(m.ints(), xi_te, yte))
    print(f"{tag:38s} shift={s}  int-test {best*100:5.2f}%")
    return best


if __name__ == "__main__":
    t0 = time.time()
    (xtr, ytr), (xte, yte) = load_cifar()
    print(f"CIFAR-10 RGB  train {xtr.shape}  test {xte.shape}  4-bit pixels\n")
    for h in (16, 32, 64):
        run(f"CIFAR-10 W4A4 dense 3072-{h}-10", xtr, ytr, xte, yte, h)
    (gtr, _), (gte, _) = load_cifar(grayscale=True)
    run("CIFAR-10 W4A4 dense 1024-32-10 (grey)", gtr, ytr, gte, yte, 32)

    # float upper bound for the same topology, to separate "quantisation hurt"
    # from "a dense net simply cannot do CIFAR"
    net = nn.Sequential(nn.Linear(3072, 32), nn.ReLU(), nn.Linear(32, 10))
    o = torch.optim.Adam(net.parameters(), 1e-3)
    X = torch.from_numpy(xtr).float() / 15.0; Y = torch.from_numpy(ytr)
    Xt = torch.from_numpy(xte).float() / 15.0
    for ep in range(40):
        p = torch.randperm(len(X))
        for i in range(0, len(X), 256):
            j = p[i:i + 256]
            l = F.cross_entropy(net(X[j]), Y[j]); o.zero_grad(); l.backward(); o.step()
    with torch.no_grad():
        fa = (net(Xt).argmax(1).numpy() == yte).mean()
    print(f"{'CIFAR-10 FLOAT dense 3072-32-10':38s}          float-test {fa*100:5.2f}%")
    print(f"\n{time.time()-t0:.0f}s")
