/*
 * i2c_master.v -- I2C controller, byte-level command interface
 * SPDX-License-Identifier: Apache-2.0
 *
 * OPEN DRAIN: nobody ever drives SCL/SDA high. A device either pulls a line
 * LOW or releases it, and a pull-up resistor makes it high. So our outputs
 * are "pull low" enables:  scl_oe = 1 -> SCL pulled low,  0 -> released.
 * On Tiny Tapeout: uio_out[n] = 0, uio_oe[n] = scl_oe, scl_in = uio_in[n].
 *
 * The bus rule: SDA may only change while SCL is LOW. Changing SDA while SCL
 * is HIGH is how START (SDA falls) and STOP (SDA rises) are signalled.
 *
 * A byte is 9 SCL clocks: 8 data bits MSB first, then 1 ACK bit where the
 * RECEIVER pulls SDA low (ACK) or leaves it high (NACK).
 *
 * The same 9-bit loop handles both directions:
 *   write: send {data, 1}   (the final 1 = release SDA so the target can ACK)
 *   read:  send {FF, ~ack}  (release SDA for 8 bits, then we ACK or NACK)
 * and we sample SDA on all 9 bits either way.
 *
 * Each step is split into four "quarter" periods:
 *   SCL period ~= 4 * quarter + 3 clk cycles  (plus any clock stretching)
 * The +3: after releasing SCL we only start timing once we SEE it high,
 * and seeing it takes ~3 cycles through the input synchronizer. So the
 * high time is 2*quarter + 3 and the low time is exactly 2*quarter.
 *   e.g. 10 MHz clk, quarter = 25  ->  SCL ~ 97 kHz (standard mode)
 *
 * Clock stretching: after releasing SCL we WAIT until it actually reads high,
 * because a slow target is allowed to hold it low.
 *
 * Commands (pulse one when busy == 0):
 *   cmd_start  -> START, or repeated START if a transaction is in progress
 *   cmd_write  -> send wr_data, then ack_received = target ACKed
 *   cmd_read   -> receive rd_data, then send ACK if send_ack=1, else NACK
 *   cmd_stop   -> STOP
 */
`default_nettype none

module i2c_master (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] quarter,  // clk cycles per quarter SCL period, >= 1
    input  wire        cmd_start,
    input  wire        cmd_stop,
    input  wire        cmd_write,
    input  wire        cmd_read,
    input  wire [7:0]  wr_data,
    input  wire        send_ack,  // for reads: 1 = ACK (want more), 0 = NACK
    output reg  [7:0]  rd_data,
    output reg         ack_received,
    output wire        busy,
    output reg         done,      // 1-cycle pulse when a command finishes
    // bus pins
    output reg         scl_oe,    // 1 = pull SCL low
    output reg         sda_oe,    // 1 = pull SDA low
    input  wire        scl_in,
    input  wire        sda_in
);

  // Synchronize the bus inputs (they're driven by other chips).
  reg [1:0] scl_s, sda_s;
  always @(posedge clk) begin
    if (!rst_n) begin
      scl_s <= 2'b11;
      sda_s <= 2'b11;
    end else begin
      scl_s <= {scl_s[0], scl_in};
      sda_s <= {sda_s[0], sda_in};
    end
  end
  wire scl = scl_s[1];
  wire sda = sda_s[1];

  localparam S_IDLE  = 2'd0;
  localparam S_START = 2'd1;
  localparam S_BYTE  = 2'd2;
  localparam S_STOP  = 2'd3;

  reg [1:0]  state;
  reg [1:0]  phase;    // which quarter of the current step we're in
  reg [15:0] cnt;
  reg [3:0]  bit_idx;  // 0..8 within a byte
  reg [8:0]  tx_sr;    // bit 8 goes out next; 1 = release SDA
  reg [8:0]  rx_sr;

  assign busy = (state != S_IDLE);

  // A phase ends after `quarter` cycles. If this phase released SCL, the
  // timer is held at 0 until SCL is really high (clock stretching).
  wire waiting_for_scl = !scl_oe && !scl;
  wire phase_end = (cnt == quarter - 1) && !waiting_for_scl;

  always @(posedge clk) begin
    if (!rst_n) begin
      state        <= S_IDLE;
      phase        <= 2'd0;
      cnt          <= 16'd0;
      bit_idx      <= 4'd0;
      tx_sr        <= 9'h1FF;
      rx_sr        <= 9'd0;
      rd_data      <= 8'd0;
      ack_received <= 1'b0;
      done         <= 1'b0;
      scl_oe       <= 1'b0;  // both lines released = bus idle
      sda_oe       <= 1'b0;
    end else begin
      done <= 1'b0;

      if (state != S_IDLE)
        cnt <= (phase_end || waiting_for_scl) ? 16'd0 : cnt + 16'd1;

      case (state)
        S_IDLE: begin
          phase <= 2'd0;
          cnt   <= 16'd0;
          if (cmd_start) state <= S_START;
          else if (cmd_stop) state <= S_STOP;
          else if (cmd_write || cmd_read) begin
            tx_sr   <= cmd_write ? {wr_data, 1'b1} : {8'hFF, ~send_ack};
            bit_idx <= 4'd0;
            state   <= S_BYTE;
          end
        end

        // START (also works as repeated START when SCL is low):
        //   release SDA -> release SCL -> pull SDA (START!) -> pull SCL
        S_START: begin
          case (phase)
            2'd0: sda_oe <= 1'b0;
            2'd1: scl_oe <= 1'b0;
            2'd2: sda_oe <= 1'b1;  // SDA falls while SCL high = START
            2'd3: scl_oe <= 1'b1;
          endcase
          if (phase_end) begin
            phase <= phase + 2'd1;
            if (phase == 2'd3) begin
              state <= S_IDLE;
              done  <= 1'b1;
            end
          end
        end

        // One bit: set SDA while SCL low -> release SCL -> sample -> pull SCL
        S_BYTE: begin
          case (phase)
            2'd0: sda_oe <= ~tx_sr[8];  // 0 -> pull low, 1 -> release
            2'd1: scl_oe <= 1'b0;       // target samples on this rising edge
            2'd2: ;                     // SCL high: SDA must hold steady
            2'd3: scl_oe <= 1'b1;
          endcase
          if (phase_end) begin
            phase <= phase + 2'd1;
            if (phase == 2'd2) rx_sr <= {rx_sr[7:0], sda};  // middle of SCL high
            if (phase == 2'd3) begin
              tx_sr   <= {tx_sr[7:0], 1'b1};
              bit_idx <= bit_idx + 4'd1;
              if (bit_idx == 4'd8) begin
                rd_data      <= rx_sr[8:1];
                ack_received <= ~rx_sr[0];  // SDA low on 9th bit = ACK
                state        <= S_IDLE;
                done         <= 1'b1;
              end
            end
          end
        end

        // STOP: pull SDA (SCL low) -> release SCL -> release SDA (STOP!)
        S_STOP: begin
          case (phase)
            2'd0: sda_oe <= 1'b1;
            2'd1: scl_oe <= 1'b0;
            2'd2: sda_oe <= 1'b0;  // SDA rises while SCL high = STOP
            2'd3: ;                // bus-free time before the next START
          endcase
          if (phase_end) begin
            phase <= phase + 2'd1;
            if (phase == 2'd3) begin
              state <= S_IDLE;
              done  <= 1'b1;
            end
          end
        end
      endcase
    end
  end

endmodule