`default_nettype none
`timescale 1ns / 1ps

// Parameter overrides come in as +defines; iverilog's -P silently no-ops here.
`ifndef WW_FRAME_LOG2
  `define WW_FRAME_LOG2 16
`endif
`ifndef WW_NBAND
  `define WW_NBAND 8
`endif
`ifndef WW_NHID
  `define WW_NHID 4
`endif

/* Testbench wrapper for tt_um_wakeword. */
module tb ();

  initial begin
    $dumpfile("tb.vcd");
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

  tt_um_wakeword #(
      .FRAME_LOG2(`WW_FRAME_LOG2),
      .NBAND     (`WW_NBAND),
      .NHID      (`WW_NHID)
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
