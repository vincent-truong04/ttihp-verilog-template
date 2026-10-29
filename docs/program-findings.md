# Program findings: UART RX, SPI and I2C master on ISA v0.2

Three protocol programs were written for the v0.2 ISA and tested on the Python model against simulated
devices (`model/programs/`, `model/devices.py`, `model/tests/`). This report compares them with the
official PIO examples and lists what they showed about the ISA. The spec was not changed; the
suggestions below are proposals.

## (a) Instruction counts against pico-examples

Counted from the pico-examples files at commit `0d62f75`.

| Protocol | Ours | PIO example | PIO count |
|---|---|---|---|
| UART RX | **9** | `uart_rx` (with framing check) | 9 (`uart_rx_mini`, no framing check: 4) |
| SPI mode 0 | **2** | `spi_cpha0` | 2 |
| I2C master | **41** | `i2c` | 18 |

The I2C gap comes from what the two programs do:

- **PIO's version leans on the CPU.** START, STOP and the repeated start are 4 `set_scl_sda` instructions
  that the SDK pushes through the FIFO and runs with OUT EXEC, so they take no memory slots. On a NACK,
  PIO's program halts with `irq wait` and the CPU recovers.
- **Ours is self-contained.** It handles START/STOP (13 instructions), error recovery (7), timeouts, and
  checks that START/STOP actually appear on the bus. PIO has no timeouts at all.

The 41 instructions split like this:

| Part | Ours | PIO |
|---|---|---|
| Dispatch | 5 | 6 |
| Bit loop | 6 | 5 |
| ACK handling | 9 | 4 |
| Errors and skip | 7 | 2 |
| START | 7 | 0 |
| STOP | 6 | 0 |
| Bit-counter `set` | 1 | 1 |

## (b) Slots if everything is loaded together

The three new programs use **52 of 64** slots. With `uart_tx` it's **56 of 64**, leaving 8 free. That's
tested, not just added up (`test_programs_together.py`): all four are loaded with relocated JMPs, then
UART TX→RX loopback runs on the two SMs, and the SPI and I2C masters run at the same time.

## (c) Where the ISA was awkward, and suggested changes

1. **An edge WAIT can miss an edge its own program caused.** A pin write takes 3 clocks to come back to
   the SM (output register, then the 2-flop synchronizer). If `clkdiv` ≥ 3, that edge lands on or before
   the WAIT's first tick, and v0.2 doesn't count it. So "`set pins, 1` then wait for the STOP edge" fails
   at any realistic I2C `clkdiv`.
   - **Workaround used:** put SDA on side-set, so the WAIT makes the edge itself. That forces SCL onto SET,
     which costs +1 instruction per bit.
   - **Proposal:** arm the edge detector when the *preceding* instruction completes, not at the WAIT's
     first tick. SCL could then go back on side-set as in PIO.
2. **The timeout shares Y, and TO clears at every timeout WAIT.** The I2C master needs `mov y, ~null`, a
   sticky-budget trick (`jmp !y` once per byte, since TO can't be checked once per byte), and 3 explicit
   timeout checks.
   - **Proposal:** a dedicated timeout counter that reloads from a per-SM config register on each timeout
     WAIT.
   - **Proposal:** make TO sticky until a `JMP to/!to` reads it.
   - **Optional:** a per-SM timeout vector that a timeout jumps to.
3. **A flag bit can't be tested without spending an extra instruction to consume it.** `out x, 1` +
   `jmp !x` appears 3 times.
   - **Proposal:** use the reserved JMP conditions 7 and 15 for "shift 1 bit out of the OSR and jump if it
     was 1 / if it was 0".
4. **There's no EXEC, and the host can't redirect an SM or flush its FIFOs.** Error recovery needed a
   4-instruction skip loop and a convention that STOP is the word 0x0000.
   - **Proposal:** a host Control "jump to address" command and a host "clear FIFOs" command. Both are
     cheap; full EXEC would cost far more.
5. **The 3-bit delay field.** `uart_rx` needed `[10]`; it fit only because the WAIT could carry 3 ticks.
   - **Proposal:** when side-set enable [12] = 0, use [11:10] as 2 more delay bits, giving 0–31.
6. **Bytes sit at the top of 16-bit FIFO words.** UART RX bytes arrive in [15:8], and SPI TX bytes must be
   written to [15:8]. PIO hides this with RP2040's byte-lane tricks.
   - **Proposal:** 1-byte host FIFO commands that justify the byte by shift direction. That also halves SPI
     host traffic.
7. **The synchronizer caps full-duplex SPI at SCK = 6.25 MHz** (`clkdiv` ≥ 2). At `clkdiv` 1 every MISO bit
   arrives one bit late (`test_clkdiv_1_is_too_fast_for_the_miso_round_trip`).
   - **Proposal:** a per-pin synchronizer bypass for synchronous inputs, like PIO's INPUT_SYNC_BYPASS.
8. **Reset-low TX vs. enable order.** RX enabled on the same clock as TX catches the reset-low level as a
   start bit, because the pin init reaches the pad one edge after enable. This follows from the decided
   reset state, so the fix is sequencing: enable TX first.
   - **Proposal:** add a sequencing note to spec section 6.
9. **The tools make program-computed jumps impractical.** SET values can't be labels, and nothing relocates
   OUT PC targets, so OUT PC dispatch, which would have saved 3 instructions, wasn't used. pioasm has
   expressions.
   - **Proposal:** add expressions to the assembler (tooling only).

**Not ISA problems, but worth knowing:**

- **UART RX tolerates −2.5% / +5% baud on a continuous stream.** −2% was measured passing and −3% failing.
  Each frame takes 78 of its 80 ticks to process, so a fast sender gains on the SM frame by frame. PIO's
  `uart_rx` has exactly the same timing.
- **The I2C master pushes a read-back of every byte, writes included.** The host must drain the RX FIFO or
  the master stalls with SCL high. PIO does the same.

With items 1–4, the I2C master is estimated to drop from 41 to roughly 30 instructions. That's an
estimate, not built.

## (d) What this says about the open decisions

- **SM count: keep 2.** Full-duplex UART alone needs both SMs (TX + RX), and SPI and I2C ran together fine.
  One SM would rule out full-duplex UART.
- **Register width: keep 16-bit.** SPI and UART need only 8. But the I2C command words use 11 bits, and the
  timeout budget needs the range: 65,536 ticks is about 35 ms at `clkdiv` 27, which covers SMBus's 25–35 ms.
  An 8-bit Y would cap that at about 0.14 ms.
- **Slot count: keep 64.** I2C alone (41) doesn't fit in 32. Even at about 30 instructions, the three
  programs would total about 41, still over 32.
- **INC X / INC Y: leave it out.** None of the programs needed to count up.
- **WAIT timeout counter: evidence favors a dedicated counter** (item 2).
- **20-bit instructions:** separate side-set and delay fields were used together often, e.g.
  `mov y, ~null side 1 [3]`.
