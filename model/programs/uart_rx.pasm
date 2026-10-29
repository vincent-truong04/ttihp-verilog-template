; 8N1 UART receiver, 8 ticks per bit, mid-bit sampling, framing check.
;
; Config: IN base = JMP pin = RX pin, shift right (LSB first), autopush off.
; Baud = f_clk / (8 * clkdiv), as for uart_tx.pasm.
; Each good byte is pushed to the RX FIFO in bits [15:8]; the host's 1-byte RX read returns it.
; A bad stop bit (framing error or break) raises IRQ flag 0 and the byte is dropped.

.wrap_target
start:
    wait 0 pin 0                ; start bit (level: still works if we arrive late)
    set x, 7               [10] ; 1 + 11 = 12 ticks: the middle of data bit 0 (5-bit delay, no side-set)
bitloop:
    in pins, 1                  ; sample at mid-bit, LSB first
    jmp x--, bitloop       [6]  ; 8 ticks per bit
    jmp pin, good_stop          ; middle of the stop bit: must be high
    irq set 0                   ; framing error or break: tell the host
    wait 1 pin 0                ; wait for the line to go idle again
    jmp start
good_stop:
    push                        ; no delay: a little slack for a fast sender
.wrap
