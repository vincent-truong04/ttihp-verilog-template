; I2C master: START / repeated START, address + R/W, ACK check, write and read bytes, STOP.
; Native open-drain on SDA and SCL, qualified-edge WAIT to confirm START and STOP on the bus,
; WAIT timeout against stuck or over-stretched lines.
;
; Config (SDA = P0, SCL = P1, both open-drain; a 1 releases the line):
;   OUT base = SDA (1 pin)      data and ACK bits
;   SET base = SCL (1 pin)      clock
;   side-set base = SDA (1 pin) START/STOP: the WAIT that checks the edge also makes it, so the
;                               edge always lands after the WAIT starts, at any clkdiv
;   IN base = SDA, so WAIT pin 0 = SDA and pin 1 = SCL; JMP pin = SDA
;   OUT and IN shift left; autopush at 8 (every byte on the bus, written or read, is pushed)
;   pin_init: SDA = SCL = 1 (released)
;
; Commands (TX FIFO words):
;   0x0000                STOP
;   0x4000                START (also repeated START)
;   0x8000 | d << 7 | a << 6 | e << 5
;                         byte: d = data (0xFF to read), a = bit we put in the ACK slot
;                         (1 = release: writes and the final read's NACK; 0 = ACK a read),
;                         e = 1 if a NACK in the ACK slot is an error (writes)
; Errors: IRQ 0 = unexpected NACK, IRQ 1 = timeout. After an error the rest of the
; transaction's commands are dropped up to its STOP, which still runs.
; Timeout: START loads Y = 0xFFFF; every clock-stretch WAIT in the transaction counts it down,
; so the budget is 65536 ticks per transaction (about 35 ms at clkdiv 27, like SMBus).

.side_set 1
.wrap_target
fetch:
    pull
    out x, 1                    ; [15]: 1 = byte, 0 = START/STOP
    jmp !x, control
    set x, 7
bitloop:
    out pins, 1            [3]  ; SDA = data bit while SCL is low
    set pins, 1            [3]  ; release SCL
    wait 1 pin 1 timeout        ; SCL really high: the target may stretch it
    in pins, 1             [3]  ; sample SDA while SCL is high
    set pins, 0                 ; SCL low
    jmp x--, bitloop       [2]
    out pins, 1            [3]  ; ACK slot: our bit [6]
    set pins, 1            [3]
    wait 1 pin 1 timeout
    out x, 1                    ; [5]: 1 = a NACK here is an error
    jmp pin, nack_seen          ; SDA high while SCL high: NACK
    set x, 0                    ; ACK: never an error
nack_seen:
    set pins, 0            [3]  ; SCL low
    jmp !y, timeout_err         ; this transaction's stretch budget ran out
    jmp !x, fetch               ; ACK, or a NACK we expected
nack_err:
    irq set 0
skip:                           ; drop commands up to this transaction's STOP (the all-zero word)
    pull
    mov x, osr
    jmp !x, do_stop
    jmp skip
timeout_err:
    irq set 1
    jmp skip
control:
    out x, 1                    ; [14]: 1 = START, 0 = STOP
    jmp !x, do_stop
do_start:                       ; entered with SCL low, or with the bus idle
    mov y, ~null    side 1 [3]  ; new timeout budget; release SDA
    set pins, 1            [3]  ; release SCL
    wait 1 pin 1 timeout        ; SCL high (stretched, or stuck low)
    wait 0 edge 0 if_high 1 timeout side 0  ; pull SDA low; a START is SDA falling while SCL is high
    set pins, 0            [3]  ; SCL low
    jmp !y, timeout_err
    jmp fetch
do_stop:                        ; entered with SCL low
    nop             side 0 [3]  ; SDA low
    set pins, 1            [3]  ; release SCL
    wait 1 pin 1 timeout
    wait 1 edge 0 if_high 1 timeout side 1  ; release SDA; a STOP is SDA rising while SCL is high
    jmp y--, fetch              ; Y = 0 means a WAIT timed out
    irq set 1
.wrap
