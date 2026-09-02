# Sheila wake-word spotter: tape-out plan

Decisions taken 2026-09-02. Each one closes a branch; none are open.

## Decisions

| # | question | decision |
|---|---|---|
| 1 | tile budget | 1×1 IHP, hard constraint |
| 2 | model acceptance | none beyond AUC; ship current `ww_weights.svh` (H=4 ternary, 7-bit acc, AUC 89.3 %). Recall at a usable FA rate stays unmeasured and FINDINGS.md must keep saying so |
| 3 | area risk at 78.7 % util | harden as-is; if routing or DRC fails, drop the 16 debug outputs (`uo[4..7]`, `uio[0..7]`) before touching the model |
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

## Explicitly not doing

- No FA/hour measurement, no negative-audio corpus, no retrain (decision 2).
- No `STATE_W=9` shrink; it changes the front end and would require re-extraction and retrain.
- No H=8; needs 95.6 % util.
