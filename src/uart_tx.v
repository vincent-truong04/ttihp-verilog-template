/*
 * uart_tx.v -- UART transmitter, 8 data bits, no parity, 1 stop bit (8N1)
 * SPDX-License-Identifier: Apache-2.0
 *
 * Line format (the line idles HIGH):
 *
 *   idle | start | d0 d1 d2 d3 d4 d5 d6 d7 | stop | idle
 *   1111 |   0   | LSB first ...       MSB |  1   | 1111
 *
 * Every bit lasts exactly clks_per_bit clock cycles.
 *   clks_per_bit = f_clk / baud   e.g. 10 MHz / 115200 = 87 (rounded)
 *
 * Usage: when busy == 0, put a byte on tx_data and pulse start for one cycle.
 */
`default_nettype none

module uart_tx (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] clks_per_bit,  // baud divider, must be >= 2
    input  wire        start,         // 1-cycle pulse: begin sending tx_data
    input  wire [7:0]  tx_data,
    output reg         tx,            // serial output (idles high)
    output wire        busy           // high while a frame is being sent
);

  // The whole frame (start + 8 data + stop) lives in a 10-bit shift register.
  // We shift it out LSB first, so the start bit (0) goes in bit 0.
  reg [9:0]  shreg;
  reg [3:0]  bits_left;   // how many bits of the frame are still to send
  reg [15:0] baud_cnt;    // counts clock cycles within one bit

  assign busy = (bits_left != 0);

  // shreg[0] (the start bit) is never read: we drive tx low directly on
  // start. Mark it as intentionally unused to keep the linter quiet.
  wire _unused = shreg[0];

  always @(posedge clk) begin
    if (!rst_n) begin
      tx        <= 1'b1;  // idle high
      shreg     <= 10'h3FF;
      bits_left <= 4'd0;
      baud_cnt  <= 16'd0;
    end else if (!busy) begin
      tx <= 1'b1;
      if (start) begin
        //         stop   data     start
        shreg     <= {1'b1, tx_data, 1'b0};
        bits_left <= 4'd10;
        baud_cnt  <= 16'd0;
        tx        <= 1'b0;  // start bit goes out right away
      end
    end else begin
      if (baud_cnt == clks_per_bit - 1) begin
        // Current bit has been on the line long enough: move to the next one.
        baud_cnt  <= 16'd0;
        shreg     <= {1'b1, shreg[9:1]};
        bits_left <= bits_left - 4'd1;
        tx        <= shreg[1];  // next bit (a 1 after the stop bit = idle)
      end else begin
        baud_cnt <= baud_cnt + 16'd1;
      end
    end
  end

endmodule