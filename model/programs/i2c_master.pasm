; I2C master (spec v0.3): START / repeated START, address + R/W, ACK check, write and read bytes, STOP.
; Native open-drain on SDA and SCL, qualified-edge WAITs to confirm START and STOP on the bus, and
; WAIT timeouts against stuck or over-stretched lines.
;
; Config (SDA = P0, SCL = P1, both open-drain; a 1 releases the line):
;   side-set base = SCL (1 pin)          clock, as in PIO's i2c.pio
;   OUT base = SET base = SDA (1 pin)    data, ACK, START and STOP
;   IN base = SDA, so WAIT pin 0 = SDA and pin 1 = SCL; JMP pin = SDA
;   OUT and IN shift left; autopush at 8 (every byte on the bus, written or read, is pushed)
;   pin_init: SDA = SCL = 1 (released)
;   timeout = N: every clock-stretch and bus-check WAIT gives up after N + 1 ticks
;
; Commands (TX FIFO words):
;   0x0000                STOP
;   0x4000                START (also repeated START)
;   0x8000 | d << 7 | a << 6 | e << 5
;                         byte: d = data (0xFF to read), a = bit we put in the ACK slot
;                         (1 = release: writes and the final read's NACK; 0 = ACK a read),
;                         e = 1 if a NACK in the ACK slot is an error (writes)
;
; Errors: IRQ 0 = unexpected NACK, IRQ 1 = timeout. The SM then waits at `halt`. To recover, the host
; clears the SM's FIFOs, clears the IRQ flag and jumps the SM to `do_stop`, which sends a STOP and
; carries on with the next command.

.side_set 1
nack_seen:
    jmp to, timeout_err                 ; a timeout outranks the NACK: SCL may never have been high
    jmp osr0, fetch                     ; flag [5] = 0: we sent this NACK ourselves
nack_err:
    irq set 0
    jmp halt
timeout_err:
    irq set 1
halt:
    jmp halt                            ; wait for the host (see above)
control:
    jmp osr1, do_start                  ; [14]: 1 = START, 0 = STOP
do_stop:                                ; entered with SCL low
    set pins, 0                 [3]     ; SDA low
    wait 1 pin 1 timeout side 1 [3]     ; release SCL; wait until it is really high
    set pins, 1                         ; release SDA ...
    wait 1 edge 0 if_high 1 timeout     ; ... and see the STOP on the bus: SDA rises while SCL is high
    jmp done
do_start:                               ; entered with SCL low, or with the bus idle
    set pins, 1                 [3]     ; release SDA
    wait 1 pin 1 timeout side 1 [3]     ; release SCL; wait until it is really high
    set pins, 0                         ; pull SDA low ...
    wait 0 edge 0 if_high 1 timeout     ; ... and see the START on the bus: SDA falls while SCL is high
    jmp done             side 0 [3]     ; SCL low
.wrap_target
fetch:
    pull
    jmp osr0, control                   ; [15]: 0 = START/STOP, 1 = byte
    set x, 7
bitloop:
    out pins, 1                 [3]     ; SDA = data bit while SCL is low
    wait 1 pin 1 timeout side 1 [3]     ; release SCL; wait until it is really high (stretching)
    jmp to, timeout_err                 ; stop at once: clocking on would hand the target a corrupt byte
    in pins, 1                  [3]     ; sample SDA while SCL is high
    jmp x--, bitloop     side 0 [3]     ; SCL low
    out pins, 1                 [3]     ; ACK slot: our bit [6]
    wait 1 pin 1 timeout side 1 [3]
    jmp pin, nack_seen   side 0 [3]     ; SDA was high while SCL was high: NACK. SCL low
done:
    jmp to, timeout_err                 ; did any WAIT in this command time out? (TO is sticky)
.wrap
