<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

A self-contained wake-word spotter for the word **"sheila"**. A PDM microphone
bit-stream goes in on one pin and an LED comes out on another. There is no
host, no memory, and nothing to load: the classifier weights are hard-wired
constants baked into the logic at tape-out.

The signal chain is integer throughout and contains no multiplier:

1. **Mic clock and sampling.** The chip divides its 50 MHz clock by 32 to
   produce a 1.5625 MHz PDM clock on `uo[0]` and samples one microphone bit
   per period on `ui[0]`.
2. **Octave filterbank.** A 9-stage dyadic one-pole cascade
   (`state += (in - state) >> 2`, stage *b* clocked every 2^b ticks) built as
   a rotating shift register, so one shared subtract-shift-add serves every
   stage. Adjacent stage differences give five octave band-pass signals.
3. **Log magnitude.** A priority encoder plus one mantissa bit turns each
   band into a 4-bit log2 level in 3 dB steps. The log is free: it is the
   encoder output, not a computation.
4. **Framing.** Per-band maximum over 41.9 ms frames (65 536 mic ticks).
   Sixteen frames make one 671 ms window.
5. **Classifier.** Four hidden units, each a 16×5 ternary template
   ({-1, 0, +1}) accumulated frame by frame into a 7-bit saturating
   accumulator, then `clamp(acc >> 1, 0, 15)`. A ternary output layer sums
   the four activations and compares against a threshold. Two staggered
   windows (hop = 8 frames) mean the word need not align with a window
   boundary.
6. **Detect.** On a hit the detect output is latched high for 16 frames
   (about 0.67 s).

The weights come from quantisation-aware training on Google Speech Commands
v0.02 against a bit-exact Python model of this exact datapath, including the
per-frame accumulator saturation. Measured AUC on the speaker-disjoint test
split is 89 %. Recall at a fixed false-alarm rate has **not** been measured;
see `FINDINGS.md` in the repository for what is and is not established.

## How to test

1. Connect a 3.3 V PDM MEMS microphone: its `DATA` pin to `ui[0]`, its
   `CLK` pin to `uo[0]`, plus 3.3 V and ground from the board. Leave the mic's
   L/R select pin at its default (data valid on the clock edge the chip
   samples on: mid-way through the high phase).
2. Set the threshold trim on `ui[7:1]` to the neutral value `1000000`
   (`ui[7]` = 1, `ui[6:1]` = 0). The trim is a signed offset added to the
   trained threshold in steps of 4; higher values make the detector stricter,
   lower values make it fire more readily.
3. Apply the 50 MHz clock and release reset.
4. Say "sheila" at conversational distance. `uo[3]` (and its mirrors `uo[1]`,
   `uo[2]`) go high for about two thirds of a second. `uo[7:4]` show the
   live level of the lowest band as a 4-bit value, which is a quick way to
   confirm the microphone is alive: it should move when you speak.
5. If it triggers too often on other speech, raise the trim; if it never
   fires, lower it.

The debug pins on `uio[7:0]` expose the frame index (`uio[3:0]`), the detect
line (`uio[4]`), the FSM state (`uio[6:5]`) and the mic tick (`uio[7]`). They
are outputs only.

## External hardware

- One 3.3 V PDM MEMS microphone breakout (for example the Adafruit PDM MEMS
  Microphone Breakout). Two signal wires: data to `ui[0]`, clock from
  `uo[0]`.
- The on-board LEDs on `uo[1..3]` show the detection; no additional LED is
  required.
- The on-board DIP switches on `ui[7:1]` set the threshold trim.
