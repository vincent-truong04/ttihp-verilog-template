/*
 * uart_tx.v - fixed-format 8N1 UART transmitter
 *
 * Frame: 1 start bit (0), 8 data bits LSB-first, 1 stop bit (1).
 * Line idles high, including during and right after reset.
 *
 * start is a single-cycle pulse. It is only accepted when idle; pulses while
 * busy are ignored. data is latched on the cycle start is accepted.
 */
`default_nettype none

module uart_tx #(
    parameter integer CLKS_PER_BIT = 434  // 50 MHz / 115200 baud
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output reg        tx,
    output wire       busy
);

  localparam [1:0] S_IDLE  = 2'd0,
                   S_START = 2'd1,
                   S_DATA  = 2'd2,
                   S_STOP  = 2'd3;

  localparam integer CW = $clog2(CLKS_PER_BIT);

  reg [1:0]    state;
  reg [CW-1:0] baud_cnt;
  reg [2:0]    bit_idx;
  reg [7:0]    shift;

  // High on the last clock of each bit period
  wire baud_tick = (baud_cnt == CLKS_PER_BIT - 1);

  assign busy = (state != S_IDLE);

  always @(posedge clk) begin
    if (!rst_n) begin
      state    <= S_IDLE;
      tx       <= 1'b1;
      baud_cnt <= {CW{1'b0}};
      bit_idx  <= 3'd0;
      shift    <= 8'd0;
    end else begin
      case (state)
        S_IDLE: begin
          tx       <= 1'b1;
          baud_cnt <= {CW{1'b0}};
          bit_idx  <= 3'd0;
          if (start) begin
            shift <= data;
            tx    <= 1'b0;          // start bit
            state <= S_START;
          end
        end

        S_START: begin
          if (baud_tick) begin
            baud_cnt <= {CW{1'b0}};
            tx       <= shift[0];   // first data bit (LSB)
            state    <= S_DATA;
          end else begin
            baud_cnt <= baud_cnt + 1'b1;
          end
        end

        S_DATA: begin
          if (baud_tick) begin
            baud_cnt <= {CW{1'b0}};
            if (bit_idx == 3'd7) begin
              tx    <= 1'b1;        // stop bit
              state <= S_STOP;
            end else begin
              bit_idx <= bit_idx + 1'b1;
              shift   <= {1'b0, shift[7:1]};
              tx      <= shift[1];  // next data bit
            end
          end else begin
            baud_cnt <= baud_cnt + 1'b1;
          end
        end

        S_STOP: begin
          if (baud_tick) begin
            baud_cnt <= {CW{1'b0}};
            state    <= S_IDLE;
          end else begin
            baud_cnt <= baud_cnt + 1'b1;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end

endmodule
