/*
 * spi_slave.v - SPI mode 0 slave, oversampled by the system clock
 *
 * Moves BYTES only. Knows nothing about commands (that's host_cmd.v).
 * SCK, CS_N and MOSI are treated as data and sampled with clk.
 * SCK must stay at or below ~clk/8.
 *
 * Mode 0: SCK idles low, MOSI/MISO sampled on SCK rising, changed on falling.
 * MSB first.
 */
`default_nettype none

module spi_slave (
    input  wire       clk,
    input  wire       rst_n,

    // Raw SPI pins (asynchronous to clk)
    input  wire       sck,
    input  wire       cs_n,
    input  wire       mosi,
    output wire       miso,

    // Byte interface to host_cmd
    output reg  [7:0] rx_byte,    // last complete byte received
    output reg        rx_valid,   // 1-cycle pulse when rx_byte is new
    input  wire [7:0] tx_byte,    // next byte to send to the host
    input  wire       tx_load,    // 1-cycle pulse: latch tx_byte

    // Framing
    output wire       cs_active,  // high while a transaction is in progress
    output wire       cs_start    // 1-cycle pulse when a transaction begins
);

  // ------------------------------------------------------------------
  // 1. Synchronizers: 2 flops each for sck, cs_n, mosi,
  //    plus one more stage on sck and cs_n for edge detection.
  // ------------------------------------------------------------------
  // TODO
  reg [2:0] sck_sync;
  reg [2:0] cs_n_sync;
  reg [1:0] mosi_sync;

  always @(posedge clk) begin
    if (!rst_n) begin
        sck_sync <= 3'b000;
        cs_n_sync <= 3'b111;
        mosi_sync <= 2'b00;          
    end else begin
        sck_sync <= {sck_sync[1:0], sck};
        cs_n_sync <= {cs_n_sync[1:0], cs_n};
        mosi_sync <= {mosi_sync[0], mosi};
    end    
  end


  // ------------------------------------------------------------------
  // 2. Edge detection
  //    sck_rise, sck_fall, cs_fall (transaction start)
  // ------------------------------------------------------------------
  wire sck_now   = sck_sync[1];    
  wire sck_prev  = sck_sync[2];    
  wire cs_n_now  = cs_n_sync[1];
  wire cs_n_prev = cs_n_sync[2];
  wire mosi_s    = mosi_sync[1];   

  wire sck_rise = (~sck_prev & sck_now);          
  wire sck_fall = (sck_prev & ~sck_now);        

  assign cs_active = ~cs_n_now;      
  assign cs_start  = (cs_n_prev & ~cs_n_now);       


  // ------------------------------------------------------------------
  // 3. Receive: shift MOSI in on sck_rise, MSB first.
  //    After 8 bits: update rx_byte, pulse rx_valid for one cycle.
  //    Reset the bit counter whenever CS is inactive.
  // ------------------------------------------------------------------
  reg [7:0] rx_shift;
  reg [2:0] bit_cnt;

  always @(posedge clk) begin
    rx_valid <= 1'b0;                 
    if (!rst_n) begin
      rx_shift <= 8'd0;
      bit_cnt  <= 3'd0;
      rx_byte  <= 8'd0;
    end else if (!cs_active) begin
      bit_cnt  <= 3'b0;             
    end else if (sck_rise) begin
      rx_shift <= {rx_shift[6:0], mosi_s};          
      bit_cnt  <= bit_cnt + 1'b1;          
      if (bit_cnt == 3'b111) begin    
        rx_byte  <= {rx_shift[6:0], mosi_s};        
        rx_valid <= 1'b1;
      end
    end
  end



  // ------------------------------------------------------------------
  // 4. Transmit: tx_load latches tx_byte into a holding register.
  //    MISO = MSB of a TX shift register.
  //    - At transaction start, load the shift register from the holding
  //      register so the first MSB is ready before the first sck_rise.
  //    - On sck_fall, shift to the next bit.
  //    - On the sck_fall that ends a byte, load the next byte from the
  //      holding register.
  //    - If nothing was loaded, send 0x00.
  // ------------------------------------------------------------------
  reg [7:0] tx_hold;
  reg [7:0] tx_shift;

  always @(posedge clk) begin
    if (!rst_n) begin
      tx_hold  <= 8'd0;
      tx_shift <= 8'd0;
    end else begin
      // --- Use the waiting byte ---
      if (cs_start) begin
        tx_shift <= tx_hold;              // F: first byte, ready before the first rising edge
        tx_hold  <= 8'd0;                // used up → defaults to 0x00
      end else if (cs_active && sck_fall) begin
        if (bit_cnt == 3'd0) begin     // G: a byte just finished
          tx_shift <= tx_hold;            
          tx_hold  <= 8'd0;
        end else begin
          tx_shift <= {tx_shift[6:0], 1'b0};            
        end
      end

      // --- host_cmd hands over a new byte (written LAST so it wins) ---
      if (tx_load)
        tx_hold <= tx_byte;            
    end
  end

  assign miso = cs_active & tx_shift[7];

endmodule