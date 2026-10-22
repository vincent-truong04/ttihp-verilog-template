/*
 * Copyright (c) 2026 Vincent Truong
 * SPDX-License-Identifier: Apache-2.0
 *
 * Hello-world top level: fixed UART TX.
 * Placeholder until the programmable protocol core replaces it.
 *
 *   ui_in[7:0]  data byte to send (hold stable when pulsing GO)
 *   uio_in[0]   GO: rising edge starts a transmission (ignored while busy)
 *   uo_out[0]   UART TX (8N1, 115200 baud at 50 MHz)
 *   uo_out[1]   BUSY
 */
`default_nettype none

module tt_um_vincent_truong04_protoemu (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

  // GO comes from off-chip, so synchronize it, then detect the rising edge
  reg [2:0] go_sync;
  always @(posedge clk) begin
    if (!rst_n) go_sync <= 3'b000;
    else        go_sync <= {go_sync[1:0], uio_in[0]};
  end
  wire go_rise = go_sync[1] & ~go_sync[2];

  wire tx;
  wire busy;

  uart_tx #(
      .CLKS_PER_BIT(434)
  ) u_uart_tx (
      .clk  (clk),
      .rst_n(rst_n),
      .start(go_rise),
      .data (ui_in),
      .tx   (tx),
      .busy (busy)
  );

  assign uo_out  = {6'b0, busy, tx};
  assign uio_out = 8'b0;
  assign uio_oe  = 8'b0;  // all bidirectional pins are inputs

  // Avoid unused-signal warnings
  wire _unused = &{ena, uio_in[7:1], 1'b0};

endmodule
