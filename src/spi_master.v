/*
 * spi_master.v -- SPI controller, one byte per transfer, all four modes
 * SPDX-License-Identifier: Apache-2.0
 *
 * SPI is full duplex: every SCLK period, one bit goes out on MOSI and one
 * bit comes in on MISO at the same time. After 8 clocks, the byte we sent
 * and the byte we received have swapped places.
 *
 * Each bit has two SCLK edges: a LEADING edge and a TRAILING edge.
 *   CPOL = idle level of SCLK (0 = idles low, 1 = idles high)
 *   CPHA = 0: sample on leading edge, change data on trailing edge
 *          (so the first bit must already be on MOSI before the first edge)
 *   CPHA = 1: change data on leading edge, sample on trailing edge
 *
 *   mode 0 = CPOL 0, CPHA 0   (the most common)
 *   mode 1 = CPOL 0, CPHA 1
 *   mode 2 = CPOL 1, CPHA 0
 *   mode 3 = CPOL 1, CPHA 1
 *
 * SCLK frequency = f_clk / (2 * half_period).
 *
 * This block drives cs_n low for exactly one byte. For multi-byte
 * transactions you'd hold CS low across bytes; that's a good exercise.
 */
`default_nettype none

module spi_master (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        cpol,
    input  wire        cpha,
    input  wire        lsb_first,    // 0 = MSB first (the usual)
    input  wire [15:0] half_period,  // clk cycles per half SCLK period, >= 1
    input  wire        start,        // 1-cycle pulse: begin transfer
    input  wire [7:0]  tx_data,
    output reg  [7:0]  rx_data,
    output reg         done,         // 1-cycle pulse: rx_data is valid
    output wire        busy,
    output reg         sclk,
    output wire        mosi,
    input  wire        miso,
    output reg         cs_n
);

  localparam S_IDLE  = 2'd0;
  localparam S_SETUP = 2'd1;  // CS low, give the target half a period to wake up
  localparam S_XFER  = 2'd2;  // toggling SCLK
  localparam S_HOLD  = 2'd3;  // half a period before raising CS again

  reg [1:0]  state;
  reg [15:0] cnt;
  reg [4:0]  edge_idx;  // 0..15: even = leading edge, odd = trailing edge
  reg [7:0]  tx_sr;     // bits waiting to go out; tx_sr[7] is on MOSI
  reg [7:0]  rx_sr;

  assign busy = (state != S_IDLE);
  assign mosi = tx_sr[7];

  // Reverse bit order so the rest of the logic can always be "MSB first".
  function [7:0] rev8(input [7:0] b);
    rev8 = {b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]};
  endfunction

  wire leading      = ~edge_idx[0];
  wire sample_edge  = cpha ? ~leading : leading;
  // CPHA=0: shift on trailing edges. CPHA=1: shift on leading edges, except
  // the very first one (bit 7 is already sitting on MOSI).
  wire shift_edge   = cpha ? (leading && edge_idx != 0) : ~leading;

  always @(posedge clk) begin
    if (!rst_n) begin
      state    <= S_IDLE;
      cnt      <= 16'd0;
      edge_idx <= 5'd0;
      tx_sr    <= 8'd0;
      rx_sr    <= 8'd0;
      rx_data  <= 8'd0;
      done     <= 1'b0;
      sclk     <= 1'b0;
      cs_n     <= 1'b1;
    end else begin
      done <= 1'b0;

      case (state)
        S_IDLE: begin
          sclk <= cpol;  // park SCLK at its idle level
          if (start) begin
            tx_sr    <= lsb_first ? rev8(tx_data) : tx_data;
            cs_n     <= 1'b0;
            cnt      <= 16'd0;
            edge_idx <= 5'd0;
            state    <= S_SETUP;
          end
        end

        S_SETUP: begin
          if (cnt == half_period - 1) begin
            cnt   <= 16'd0;
            state <= S_XFER;
          end else cnt <= cnt + 16'd1;
        end

        S_XFER: begin
          if (cnt == half_period - 1) begin
            cnt  <= 16'd0;
            sclk <= ~sclk;  // this is edge number edge_idx
            if (sample_edge) rx_sr <= {rx_sr[6:0], miso};
            if (shift_edge)  tx_sr <= {tx_sr[6:0], 1'b0};
            edge_idx <= edge_idx + 5'd1;
            if (edge_idx == 5'd15) state <= S_HOLD;
          end else cnt <= cnt + 16'd1;
        end

        S_HOLD: begin
          if (cnt == half_period - 1) begin
            cnt     <= 16'd0;
            cs_n    <= 1'b1;
            rx_data <= lsb_first ? rev8(rx_sr) : rx_sr;
            done    <= 1'b1;
            state   <= S_IDLE;
          end else cnt <= cnt + 16'd1;
        end
      endcase
    end
  end

endmodule