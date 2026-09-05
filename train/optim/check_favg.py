"""Is the AVG_N front end in the RTL the same thing fe_stats.py measured?

The whole accuracy case for ``AVG_N`` rests on the software model in
``train/optim/fe_stats.py``. If the RTL computes something else, every number
in ``docs/task_optimization.md`` about the frame mean is about a design that
does not exist. ``test/test.py`` would normally answer this, but it needs
cocotb, which is not installed here and cannot be (no pip in the venv, no host
iverilog, no network in the librelane image).

The image does carry iverilog and vvp, so this drives ``test/tb_favg.v``
directly: generate a PDM bit stream, simulate, and compare the per-frame band
maxima and band means against ``fe_stats.frontend_stats`` on the same bits.

    python train/optim/check_favg.py

Exits non-zero on any mismatch.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fe_stats  # noqa: E402
import wwhw  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IMAGE = os.environ.get("LIBRELANE_IMAGE", "ghcr.io/librelane/librelane:3.0.6")

# Must match test/tb_favg.v exactly.
NBAND, TAP0, NSTAGE, STATE_W = 6, 3, 9, 9
AVG_N, AVG_SHIFT, FRAME_LOG2, NFRAMES = 3, 4, 12, 4
MANT, FEAT_W, K_SHIFT = 1, 4, 2


def main() -> int:
    cfg = wwhw.HWConfig(nstage=NSTAGE, nband=NBAND, tap0=TAP0, state_w=STATE_W,
                        mant=MANT, feat_w=FEAT_W, k_shift=K_SHIFT,
                        frame_log2=FRAME_LOG2, nphase=1, score_w=10)
    n_ticks = NFRAMES << FRAME_LOG2

    # A deterministic tone mix. No envelope, and only as long as the simulated
    # window actually reaches: n_ticks at pdm_hz is ~10 ms, so an enveloped
    # 1 s clip would present the chip with near-silence and compare zeros.
    rng = np.random.default_rng(7)
    n_audio = int(np.ceil(n_ticks * wwhw.AUDIO_HZ / cfg.pdm_hz)) + 2
    t = np.arange(n_audio) / wwhw.AUDIO_HZ
    sig = np.zeros(n_audio, dtype=np.float32)
    for f, a in [(220, .6), (700, .5), (1500, .35), (3000, .2)]:
        sig += a * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
    sig = (sig / max(abs(sig).max(), 1e-6) * 0.7).astype(np.float32)

    # The chip latches ui_in mid-period and consumes it on the *next* period,
    # so it sees its reset value first and then the stream. Both sides get the
    # same bits with that leading tick, exactly as test/test.py models it.
    pdm = np.array([y[0] for y in
                    wwhw.pdm_encode_batch(sig[None, :], n_ticks, cfg)],
                   dtype=np.float32)
    bits = [1 if v > 0 else 0 for v in pdm]
    shifted = np.concatenate([[-1.0], pdm])[:n_ticks].astype(np.float32)
    golden = fe_stats.frontend_stats(sig[None, :], cfg, NFRAMES,
                                     bits=shifted[:, None])[0]
    imax = fe_stats.STATS.index("max")
    ism = fe_stats.STATS.index(f"smean{AVG_SHIFT}")

    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "artifacts")) as td:
        for f in ("src/tt_um_wakeword.sv", "src/ww_weights.svh",
                  "src/ww_weights_drone.svh", "test/tb_favg.v"):
            shutil.copy(os.path.join(ROOT, f), td)
        with open(os.path.join(td, "bits.txt"), "w") as f:
            f.write("\n".join(str(b) for b in bits) + "\n")
        cmd = ["docker", "run", "--rm", "-v", f"{td}:/w", "-w", "/w", IMAGE,
               "bash", "-lc",
               "iverilog -g2012 -o sim tb_favg.v tt_um_wakeword.sv && vvp sim"]
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write((r.stdout or "") + (r.stderr or ""))
        return 2

    rows = []
    for line in r.stdout.splitlines():
        m = re.match(r"FRAME (\d+) MAX ((?:\d+ ?)+) AVG ((?:\d+ ?)+)$", line.strip())
        if m:
            rows.append(([int(x) for x in m.group(2).split()],
                         [int(x) for x in m.group(3).split()]))
    if not rows:
        sys.stderr.write(r.stdout[-2000:])
        return 2

    bad = 0
    for i, (rtl_max, rtl_avg) in enumerate(rows):
        if i >= len(golden):
            break
        g_max = [int(v) for v in golden[i, :, imax]]
        g_avg = [int(v) for v in golden[i, NBAND - AVG_N:, ism]]
        if rtl_max != g_max or rtl_avg != g_avg:
            bad += 1
            if bad <= 3:
                print(f"frame {i}: RTL max {rtl_max} avg {rtl_avg}")
                print(f"          sw  max {g_max} avg {g_avg}")
    print(f"{min(len(rows), len(golden))} frames compared, {bad} mismatched")
    if not bad:
        print("AVG_N front end is bit-exact against fe_stats.frontend_stats")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
