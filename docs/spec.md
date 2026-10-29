# Protocol Emulator ISA Spec — v0.1 draft

## 1. Overview

A small programmable I/O engine, in the style of the RP2040 PIO, sized for a 6x4 Tiny Tapeout tile on IHP CMOS5L (about 24K logic cells). Protocols are written as short programs and loaded after fabrication over a host SPI interface.

**Status:** v0.1 draft. Items marked **Decide** are open choices with a proposed default. Nothing here is final until the UART, SPI and I2C programs are written against it and fit.

**Goals**

- UART TX/RX, SPI master and I2C master run as programs, with no protocol-specific hardware.
- Cycle-exact pin timing, same as PIO.
- Fits comfortably in 6x4 at 60–70% utilization or less.
- Protocol-aware primitives that PIO lacks: native open-drain pins, qualified-edge WAIT, WAIT with timeout, and a hardware CRC unit.

**Non-goals for v1**

- Running four protocols at once.
- 32-bit datapaths.
- USB and CAN as committed goals (stretch only; the CRC unit keeps the door open).

**Differences from RP2040 PIO**

| Area | RP2040 PIO | This design |
| --- | --- | --- |
| State machines | 4 per block | 2 (**Decide**) |
| Register width | 32-bit | 16-bit (**Decide**) |
| Instruction memory | 32 × 16-bit, shared | 64 × 20-bit, shared (**Decide**) |
| Delay and side-set | Share 5 bits | Separate fields, plus a long `DELAY` instruction |
| Open-drain | Emulated via PINDIRS | Native per-pin mode |
| WAIT | One pin or IRQ, can hang forever | Edge qualified by a second pin, optional timeout |
| Arithmetic | `X--` only | `X--`, `Y--`, plus hardware CRC/parity |

## 2. Architecture

&#91;embedded content: core architecture · 2 state machines, shared memory\]

Two independent state machines (SMs) fetch from one shared instruction memory. Each SM has its own registers, FIFOs, counters, and CRC unit, so each runs its own program. The host loads programs and moves data over SPI.

**Per-SM registers (proposed)**

| Register | Width | Purpose |
| --- | --- | --- |
| OSR | 16 | Output shift register: bits shift out to pins |
| ISR | 16 | Input shift register: bits shift in from pins |
| X, Y | 16 each | Scratch and loop counters; Y also counts down a WAIT timeout |
| PC | 6 | Program counter into the 64-slot memory |
| CRC | 16 | Hardware CRC/parity accumulator |
| TX FIFO | 4 × 16 | Host to SM |
| RX FIFO | 4 × 16 | SM to host |
| Clock divider | 16 | Integer divider: one instruction tick every N clocks (no fraction) |
| Delay counter | 10 | Counts out per-instruction and `DELAY` waits |
| Flags | 2 | TO (last WAIT timed out), CRCZ (CRC is zero) |

Per-SM configuration (written by the host): program start address, wrap target and wrap end, pin bases and counts for OUT, SET, IN and side-set, shift direction and autopull/autopush thresholds, CRC polynomial, width and seed.

Shared between SMs: instruction memory and 4 IRQ flags for SM-to-SM and SM-to-host signalling.

## 3. Pin map

Tiny Tapeout gives 8 inputs (`ui_in`), 8 outputs (`uo_out`) and 8 bidirectional pins (`uio`). Four go to the host SPI interface; the other 20 form one protocol pin space, P0 to P19, that both SMs address by index.

| Physical pin | Index | Direction | Use |
| --- | --- | --- | --- |
| `ui_in[0]` | none | In | Host SPI SCK |
| `ui_in[1]` | none | In | Host SPI CS\_N |
| `ui_in[2]` | none | In | Host SPI MOSI |
| `uo_out[7]` | none | Out | Host SPI MISO |
| `uio[7:0]` | P0–P7 | In/out, open-drain capable | Protocol pins: I2C, bidirectional data |
| `uo_out[6:0]` | P8–P14 | Out only | Protocol outputs: TX, SCK, MOSI, CS |
| `ui_in[7:3]` | P15–P19 | In only | Protocol inputs: RX, MISO |

Each SM maps its OUT, SET, IN and side-set groups onto this space with a base index and count, as in PIO, so one program runs on any suitable pins. Pin indexes are 5 bits.

**Decided: SPI.** The host supplies the clock, CS\_N frames every transaction for free, the receiver is a synchronizer plus a shift register, and it streams FIFO data roughly 60 times faster than a 115200-baud UART. The 2 pins UART would free are not needed: two SMs use far fewer than 20 protocol pins.

## 4. Instruction format

Every instruction is 20 bits. **Decided: 20 bits.** The extra 4 bits over PIO buy a 6-bit jump address for 64 slots, 16 opcodes, 16 jump conditions, a 7-bit SET immediate, room for the qualified-edge WAIT with timeout, and separate delay and side-set fields. The cost is about 750 cells (around 3% of the budget) and 3 bytes per instruction over SPI. Revisit only if synthesis is over budget or instruction memory moves to SRAM, where 16-bit words map more cleanly.

```
 19     16 15   13 12      10 9                 0
[ opcode ][ delay ][ side-set ][     operand      ]
   4 bits   3 bits    3 bits        10 bits
```

| Field | Bits | Meaning |
| --- | --- | --- |
| opcode | \[19:16\] | 16 opcodes; 10 used, 6 reserved for later |
| delay | \[15:13\] | Extra ticks (0–7) after the instruction; use `DELAY` for longer waits |
| side-set | \[12:10\] | \[12\] enable; \[11:10\] values driven onto up to 2 side-set pins this cycle |
| operand | \[9:0\] | Instruction-specific |

**Opcode map**

| Opcode | Mnemonic | Operand \[9:0\] |
| --- | --- | --- |
| 0000 | `JMP` | cond \[9:6\], addr \[5:0\] |
| 0001 | `WAIT` | mode \[9:8\], polarity \[7\], pin A \[6:4\], pin B \[3:1\], timeout enable \[0\] |
| 0010 | `IN` | source \[9:7\], count \[4:0\] (1–16) |
| 0011 | `OUT` | dest \[9:7\], count \[4:0\] (1–16) |
| 0100 | `PUSH` / `PULL` | \[9\] 0 = push, 1 = pull; \[8\] block; \[7\] if full / if empty |
| 0101 | `MOV` | dest \[9:7\], op \[6:5\], source \[2:0\] |
| 0110 | `SET` | dest \[9:7\], value \[6:0\] (0–127) |
| 0111 | `IRQ` | \[9:8\] set / clear / wait-high / wait-low, flag \[1:0\] |
| 1000 | `CRC` | \[9\] reset to seed, \[8\] tap OUT bits, \[7\] tap IN bits, \[6\] move CRC to ISR |
| 1001 | `DELAY` | ticks \[9:0\] (0–1023) |
| 1010–1111 | reserved |  |

## 5. Instruction set

**JMP conditions** (4 bits)

| Code | Condition | Code | Condition |
| --- | --- | --- | --- |
| 0 | always | 8 | JMP pin high |
| 1 | X == 0 | 9 | JMP pin low |
| 2 | X != 0, then X-- | 10 | OSR not empty |
| 3 | Y == 0 | 11 | RX FIFO full |
| 4 | Y != 0, then Y-- | 12 | TO set (last WAIT timed out) |
| 5 | X != Y | 13 | TO clear |
| 6 | X == Y | 14 | CRCZ set (CRC is zero) |
| 7 | reserved | 15 | reserved |

**WAIT** stalls until a pin condition holds. Pins A and B are indexes relative to the SM's IN base.

| Mode \[9:8\] | Waits for | Example |
| --- | --- | --- |
| 00 | Pin A at a level | UART RX idle, `wait 0 pin 0` for a start bit |
| 01 | Edge on pin A | SPI clock edge |
| 10 | Edge on pin A while pin B is high | I2C START: SDA falls while SCL high |
| 11 | Edge on pin A while pin B is low | SPI data change only while CS low |

Polarity \[7\] picks high or rising (1) versus low or falling (0). With timeout \[0\] set, Y counts down once per tick while waiting; if it reaches zero the WAIT ends early and sets TO, which the next `JMP` can test. Without timeout, WAIT can stall forever, as in PIO.

**IN sources / OUT destinations** (3 bits)

| Code | IN source | OUT destination |
| --- | --- | --- |
| 0 | PINS | PINS |
| 1 | X | X |
| 2 | Y | Y |
| 3 | NULL (zeros) | NULL (discard) |
| 4 | ISR | PINDIRS |
| 5 | OSR | PC |
| 6 | CRC | ISR |
| 7 | reserved | reserved |

**SET destinations:** PINS, PINDIRS, X, Y. The value field is 7 bits (0–127), wider than PIO's 5 bits, so small constants like bit counts and short timeouts load in one instruction.

**MOV ops:** none, invert, bit-reverse. Sources and destinations: PINS, X, Y, ISR, OSR, CRC, STATUS.

**Decide:** whether to add `INC X` / `INC Y` in a reserved opcode, for protocols that need to count up.

## 6. Execution model

**Timing.** Each SM runs one instruction per tick. A tick is one pulse of that SM's clock divider (every N system clocks, N from 1 to 65535). At 50 MHz with N = 1, one instruction takes 20 ns.

**Delay.** After an instruction completes, the delay field holds the PC for 0–7 extra ticks. `DELAY n` holds it for n ticks (up to 1023) without doing anything else. Delay ticks do not start until a stalled instruction completes.

**Stalls.** `WAIT`, a blocking `PULL` on an empty TX FIFO and a blocking `PUSH` on a full RX FIFO freeze the PC until the condition clears. Pins keep their last value while stalled.

**Side-set.** If enabled, the side-set value is driven onto the side-set pins in the first tick of the instruction, even if the instruction then stalls. This matches PIO and lets `pull side 1` hold a line idle while waiting for data.

**Wrap.** When the PC reaches the configured wrap end it jumps to the wrap target at no cost, so loops need no `JMP`.

**Autopull and autopush.** When enabled, the OSR refills from the TX FIFO after a set number of bits have shifted out, and the ISR pushes to the RX FIFO after a set number have shifted in. Thresholds are 1–16 bits.

**Open-drain pins.** Each `uio` pin has an open-drain bit in the host config. In open-drain mode, writing 0 drives the pin low and writing 1 releases it (output enable off), so the bus pull-up takes it high. Reading the pin returns the real line level, so a program can detect another device holding it low, such as I2C clock stretching or a lost arbitration.

**Input synchronizers.** Every protocol input passes through a 2-flop synchronizer before any SM sees it, the same reason the hello-world GO input does. This adds 2 system clocks of input latency, which programs must allow for.

**CRC unit.** A bit-serial LFSR with a host-configured polynomial, width (up to 16) and seed. When tapping is enabled with `CRC`, each bit shifted by `OUT` or `IN` updates it. `CRC` can reset it or copy it into the ISR to send, and `JMP` on CRCZ checks a received frame. Covers UART parity (width 1), I2C PEC (CRC-8), CAN (CRC-15) and USB (CRC-5 and CRC-16).

**Decide:** whether reset is synchronous, as in the hello-world UART, or asynchronous using the `dfrbpq` reset pin.

## 7. Host interface (outline)

An SPI slave, mode 0, MSB first. SCK, CS\_N and MOSI are synchronized into the 50 MHz clock domain and sampled there, so host SCK must stay well below the system clock; about 1/8 of it (around 6 MHz) is a safe starting limit. Every transaction starts with a command byte.

| Command | Followed by | Does |
| --- | --- | --- |
| Read ID | 2 bytes out | Returns a fixed ID and version; first bring-up test |
| Write instruction | address, 3 bytes | Writes one 20-bit word into instruction memory |
| Read instruction | address, 3 bytes out | Reads it back to verify a load |
| Write config | register, 2 bytes | Pin bases, wrap, divider, open-drain bits, CRC setup |
| Read status | 2 bytes out | FIFO levels, IRQ flags, TO, CRCZ, SM enabled |
| Write TX FIFO | SM, 2 bytes | Push a word to an SM |
| Read RX FIFO | SM, 2 bytes out | Pop a word from an SM |
| Control | SM, 1 byte | Enable, disable, restart at start address |
| Debug | SM, 1 byte | Single-step; read PC, X, Y, OSR, ISR (stretch) |

Exact opcodes, register addresses and byte formats are still to be defined.

## 8. Area budget (rough estimate)

First-pass guesses to check that the design fits, to be replaced block by block with synthesis numbers. Reference point: the hello-world UART TX synthesized to 208 cells for 26 flip-flops, 55 of them delay cells added for hold fixing.

| Block | Flip-flops | Est. logic cells | Est. total |
| --- | --- | --- | --- |
| Instruction memory, 64 × 20 | 1,280 | \~2,500 (two read ports, one per SM) | \~3,800 |
| State machine, each | \~400 | \~2,000 | \~2,400 |
| State machines, both | \~800 | \~4,000 | \~4,800 |
| Host SPI and config registers | \~150 | \~250 | \~400 |
| Pin block (synchronizers, open-drain) | \~60 | \~60 | \~120 |
| Subtotal |  |  | \~9,100 |
| Hold-fix delay cells and buffers (+30%) |  |  | \~2,700 |
| Total |  |  | \~11,800 |

About 11,800 of roughly 24,000 cells, around half the budget, leaving room under the 60–70% ceiling.

The biggest single cost is instruction memory, and most of that is the read logic: two SMs fetching every cycle need two independent read ports. If it is over budget, the levers in order are: SRAM for instruction memory, 32 slots instead of 64, 16-bit instructions, or one SM.

## 9. Open decisions

Resolve these by writing UART TX/RX, SPI master and I2C master against this spec on paper, then in the Python model.

- [ ] Number of state machines: 2 (proposed) or 1
- [ ] Register width: 16-bit (proposed) or 8-bit
- [ ] Instruction memory: 64 slots (proposed) or 32; flip-flops or SRAM
- [x] Instruction width: 20 bits (decided)
- [ ] FIFO depth and width: 4 × 16 (proposed)
- [x] Host interface: SPI (decided)
- [ ] `INC X` / `INC Y`: add or leave out
- [ ] Reset style: synchronous or asynchronous
- [ ] WAIT timeout counter: reuse Y (proposed) or a dedicated counter
- [ ] Stretch: bit-stuffing helper for USB and CAN
- [ ] Stretch: debug commands over the host interface

**Next step:** write the I2C master program twice on paper, once in plain PIO and once in this ISA. The difference in length and clarity is the core of the novelty argument.
