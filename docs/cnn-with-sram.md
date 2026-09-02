# Can we do a CNN on Tiny Tapeout with an SRAM macro?

**Short answer: yes, and the cheapest credible design point is 2 tiles, not 12.**

`design-space-1x1.md` §2 states "there is no SRAM macro available at 1x1" and
concludes that every 784→16 style layer must stream weights from the host. That
is still true *at 1x1*. But [tinytapeout.com/specs/memory](https://tinytapeout.com/specs/memory/)
now lists pregenerated IHP `sg13g2_sram` macros, and the smallest one needs
**1x2 = 2 tiles**. Going from 1 tile to 2 tiles buys 2–16 kbit of real SRAM
*and* more standard-cell area than the current 1x1 design uses. That is enough
to change the answer.

---

## 1. The reframe: for a CNN, SRAM is for activations, not weights

This is the observation that makes the estimate come out well.

A conv kernel is **tiny**. The whole conv stack of a useful MNIST CNN is
1 400–3 700 weights. Compare 12 544 weights for the single 784→16 layer in
`src/tt_um_mnist_nn.sv`. Convolution's weight *reuse* is exactly what removes
the weight wall — a 3×3 kernel is 9 nibbles, 36 bits, which fits in **flops**.

What a CNN needs and the current design does not is a place to put a **feature
map**. `28×28×8` at 4 bits is 25 kbit; even after fusing pool into conv, a
`13×13×8` intermediate is 5.4 kbit. That is 18× the ~300 bits of register state
a 1x1 tile holds, and it is *not* streamable from the host, because every
activation is read back 9·C_out times by the next layer.

So the SRAM decision is: **buy activation memory first, weight memory only if
you want the chip to be self-contained.**

---

## 2. Budget per macro (derived from the TT memory spec)

Standard-cell area left over = `(tiles × 31 318 − macro_area) × 0.60`, using
OpenLane's default 60 % placement density, the same convention as
`design-space-1x1.md` §1. "vs H16" compares against the taped-out
`HIDDEN=16` design's measured 15 688 µm². "flops" = that area if spent
entirely on `sg13g2_dfrbp_1`.

| macro (1RW) | kbit | min tiles | tile area µm² | macro % | logic µm² | vs H16 | ≈flops |
|---|---:|---:|---:|---:|---:|---:|---:|
| 256x8   |  2 |  2 |  62 637 | 28.0 % |  27 054 | 1.72× | 514 |
| 512x8   |  4 |  2 |  62 637 | 41.7 % |  21 899 | 1.40× | 416 |
| 256x16  |  4 |  2 |  62 637 | 44.9 % |  20 706 | 1.32× | 393 |
| **1024x16** | **16** | **4** | **125 273** | **63.6 %** | **27 360** | **1.74×** | **520** |
| 512x16  |  8 |  2 |  62 637 | 72.3 % |  10 396 | 0.66× | 198 |
| 1024x8  |  8 |  2 |  62 637 | 78.9 % |   7 930 | 0.51× | 151 |
| 256x64  | 16 |  4 | 125 273 | 74.4 % |  19 256 | 1.23× | 366 |
| 512x64  | 32 |  8 | 250 547 | 59.9 % |  60 267 | 3.84× | 1 145 |
| 1024x32 | 32 | 12 | 375 820 | 37.3 % | 141 383 | 9.01× | 2 687 |

Reading the table:

* **1024x8 and 512x16 are traps.** They are the "obvious" 8 kbit choices and
  both eat ~75–79 % of their 2 tiles, leaving *less* logic than the current 1x1
  design. You would have memory and nothing to compute with.
* **1024x16 @ 2x2 is the sweet spot.** 16 kbit *and* 27 360 µm² of logic —
  1.74× the taped-out design — for 4 tiles.
* **512x8 @ 1x2 is the bargain.** 4 kbit + 21 899 µm² logic (1.40× current)
  for 2 tiles. Enough activation memory for a small CNN if weights stay
  host-streamed.
* The 32-bit-wide macros jump to 3x4 = 12 tiles in the spec's table (a width
  of 416 µm exceeds 2 tile widths). Note the spec is internally inconsistent
  here — 256x48 at 596 µm wide is listed as 3x1 while the narrower 256x32 is
  listed as 3x4 — so **confirm 3x1/3x2 availability with TT before costing
  those rows.**
* **Dual-port halves the density** (~1 700–4 700 bits/tile vs ~3 700–8 800).
  Not worth it: time-multiplex a single port instead (§4).

---

## 3. Four CNNs, sized

3×3 valid convs, 2×2 maxpool fused into the conv (so the unpooled map is never
materialised — only 2 rows live in flops), global average pool + FC head to
kill the fully-connected weight explosion. W4A4, matching the current design.

Activation working set = `max over layers (input map + output map)` in nibbles,
assuming the 784-pixel image is streamed in from the host rather than stored.

| # | topology | conv+FC weights | W kbit @4b | act kbit @4b | total kbit | MACs/img |
|---|---|---:|---:|---:|---:|---:|
| **A** | 8-16-16 + GAP + FC | 3 688 | 14.4 | 6.8 | **21.2** | 208 960 |
| **B** | 4-8-16 + GAP + FC | 1 636 |  6.4 | 3.4 | **9.8** |  69 712 |
| **C** | 8-16 + GAP + FC | 1 384 |  5.4 | 6.8 | **12.2** | 188 224 |
| **A′** | A with ternary (2b) weights | 3 688 | 7.2 | 6.8 | **14.0** | 208 960 |

Layer detail for A: `28×28×1 → 26×26×8 → pool → 13×13×8 → 11×11×16 → pool →
5×5×16 → 3×3×16 → GAP(16) → 10`.

### Fits

| design point | tiles | macro | needs | fits? |
|---|---:|---|---:|---|
| B, self-contained | 4 | 1024x16 | 9.8 kbit | **yes**, 61 % of macro, 27 360 µm² logic |
| C, self-contained | 4 | 1024x16 | 12.2 kbit | **yes**, 76 % of macro |
| A′, self-contained | 4 | 1024x16 | 14.0 kbit | **yes**, 88 % of macro — tight |
| A, self-contained | 4 | 1024x16 | 21.2 kbit | **no** — needs 512x64 (8 tiles) or 1024x32 |
| B, host-streamed weights | 2 | 512x8 | 3.4 kbit act | **yes**, 85 % of macro, 21 899 µm² logic |
| C, host-streamed weights | 2 | 512x16 or 1024x8 | 6.8 kbit act | yes on capacity, **but** only 0.5–0.66× current logic — too tight |

Two recommendations fall out:

1. **2-tile CNN**: candidate B + `512x8` for activations, weights still
   streamed from the host on the existing byte protocol. Cheapest real CNN.
2. **4-tile self-contained CNN**: candidate B or C + `1024x16`, weights
   preloaded into SRAM once. This is the qualitatively new thing — a chip that
   classifies from a raw image with no host in the loop.

Ternary weights, which `design-space-1x1.md` correctly rejected for the dense
design (3.1 % area saving for 2.7 accuracy points), become *interesting again*
here for a different reason: they halve **SRAM** occupancy, not cell area.
A′ shows they are what makes the biggest topology fit 16 kbit.

---

## 4. Bandwidth: the second wall

A single-port macro gives **one word per cycle, total** — shared between weight
reads and activation reads. That is the real throughput limit, not the MACs.

With a 16-bit word = 4 nibbles:

* read 4 weights (1 cycle) + 4 activations (1 cycle) → 4 MACs per 2 cycles =
  **2 MACs/cycle**, needing 4 parallel 4×4 multipliers and an adder tree
  (~2 000 µm² — affordable in 27 360).
* Better, using conv weight reuse: hold the 3×3 window (9 nibbles = 36 flops)
  in a shift register and stream weights, or hold a whole `3×3×C_in=4` kernel
  (144 flops) and stream activations. Either keeps the port ~100 % busy.
* Keep the **output-channel-outer loop** trick from the current design: one
  accumulator, re-read the input window once per output channel. 16 accumulators
  would cost 256 flops of the ~520 budget; re-reading costs only cycles.

Throughput at 50 MHz:

| design | MACs/img | @1 MAC/cyc | @2 MAC/cyc |
|---|---:|---:|---:|
| A | 208 960 | 239 img/s | 479 img/s |
| B |  69 712 | 717 img/s | 1 434 img/s |
| C | 188 224 | 266 img/s | 531 img/s |

For reference the current 1x1 design does 3 900 img/s but requires the host to
push 12 838 bytes per image. The CNN is ~5× slower and needs **zero** host
bandwidth. For any plausible demo (a camera, a button, a person drawing a
digit) 500 img/s is far more than enough.

---

## 5. Expected accuracy — unverified

Literature for W4A4 CNNs of this shape on MNIST puts candidate B around
**~98 %** and candidate A around **~98.5–99 %**, against the measured 95.47 %
of the current dense `784→16→10`. That is the payoff: roughly halving to
quartering the error rate *and* removing the host.

**These numbers are estimates, not measurements.** The project already has the
tooling to settle them cheaply — `train/train.py` does W4A4/ternary QAT with
straight-through estimators and `train/hw.py` is a bit-exact integer model. The
correct next step is to train B, C and A′ and get real accuracy numbers
*before* committing to a tile count, exactly as §3 of `design-space-1x1.md`
insists on synthesis before commitment.

---

## 6. Risks, in order of how likely they are to sink this

1. **SRAM integration is explicitly not trivial.** The spec says so in as many
   words: "There have been many changes since the example project linked above,
   and additional changes will come after TTIHP26A. The table below should be
   used as rough guidance only, and is subject to change at any time." Every
   number in §2 above inherits that caveat.
2. **Only `1024x8` has proven silicon** (`tt_um_urish_sram_test` on ttihp0p2).
   `1024x16`, the recommended macro, has not been demonstrated on a TT shuttle
   as far as the spec says. That is the single biggest technical risk in the
   4-tile plan.
3. **Power hookup.** The spec flags connecting macro power/ground as the hard
   part even for DFFRAM. Budget real time for this, not an afternoon.
4. **Rotation and floorplan.** Most useful macros are marked `*` = must be
   rotated 90° to fit. With the macro occupying 64 % of a 2x2 area, OpenLane
   has to route the whole design around one large obstruction. Expect
   floorplan iteration.
5. **Tile cost.** 4 tiles is 4× the shuttle cost of the current design.

### The de-risked path

Given risk 2, the ordering that dominates is:

1. Train B / C / A′ with the existing QAT harness. Cost: hours. If the accuracy
   gain over 95.47 % is not there, stop — nothing else matters.
2. Write the RTL against a **behavioural** SRAM model and verify with cocotb
   against `hw.py`, exactly as the current design is verified. No macro yet.
3. Synthesise for area with `area_check.sh` to confirm the logic fits
   21 899 µm² (2-tile) / 27 360 µm² (4-tile).
4. *Then* attempt macro integration — and if it fights back, fall back to the
   2-tile host-streamed-weight version, whose only external dependency is a
   `512x8` macro, or even to no macro at all with a reduced-resolution input.

Step 4 is the only step with unbounded schedule risk, and steps 1–3 produce a
working, verified, tapeable design without it.
