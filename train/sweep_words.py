"""Which Speech Commands word is the best wake word for this silicon?

The front end is keyword-agnostic, so `extract_all.py` caches its output once
and this script re-labels those features for each of the 35 candidate words in
turn. Two stages, because the exact hardware trainer is ~100x the cost of a
float probe:

  screen  fp32 linear + fp32 MLP-4 on the same features. Says how much
          keyword-vs-rest information the silicon's 5-band map contains for
          that word -- the ceiling a hardware classifier can chase.
  exact   the shipped model: H=4 ternary hidden layer, 7-bit saturating
          accumulator clamped every frame, clamp(acc>>1, 0, 15), ternary
          output layer, 2 staggered 16-frame windows. Same semantics as
          train_sheila.py, which is what tt_um_wakeword implements.

    python train/sweep_words.py --mode screen --jobs 10
    python train/sweep_words.py --mode exact --words sheila seven ...

Writes artifacts/ww_sweep_<mode>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_sheila as TS  # noqa: E402  (QW/RQ/SatSTE/build/auc/operating_points)
import wwhw  # noqa: E402

ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "artifacts")
_G = {}


def _load(path):
    d = np.load(path, allow_pickle=True)
    cfg = wwhw.HWConfig(**json.loads(str(d["cfg"])))
    return d, cfg


def _prepare(args):
    d, cfg = _load(os.path.join(ART, f"{args.feats}.npz"))
    feats, word, splits = d["feats"], d["word"], d["splits"]
    clean = d["clean"] if "clean" in d.files else np.ones(len(word), dtype=np.int64)
    words = [str(w) for w in d["words"]]
    NF = args.nframe or cfg.nframe
    nphase = args.nphase or cfg.nphase
    hop = NF // nphase
    starts = list(range(0, feats.shape[1] - NF + 1, hop))
    centre = int(round(feats[splits == 0].mean()))
    X = feats.astype(np.float32) - centre
    Xw = torch.from_numpy(np.stack([X[:, s:s + NF] for s in starts], 1))
    _G.update(dict(Xw=Xw, word=word, splits=splits, clean=clean, words=words,
                   cfg=cfg, NF=NF, NB=cfg.nband, hop=hop, nphase=nphase,
                   centre=centre, args=args))
    return words


def _init(args):
    torch.set_num_threads(1)
    _prepare(args)


def _fit(arch, H, WL, shift, accw, epochs, warmup, Y, idx, seed, batch):
    """Train one model on the cached features; returns the per-clip max score."""
    Xw = _G["Xw"]
    torch.manual_seed(seed)
    np.random.seed(seed)
    if arch == "fp32":
        # Float MLP-H on the same features: the information ceiling.
        w1 = nn.Parameter(torch.empty(H, _G["NF"], _G["NB"]).uniform_(-0.1, 0.1))
        b1 = nn.Parameter(torch.zeros(H))
        w2 = nn.Parameter(torch.empty(1, H).uniform_(-0.5, 0.5))
        b2 = nn.Parameter(torch.zeros(1))
        params = [w1, b1, w2, b2]

        def fwd(x):
            h = F.relu(torch.einsum('nwfb,hfb->nwh', x, w1) + b1)
            return torch.einsum('nwh,kh->nwk', h, w2) + b2
        opt = torch.optim.Adam(params, lr=0.01)
        clampers = []
    elif arch == "fp32lin":
        w1 = nn.Parameter(torch.empty(1, _G["NF"], _G["NB"]).uniform_(-0.1, 0.1))
        b2 = nn.Parameter(torch.zeros(1))
        params = [w1, b2]

        def fwd(x):
            return torch.einsum('nwfb,kfb->nwk', x, w1) + b2
        opt = torch.optim.Adam(params, lr=0.02)
        clampers = []
    else:  # "hw": exactly what the chip does
        TS.BIAS_LIM = (1 << (accw - 1)) - 1 if accw else 0
        groups, hwfwd, clampers, params = TS.build(
            "mlp", H, _G["NF"], _G["NB"], WL, Xw, idx)
        lt = nn.Parameter(torch.tensor(2.0))
        groups.append({'params': [lt], 'lr': 0.01})
        opt = torch.optim.Adam(groups)

        def fwd(x):
            return hwfwd(x, shift, accw)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    nwarm = int(warmup * epochs)
    scale = torch.tensor(1.0)
    if arch == "hw":
        scale = None
    best = (-1.0, None)
    va = _G["splits"] == 1
    for ep in range(epochs):
        perm = np.random.permutation(idx)
        for i in range(0, len(perm), batch):
            j = perm[i:i + batch]
            s = fwd(Xw[j])
            pooled = (s.mean(1) if ep < nwarm
                      else s.logsumexp(1) - np.log(s.shape[1]))
            t = torch.exp(lt) if arch == "hw" else 1.0
            loss = F.binary_cross_entropy_with_logits(pooled / t, Y[j])
            opt.zero_grad()
            loss.backward()
            opt.step()
            with torch.no_grad():
                for c in clampers:
                    c.clamp_(-(WL + 0.5), WL + 0.5)
                if arch == "hw" and TS.BIAS_LIM:
                    params[1].clamp_(-TS.BIAS_LIM - 1, TS.BIAS_LIM)
        sch.step()
        if (ep + 1) % max(epochs // 6, 1) == 0 or ep == epochs - 1:
            with torch.no_grad():
                sv = fwd(Xw).max(1).values[:, 0].numpy()
            a = TS.auc(sv[va], Y.numpy()[:, 0][va] > 0.5)
            if a > best[0]:
                best = (a, [p.detach().clone() for p in params], sv)
    return best  # (val auc, params, scores)


def _one(wi):
    """Evaluate candidate word index `wi`. Runs in a worker process."""
    a = _G["args"]
    word, splits, clean = _G["word"], _G["splits"], _G["clean"]
    Y = torch.from_numpy((word == wi).astype(np.float32))[:, None]
    idx = np.where(splits == 0)[0]
    t0 = time.time()
    out = dict(word=_G["words"][wi],
               n_pos=int((word == wi).sum()),
               n_pos_test=int(((word == wi) & (splits == 2)).sum()))
    archs = (["fp32lin", "fp32"] if a.mode == "screen" else ["hw"])
    for arch in archs:
        # Several seeds, pick the one with the best *validation* AUC: ternary
        # QAT is seed-sensitive, and picking a run on val is exactly what you
        # would do when choosing the weights to hard-wire.
        runs = [_fit(arch, a.H if arch != "fp32" else a.screen_H, a.WL,
                     a.shift, a.accw, a.epochs, a.warmup, Y, idx,
                     a.seed + k, a.batch) for k in range(a.seeds)]
        vauc, _, sv = max(runs, key=lambda r: r[0])
        # Report on clean (un-jittered) test clips only.
        te = (splits == 2) & (clean == 1)
        pos = (word == wi)[te]
        ops, eph = TS.operating_points(sv[te], pos, _G["hop"], _G["cfg"].frame_ms,
                                       fas=(5, 20, 50))
        out[arch] = dict(val_auc=vauc, test_auc=TS.auc(sv[te], pos),
                         eph=eph, ops=ops,
                         seed_val_aucs=[float(r[0]) for r in runs]
                         if arch != "fp32lin" else None)
    out["secs"] = time.time() - t0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feats", default="ww_feats_allwords")
    ap.add_argument("--mode", choices=["screen", "exact"], default="screen")
    ap.add_argument("--words", nargs="*", default=None, help="default: all")
    ap.add_argument("--H", type=int, default=4, help="hidden units (hw model)")
    ap.add_argument("--screen-H", type=int, default=32, help="hidden units (fp32 probe)")
    ap.add_argument("--WL", type=int, default=1, help="1 = ternary")
    ap.add_argument("--shift", type=int, default=1)
    ap.add_argument("--accw", type=int, default=7)
    ap.add_argument("--nphase", type=int, default=2)
    ap.add_argument("--nframe", type=int, default=16)
    ap.add_argument("--epochs", type=int, default=0)
    ap.add_argument("--warmup", type=float, default=0.4)
    ap.add_argument("--batch", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--seeds", type=int, default=1, help="restarts; best val wins")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if not args.epochs:
        args.epochs = 40 if args.mode == "screen" else 120

    words = _prepare(args)
    targets = [words.index(w) for w in (args.words or words)]
    print(f"{args.mode}: {len(targets)} candidates, feats {_G['Xw'].shape}, "
          f"window {_G['NF']} frames ({_G['NF']*_G['cfg'].frame_ms:.0f} ms), "
          f"phases {_G['nphase']}, FEAT_OFF {_G['centre']}")

    t0 = time.time()
    if args.jobs > 1:
        import multiprocessing as mp
        with mp.Pool(args.jobs, initializer=_init, initargs=(args,)) as pool:
            res = []
            for r in pool.imap_unordered(_one, targets):
                res.append(r)
                key = "fp32" if args.mode == "screen" else "hw"
                print(f"  {len(res):2d}/{len(targets)}  {r['word']:9s} "
                      f"AUC {r[key]['test_auc']*100:5.1f}%  "
                      f"({r['secs']:.0f}s)", flush=True)
    else:
        _init(args)
        res = [_one(t) for t in targets]

    key = "fp32" if args.mode == "screen" else "hw"
    res.sort(key=lambda r: -r[key]["test_auc"])
    print(f"\ntotal {time.time()-t0:.0f}s\n")
    hdr = (f"{'word':10s} {'pos':>5s} {'lin':>7s} {'mlp':>7s}" if args.mode == "screen"
           else f"{'word':10s} {'pos':>5s} {'hw AUC':>7s} {'R@5FA':>7s} {'R@20FA':>7s}")
    print(hdr)
    for r in res:
        if args.mode == "screen":
            print(f"{r['word']:10s} {r['n_pos']:5d} "
                  f"{r['fp32lin']['test_auc']*100:6.1f}% {r['fp32']['test_auc']*100:6.1f}%")
        else:
            o = {int(x['fa']): x for x in r['hw']['ops']}
            print(f"{r['word']:10s} {r['n_pos']:5d} {r['hw']['test_auc']*100:6.1f}% "
                  f"{o[5]['recall']*100:6.1f}% {o[20]['recall']*100:6.1f}%")

    out = os.path.join(ART, args.out or f"ww_sweep_{args.mode}.json")
    with open(out, "w") as f:
        json.dump(dict(mode=args.mode, args=vars(args), centre=_G["centre"],
                       results=res), f, indent=1, default=float)
    print(f"\nwrote {os.path.relpath(out)}")


if __name__ == "__main__":
    main()
