// SPDX-FileCopyrightText: 2026
// SPDX-License-Identifier: Apache-2.0
//
// tt_um_mnist_nn -- streaming W4A4 MNIST MLP inference engine for a
// TinyTapeout IHP 1x1 tile.
//
// Network:   784 -> HIDDEN -> 10, fully connected.
// Arithmetic: pixels uint4, weights int4, one shared 4x4 multiplier,
//             ONE shared ACC_W-bit accumulator, HIDDEN x 4-bit hidden
//             activation register file, streaming argmax.
//
// The host (MCU/FPGA/USB bridge) owns all weights and drives the loop
// order so that the chip only ever needs a single accumulator:
//
//   for n in 0..HIDDEN-1:            // one hidden neuron at a time
//       LOAD_ACC(bias1[n])
//       for p in 0..783: DATA{pixel[p], w1[n][p]}
//       NEURON_DONE                  // hidden[n] = clamp(acc >>> s1, 0, 15)
//   START_L2
//   for c in 0..9:
//       LOAD_ACC(bias2[c])
//       for h in 0..HIDDEN-1: DATA{-, w2[c][h]}
//       CLASS_DONE                   // running argmax
//   FINISH
//
// Everything is bit-exact integer; see train/hw.py for the golden model.

`default_nettype none

module tt_um_mnist_nn #(
    // Hidden layer width. 8 / 16 / 32 all synthesise; 16 is the default
    // sweet spot for a 1x1 tile (HIDDEN*4 flops of activation storage).
    parameter HIDDEN = 16,
    // Accumulator width. Worst case layer-1 |sum| = 784*15*8 = 94_080,
    // so 18 bits would do; 20 gives headroom for the loadable bias.
    parameter ACC_W  = 20,
    // 0 = general int4 weights (4x4 signed multiplier).
    // 1 = ternary weights {-1,0,+1}: the multiplier collapses to an
    //     add/sub/zero select. Weight nibble must then be 0x1/0x0/0xF.
    parameter MULT_TERNARY = 0,
    // 16 = shift1 is programmable at run time via SET_SHIFT (a 20-bit
    //      barrel shifter). 0..15 = hard-wire the shift and let synthesis
    //      delete both the shifter and the config register.
    parameter SHIFT_FIXED = 16
) (
    input  wire [7:0] ui_in,    // dedicated inputs  : command / data byte
    output wire [7:0] uo_out,   // dedicated outputs : result + status
    input  wire [7:0] uio_in,   // IOs: input path   : handshake
    output wire [7:0] uio_out,  // IOs: output path  : debug
    output wire [7:0] uio_oe,   // IOs: enable (1 = drive out)
    input  wire       ena,      // always 1 when powered -- unused
    input  wire       clk,
    input  wire       rst_n
);

  localparam HIDX_W = (HIDDEN <= 2)  ? 1 :
                      (HIDDEN <= 4)  ? 2 :
                      (HIDDEN <= 8)  ? 3 :
                      (HIDDEN <= 16) ? 4 : 5;

  // ---------------------------------------------------------------------
  // Host interface
  // ---------------------------------------------------------------------
  wire       in_valid = uio_in[0];  // host: this cycle ui_in is meaningful
  wire       in_cmd   = uio_in[1];  // host: 1 = command byte, 0 = data byte
  wire [7:0] in_byte  = ui_in;

  wire [3:0] opcode = in_byte[7:4];
  wire [3:0] imm    = in_byte[3:0];

  // Opcodes (command byte = {opcode, imm})
  localparam [3:0] OP_NOP        = 4'h0;
  localparam [3:0] OP_RESET      = 4'h1;
  localparam [3:0] OP_SET_SHIFT  = 4'h2;  // imm = layer-1 requant shift
  localparam [3:0] OP_LOAD_ACC   = 4'h3;  // followed by 3 data bytes, MSB first
  localparam [3:0] OP_ZERO_ACC   = 4'h4;
  localparam [3:0] OP_NEURON_DONE= 4'h5;
  localparam [3:0] OP_START_L2   = 4'h6;
  localparam [3:0] OP_CLASS_DONE = 4'h7;
  localparam [3:0] OP_FINISH     = 4'h8;
  localparam [3:0] OP_RD_HIDDEN  = 4'h9;  // imm = hidden index -> uo_out[3:0]
  localparam [3:0] OP_DBG_OFF    = 4'hA;

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  localparam [1:0] S_L1 = 2'd0, S_L2 = 2'd1, S_BIAS = 2'd2;

  logic [1:0]              state, ret_state;
  logic [1:0]              bias_cnt;

  logic signed [ACC_W-1:0] acc;
  logic signed [ACC_W-1:0] best_score;
  logic [3:0]              best_class;
  logic [3:0]              class_idx;
  logic [HIDX_W-1:0]       h_idx;
  logic [3:0]              shift1;
  logic [3:0]              hidden [HIDDEN];

  logic                    result_valid;
  logic                    acc_ovf;
  logic                    dbg_en;
  logic [3:0]              dbg_out;
  logic                    ack;

  // ---------------------------------------------------------------------
  // Datapath: one 4x4 signed-by-unsigned multiplier, one adder
  // ---------------------------------------------------------------------
  // Activation operand: layer 1 takes the pixel from the high nibble of the
  // data byte, layer 2 takes it from the hidden register file.
  wire [3:0] act = (state == S_L2) ? hidden[h_idx] : in_byte[7:4];

  logic signed [9:0] prod;                                     // -120 .. +105
  generate
    if (MULT_TERNARY != 0) begin : g_ternary
      // 2-bit ternary code in the weight nibble: 01 = +1, 11 = -1, else 0.
      always_comb begin
        unique case (in_byte[1:0])
          2'b01:   prod =  $signed({6'b0, act});
          2'b11:   prod = -$signed({6'b0, act});
          default: prod = '0;
        endcase
      end
    end else begin : g_mult4x4
      // Weight is the low nibble, two's complement int4.
      wire signed [4:0] w_ext   = {in_byte[3], in_byte[3:0]};
      wire signed [4:0] act_ext = {1'b0, act};
      always_comb prod = act_ext * w_ext;
    end
  endgenerate

  wire signed [ACC_W-1:0]  prod_ext = {{(ACC_W-10){prod[9]}}, prod};
  wire signed [ACC_W-1:0]  acc_add  = acc + prod_ext;

  // Two's-complement overflow: operands agree in sign, result disagrees.
  wire acc_add_ovf = (acc[ACC_W-1] == prod_ext[ACC_W-1]) &&
                     (acc_add[ACC_W-1] != acc[ACC_W-1]);

  // Requantise: clamp(acc >>> shift1, 0, 15) -- ReLU is free (negative -> 0).
  wire [3:0] eff_shift = (SHIFT_FIXED < 16) ? SHIFT_FIXED[3:0] : shift1;
  wire signed [ACC_W-1:0] acc_sh = acc >>> eff_shift;
  wire [3:0] quant = acc[ACC_W-1]              ? 4'd0  :   // ReLU
                     (|acc_sh[ACC_W-1:4])      ? 4'd15 :   // saturate
                                                 acc_sh[3:0];

  // Bias load: 3 data bytes MSB-first shifted into the accumulator.
  wire signed [ACC_W-1:0] acc_bias = {acc[ACC_W-9:0], in_byte};

  wire beats_best = acc > best_score;

  // Debug readback index. imm is 4 bits, so for HIDDEN=32 only hidden[0..15]
  // are reachable over RD_HIDDEN -- a debug-only limitation.
  /* verilator lint_off UNUSEDSIGNAL */
  wire [4:0]        imm_wide = {1'b0, imm};   // upper bits unread when HIDX_W<5
  /* verilator lint_on UNUSEDSIGNAL */
  wire [HIDX_W-1:0] imm_idx  = imm_wide[HIDX_W-1:0];

  // ---------------------------------------------------------------------
  // Sequential
  // ---------------------------------------------------------------------
  integer i;
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      state        <= S_L1;
      ret_state    <= S_L1;
      bias_cnt     <= 2'd0;
      acc          <= '0;
      best_score   <= {1'b1, {(ACC_W-1){1'b0}}};  // most negative
      best_class   <= 4'd0;
      class_idx    <= 4'd0;
      h_idx        <= '0;
      shift1       <= 4'd5;
      result_valid <= 1'b0;
      acc_ovf      <= 1'b0;
      dbg_en       <= 1'b0;
      dbg_out      <= 4'd0;
      ack          <= 1'b0;
      for (i = 0; i < HIDDEN; i = i + 1) hidden[i] <= 4'd0;
    end else begin
      ack <= in_valid;

      if (in_valid && in_cmd) begin
        // ------------------------------------------------- commands
        case (opcode)
          OP_RESET: begin
            state        <= S_L1;
            ret_state    <= S_L1;
            bias_cnt     <= 2'd0;
            acc          <= '0;
            best_score   <= {1'b1, {(ACC_W-1){1'b0}}};
            best_class   <= 4'd0;
            class_idx    <= 4'd0;
            h_idx        <= '0;
            result_valid <= 1'b0;
            acc_ovf      <= 1'b0;
            dbg_en       <= 1'b0;
            for (i = 0; i < HIDDEN; i = i + 1) hidden[i] <= 4'd0;
          end

          OP_SET_SHIFT: shift1 <= imm;

          OP_LOAD_ACC: begin
            ret_state <= (state == S_BIAS) ? ret_state : state;
            state     <= S_BIAS;
            bias_cnt  <= 2'd0;
            acc       <= '0;
          end

          OP_ZERO_ACC: acc <= '0;

          OP_NEURON_DONE: begin
            hidden[h_idx] <= quant;
            h_idx         <= h_idx + 1'b1;   // wraps at HIDDEN for 2^n
            acc           <= '0;
          end

          OP_START_L2: begin
            state     <= S_L2;
            ret_state <= S_L2;
            h_idx     <= '0;
            class_idx <= 4'd0;
            acc       <= '0;
          end

          OP_CLASS_DONE: begin
            if (beats_best) begin
              best_score <= acc;
              best_class <= class_idx;
            end
            class_idx <= class_idx + 1'b1;
            h_idx     <= '0;
            acc       <= '0;
          end

          OP_FINISH:    result_valid <= 1'b1;

          OP_RD_HIDDEN: begin
            dbg_out <= hidden[imm_idx];
            dbg_en  <= 1'b1;
          end

          OP_DBG_OFF:   dbg_en <= 1'b0;

          OP_NOP:  ;
          default: ;  // reserved opcodes are no-ops
        endcase

      end else if (in_valid) begin
        // ------------------------------------------------- data bytes
        if (state == S_BIAS) begin
          acc <= acc_bias;
          if (bias_cnt == 2'd2) begin
            state    <= ret_state;
            bias_cnt <= 2'd0;
          end else begin
            bias_cnt <= bias_cnt + 1'b1;
          end
        end else begin
          acc     <= acc_add;
          acc_ovf <= acc_ovf | acc_add_ovf;
          if (state == S_L2) h_idx <= h_idx + 1'b1;
        end
      end
    end
  end

  // ---------------------------------------------------------------------
  // Outputs
  // ---------------------------------------------------------------------
  assign uo_out[3:0] = dbg_en ? dbg_out : best_class;
  assign uo_out[4]   = result_valid;
  assign uo_out[5]   = (state == S_L2);
  assign uo_out[6]   = acc_ovf;
  assign uo_out[7]   = ack;

  /* verilator lint_off UNUSEDSIGNAL */
  wire [4:0] h_idx_wide = {{(5-HIDX_W){1'b0}}, h_idx};
  /* verilator lint_on UNUSEDSIGNAL */
  wire [3:0] h_idx4     = h_idx_wide[3:0];

  assign uio_out[1:0] = 2'b00;
  assign uio_out[5:2] = h_idx4;
  assign uio_out[7:6] = state;
  assign uio_oe       = 8'b1111_1100;   // [1:0] inputs, rest debug outputs

  // Unused: ena is tied high by the harness, uio_in[7:2] are driven as outputs.
  wire _unused = &{ena, uio_in[7:2], 1'b0};

endmodule

`default_nettype wire
