import os, sys, collections
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train"))
import wwhw
from test import make_pdm, Bench, test_cfg, PDM_DIV

@cocotb.test()
async def dbg(dut):
    cocotb.start_soon(Clock(dut.clk, 20, units="ns").start())
    cfg = test_cfg(); b = Bench(dut, cfg); await b.reset()
    dut._log.info(f"RTL FRAME_LOG2={int(dut.user_project.FRAME_LOG2)} "
                  f"model frame_log2={cfg.frame_log2}")
    n_ticks = 4 << cfg.frame_log2
    bits = make_pdm(n_ticks, cfg)
    cands = {
        "aligned":  wwhw.frontend_bits(bits, cfg, n_frames=4),
        "lag1":     wwhw.frontend_bits([0] + bits, cfg, n_frames=4),
        "lag2":     wwhw.frontend_bits([0, 0] + bits, cfg, n_frames=4),
        "lead1":    wwhw.frontend_bits(bits[1:], cfg, n_frames=4),
    }
    golden = cands["aligned"]
    got=[]; prev_st=0; hist=collections.Counter()
    for tick_i in range(n_ticks):
        b.set_bit(bits[tick_i])
        for _ in range(PDM_DIV):
            await RisingEdge(dut.clk)
            st = int(dut.user_project.st.value)
            hist[st]+=1
            if st == 2 and prev_st != 2:
                got.append([int(dut.user_project.fmax[k].value) for k in range(cfg.nband)])
            prev_st = st
    dut._log.info(f"st hist {dict(hist)}  captured {len(got)} frames")
    for name, g in cands.items():
        n = min(len(got), len(g))
        eq = sum(got[i] == g[i] for i in range(n))
        dut._log.info(f"  {name:8s}: {eq}/{n} frames equal")
    for k in range(min(3, len(got))):
        dut._log.info(f"  f{k} rtl  {got[k]}")
        dut._log.info(f"  f{k} lag1 {cands['lag1'][k]}")
