# Sheila wake-word spotter: tape-out plan

Decisions taken 2026-09-02. Each one closes a branch; none are open.

## Decisions

| # | question | decision |
|---|---|---|
| 1 | tile budget | 1×1 IHP, hard constraint |
| 2 | model acceptance | none beyond AUC. Recall at a usable FA rate stays unmeasured and FINDINGS.md must keep saying so. **Revised after the flow run:** the original weights did not fit (see 3); shipped model is H=4 ternary with a **6-bit** accumulator, retrained, AUC 89.1 % |
| 3 | area risk at 78.7 % util | harden as-is; fallback was dropping debug pins. **Outcome:** the real flow reached 105.7 % utilisation. Fixed with bit-exact RTL changes (async reset, rotating rings, narrower output sum) plus the 6-bit accumulator. Debug pins saved nothing and stay. Option chosen by the user after measurement: model change over 2×1 tiles. FINDINGS.md §7 has the numbers |
| 4 | submission location | restructure this repo into the ttihp-verilog-template layout |
| 5 | MNIST tree | full delete: root `src/ test/ train/ docs/ info.yaml area_check.sh`, `artifacts/weights_*`, `artifacts/kws_fit.json`. Lives on in git history |
| 6 | pinout | frozen as in `wakeword/info.yaml`. External hardware: one 3.3 V PDM MEMS mic, data → `ui[0]`, clock ← `uo[0]` (1.5625 MHz). Trim switches `ui[7:1]` = `1000000` for nominal |
| 7 | verification bar | CI: cocotb at `FRAME_LOG2=8` and `FRAME_LOG2=16`, plus template gate-level test. Once, locally: real `sheila` and non-target clip through RTL under verilator, compared frame-for-frame to `wwhw`, result recorded in FINDINGS.md |
| 8 | shuttle | no deadline constraint; next open IHP shuttle |

## Facts the plan rests on

- Wakeword has no dependency on the root MNIST tree (verified by grep: no imports of `train/`, `src/`, or `weights_*`).
- cocotb tests are self-contained: synthetic tone input, numpy + `wwhw.py` only, ~7 s total.
- Makefile default `FRAME_LOG2=8` differs from the hardened RTL default of 16. CI must run both.
- No `.github/` exists yet; workflows come from the template.
- `wakeword/docs/` is empty; TT precheck requires `docs/info.md`.
- Synthesised cell area 24 643 µm² is against the real sg13g2 liberty. Hardening adds CTS, hold buffers, fill: expect ~82–85 % effective. Template notes ~80 % has routed on ttihp. Failure mode is congestion, not timing.

## Ordered steps

1. Commit the untracked sweep work (`extract_all.py`, `sweep_words.py`, `ww_sweep_*.json`, `ww_feats_allwords.npz`, FINDINGS §6).
2. Delete MNIST: `git rm -r src test train docs info.yaml area_check.sh artifacts/weights_* artifacts/kws_fit.json`.
3. Move wakeword to root: `git mv wakeword/{src,test,info.yaml,area_check.sh,FINDINGS.md} .`, `git mv wakeword/train train`. Fix `area_check.sh` and `train/*` relative paths (`ART`, `SRC`, `-I wakeword/src`). Delete empty `wakeword/`.
4. Add template files from `ttihp-verilog-template`: `.github/workflows/{gds,test,docs,fpga}.yaml`, `docs/info.md` (How it works / How to test / External hardware), `test/requirements.txt` with numpy, `test/tb.v` already present.
5. Raise placement density in the template config to ~85 %. Note it in `info.yaml` comment (already partly there).
6. Add `FRAME_LOG2=16` matrix entry to `test.yaml`.
7. Rewrite root README: wakeword only. Delete MNIST prose.
8. Update FINDINGS.md: add a "Tape-out state" section stating what is and is not verified (decision 2).
9. Push. Watch GDS action. If congestion/DRC fails → decision 3 fallback, repeat.
10. Gate-level test on the hardened netlist (template `GATES=yes` path).
11. Local one-off: verilator real-clip run per decision 7. Record in FINDINGS.md.
12. Submit to the open IHP shuttle.

## Status (2026-09-02, end of day)

Steps 1–8 done. Local LibreLane harden (`runs/wokwi6`) is clean: 0 DRC, 0 LVS,
timing met, 95.9 % core utilisation. cocotb passes at both frame lengths and on
the gate-level netlist. The gate-level run caught a synthesis-semantics bug
(`-N'(x)` rectifying the mic input, present since the first version); see
FINDINGS.md §7. Remaining: push, watch the GitHub `gds` action (it should
reproduce the local run exactly; its GL test uses TT's patched iverilog), then
submit.

## Explicitly not doing

- No FA/hour measurement, no negative-audio corpus (decision 2).
- No `STATE_W=9`: measured, costs 2 AUC points alone and 7 with the 6-bit accumulator.
- No `NPHASE=1`: measured at 86 % AUC; it is the fallback if anything ever needs more area.
- No H=8; needs 2×1 tiles.
