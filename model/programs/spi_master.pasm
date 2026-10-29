; SPI master, mode 0 (CPOL 0, CPHA 0), 8-bit frames, MSB first, full duplex.
;
; Config: side-set base = SCK (1 pin), OUT base = MOSI (1 pin), IN base = MISO.
;         OUT and IN shift left; autopull and autopush at a threshold of 8.
;         pin_init: SCK = 0.
; Host writes each TX byte in bits [15:8] of a FIFO word (shift left takes the top bits first);
; each received byte arrives in bits [7:0].
; SCK = f_clk / (4 * clkdiv). clkdiv must be at least 2: see the latency note in the test.
; CS is not handled here: drive it from another pin or SM, as with PIO's spi_cpha0.

.side_set 1
.wrap_target
    out pins, 1     side 0 [1]  ; MOSI changes while SCK is low; stalls here (SCK low) when the FIFO is empty
    in pins, 1      side 1 [1]  ; SCK rises; sample MISO
.wrap
