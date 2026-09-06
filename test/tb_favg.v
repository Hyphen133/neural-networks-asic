`default_nettype none
`timescale 1ns / 1ps

// Bit-exactness testbench for the AVG_N front end, without cocotb.
//
// cocotb is not installed in this environment and cannot be (no pip in the
// venv, no host iverilog, no network in the librelane image) -- but that image
// does carry iverilog and vvp, so this drives the design from a plain Verilog
// testbench instead. It reads a PDM bit stream from `bits.txt`, runs the
// design, and at every entry to S_CLASS prints the NBAND frame maxima and the
// AVG_N frame means. train/optim/check_favg.py generates the stimulus,
// computes the same values with the software model, and diffs them.
//
// The averaging constraint TAP0+NBAND-1 <= FRAME_LOG2-AVG_SHIFT has to hold,
// so the shortened frame used here is FRAME_LOG2=12 with AVG_SHIFT=4 rather
// than the tape-out FRAME_LOG2=16 with AVG_SHIFT=6. Same arithmetic, 16x less
// simulation.

module tb_favg;

  localparam NBAND      = 6;
  localparam AVG_N      = 3;
  localparam AVG_SHIFT  = 4;
  localparam FEAT_W     = 4;
  localparam NFRAMES    = 4;
  localparam FRAME_LOG2 = 12;
  localparam PDM_DIV    = 32;
  localparam N_TICKS    = NFRAMES * (1 << FRAME_LOG2);

  reg clk = 0, rst_n = 0, ena = 1;
  reg [7:0] ui_in = 8'h02;          // trim = 1, data bit in [0]
  reg [7:0] uio_in = 0;
  wire [7:0] uo_out, uio_out, uio_oe;

  reg [0:0] bits [0:N_TICKS-1];
  integer   tick, k, cyc;
  integer   prev_st, seen;

  tt_um_wakeword #(
      .NSTAGE(9), .K_SHIFT(2), .STATE_W(9), .TAP0(3), .NBAND(NBAND),
      .MANT(1), .FEAT_W(FEAT_W), .AVG_N(AVG_N), .AVG_SHIFT(AVG_SHIFT),
      .FRAME_LOG2(FRAME_LOG2), .NFRAME(4), .NPHASE(1), .NHID(4),
      .HACC_W(5), .HSHIFT(1), .FEAT_OFF(2), .SCORE_W(10)
  ) dut (
      .ui_in(ui_in), .uo_out(uo_out), .uio_in(uio_in), .uio_out(uio_out),
      .uio_oe(uio_oe), .ena(ena), .clk(clk), .rst_n(rst_n)
  );

  always #10 clk = ~clk;

  initial begin
    $readmemb("bits.txt", bits);
    repeat (8) @(posedge clk);
    rst_n = 1;
    repeat (2) @(posedge clk);

    prev_st = 0;
    seen    = 0;
    for (tick = 0; tick < N_TICKS; tick = tick + 1) begin
      ui_in = {6'd0, 1'b1, bits[tick][0]};    // trim = 1
      for (cyc = 0; cyc < PDM_DIV; cyc = cyc + 1) begin
        @(posedge clk);
        // S_CLASS == 2. Sample on entry, before S_ROLL clears the rings.
        if (dut.st == 2 && prev_st != 2) begin
          $write("FRAME %0d MAX", seen);
          for (k = 0; k < NBAND; k = k + 1) $write(" %0d", dut.fmax[k]);
          $write(" AVG");
          for (k = 0; k < AVG_N; k = k + 1)
            $write(" %0d", dut.favg[k] >> AVG_SHIFT);
          $write("\n");
          seen = seen + 1;
        end
        prev_st = dut.st;
      end
    end
    $display("DONE %0d frames", seen);
    $finish;
  end

endmodule
