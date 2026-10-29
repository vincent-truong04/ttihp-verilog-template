; 8N1 UART transmitter, 8 ticks per bit.
;
; Config: OUT base = side-set base = TX pin, out_count = 1, sideset_count = 1, shift right (LSB first),
; pin_init bit for TX = 1 so the line idles high from enable.
; Baud = f_clk / (8 * clkdiv). At 50 MHz, clkdiv 54 gives 115741 baud (+0.47% from 115200).
; Push one byte per TX FIFO word (low 8 bits).

.side_set 1
.wrap_target
    pull            side 1 [7]  ; stop bit / idle high; stalls here (still high) until a byte arrives
    set x, 7        side 0 [7]  ; start bit, 8 ticks
bitloop:
    out pins, 1                 ; data bit, LSB first
    jmp x--, bitloop       [6]  ; 1 + 1 + 6 = 8 ticks per bit
.wrap
