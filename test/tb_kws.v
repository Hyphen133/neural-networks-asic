`default_nettype none
`timescale 1ns / 1ps

`include "kws_weights.svh"

/* Testbench wrapper for tt_um_kws -- instantiates the module and makes all the
   TinyTapeout ports visible to cocotb.

   Parameter overrides come in as -D macros rather than iverilog's -P, because
   -P is silently ignored by Icarus 13.0 unless -s is also given, and errors
   out when it is. A build whose parameters did not take effect would compare
   the RTL against a differently configured golden model and "pass", so the
   override path is made explicit here and re-checked at run time in
   test_kws.py against the elaborated values. */

`ifndef P_NBANK
  `define P_NBANK `KWS_NBANK
`endif
`ifndef P_NSLOT
  `define P_NSLOT `KWS_NSLOT
`endif
`ifndef P_FEAT_W
  `define P_FEAT_W `KWS_FEAT_W
`endif
`ifndef P_FRAME_LOG2
  `define P_FRAME_LOG2 `KWS_FRAME_LOG2
`endif
`ifndef P_STATE_W
  `define P_STATE_W 14
`endif
`ifndef P_IN_SHIFT
  `define P_IN_SHIFT 7
`endif
`ifndef P_ENV_W
  `define P_ENV_W 12
`endif
`ifndef P_ENV_SHIFT
  `define P_ENV_SHIFT 2
`endif
`ifndef P_DECAY
  `define P_DECAY 9
`endif
`ifndef P_HOLD_FRAMES
  `define P_HOLD_FRAMES 6
`endif
module tb ();

  initial begin
    $dumpfile("tb_kws.vcd");
    $dumpvars(0, tb);
    #1;
  end

  reg        clk;
  reg        rst_n;
  reg        ena;
  reg  [7:0] ui_in;
  reg  [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

`ifdef GL_TEST
  wire VPWR = 1'b1;
  wire VGND = 1'b0;
`endif

  tt_um_kws #(
      .NBANK      (`P_NBANK),
      .NSLOT      (`P_NSLOT),
      .FEAT_W     (`P_FEAT_W),
      .FRAME_LOG2 (`P_FRAME_LOG2),
      .STATE_W    (`P_STATE_W),
      .IN_SHIFT   (`P_IN_SHIFT),
      .ENV_W      (`P_ENV_W),
      .ENV_SHIFT  (`P_ENV_SHIFT),
      .DECAY      (`P_DECAY),
      .HOLD_FRAMES(`P_HOLD_FRAMES)
  ) user_project (
`ifdef GL_TEST
      .VPWR   (VPWR),
      .VGND   (VGND),
`endif
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

endmodule
