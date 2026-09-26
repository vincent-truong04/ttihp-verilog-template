/*
 * uart_rx.v -- UART receiver, 8N1
 * SPDX-License-Identifier: Apache-2.0
 *
 * The receiver doesn't share a clock with the sender, so it:
 *   1. Synchronizes the raw rx pin into our clock domain (2 flip-flops).
 *   2. Waits for the falling edge that marks a start bit.
 *   3. Waits HALF a bit to land in the middle of the start bit, and checks
 *      it's still low (if not, it was just a glitch).
 *   4. Then waits one FULL bit at a time, sampling each data bit in its middle.
 *   5. Checks the stop bit is high. If not, flags a framing error.
 *
 * Sampling in the middle gives the most tolerance to baud-rate mismatch
 * between the two ends (roughly +/-4% total for 8N1).
 */
`default_nettype none

module uart_rx (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] clks_per_bit,  // must match the sender's baud rate
    input  wire        rx,            // serial input (asynchronous!)
    output reg  [7:0]  rx_data,       // last received byte
    output reg         rx_valid,      // 1-cycle pulse: rx_data is new
    output reg         frame_err      // 1-cycle pulse: stop bit was 0
);

  // --- 1. Two-flop synchronizer -------------------------------------------
  // rx can change at any moment relative to clk. The first flop may go
  // metastable; the second gives it a full cycle to settle.
  reg rx_meta, rx_sync;
  always @(posedge clk) begin
    if (!rst_n) begin
      rx_meta <= 1'b1;
      rx_sync <= 1'b1;
    end else begin
      rx_meta <= rx;
      rx_sync <= rx_meta;
    end
  end

  // --- 2..5. Receive state machine ----------------------------------------
  localparam S_IDLE  = 2'd0;
  localparam S_START = 2'd1;  // waiting for middle of start bit
  localparam S_DATA  = 2'd2;  // sampling the 8 data bits
  localparam S_STOP  = 2'd3;  // sampling the stop bit

  reg [1:0]  state;
  reg [15:0] baud_cnt;
  reg [2:0]  bit_idx;
  reg [7:0]  shreg;

  always @(posedge clk) begin
    if (!rst_n) begin
      state     <= S_IDLE;
      baud_cnt  <= 16'd0;
      bit_idx   <= 3'd0;
      shreg     <= 8'd0;
      rx_data   <= 8'd0;
      rx_valid  <= 1'b0;
      frame_err <= 1'b0;
    end else begin
      rx_valid  <= 1'b0;  // default: pulses last one cycle
      frame_err <= 1'b0;

      case (state)
        S_IDLE: begin
          baud_cnt <= 16'd0;
          if (!rx_sync) state <= S_START;  // line went low: maybe a start bit
        end

        S_START: begin
          if (baud_cnt == (clks_per_bit >> 1)) begin  // middle of start bit
            baud_cnt <= 16'd0;
            bit_idx  <= 3'd0;
            state    <= rx_sync ? S_IDLE : S_DATA;  // high again = glitch
          end else begin
            baud_cnt <= baud_cnt + 16'd1;
          end
        end

        S_DATA: begin
          if (baud_cnt == clks_per_bit - 1) begin  // middle of next data bit
            baud_cnt <= 16'd0;
            shreg    <= {rx_sync, shreg[7:1]};     // LSB arrives first
            bit_idx  <= bit_idx + 3'd1;
            if (bit_idx == 3'd7) state <= S_STOP;
          end else begin
            baud_cnt <= baud_cnt + 16'd1;
          end
        end

        S_STOP: begin
          if (baud_cnt == clks_per_bit - 1) begin  // middle of stop bit
            baud_cnt <= 16'd0;
            state    <= S_IDLE;
            if (rx_sync) begin
              rx_data  <= shreg;
              rx_valid <= 1'b1;
            end else begin
              frame_err <= 1'b1;
            end
          end else begin
            baud_cnt <= baud_cnt + 16'd1;
          end
        end
      endcase
    end
  end

endmodule