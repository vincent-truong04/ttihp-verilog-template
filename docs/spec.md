# Protocol Emulator ISA Spec — v0.2 draft

## 1. Overview

A small programmable I/O engine, in the style of the RP2040 PIO, sized for a 6x4 Tiny Tapeout tile on IHP CMOS5L (about 24K logic cells). Protocols are written as short programs and loaded after fabrication over a host SPI interface.

**Status:** v0.2 draft. v0.2 resolves every ambiguity found while building the Python reference model (`model/`). Items marked **Decide** are open choices with a proposed default. Nothing here is final until the UART, SPI and I2C programs are written against it and fit.

**Convention:** where this spec is silent and RP2040 PIO defines the behavior, PIO's behavior applies. "PIO §x" refers to a section of the RP2040 datasheet, chapter 3.

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
| Two SMs writing one pin on the same cycle | Highest-numbered SM wins | Lowest-numbered SM wins |
| Pin reset level | Configured per GPIO by software | Per-SM pin init value, applied on enable |

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
| Shift counters | 5 each | Bits shifted out of the OSR and into the ISR, 0–16, saturating (PIO §3.2.3.3) |
| TO flag | 1 | Set when a WAIT timed out (section 5) |
| CRC tap enables | 2 | Tap OUT bits, tap IN bits (section 5, `CRC`) |

CRCZ ("CRC is zero") is not stored: it is the comparison CRC == 0 over the configured width.

**Per-SM configuration** (written by the host):

- Program start address, wrap target and wrap end
- Clock divider N
- Pin bases and counts for OUT, SET, IN and side-set; JMP pin (an absolute pin index, PIO §3.4.2)
- OUT shift direction and IN shift direction, set separately (PIO SHIFTCTRL, §3.7)
- Autopull enable and threshold; autopush enable and threshold (1–16; 16 is written as 0, as PIO writes 32 as 0)
- STATUS select (TX or RX FIFO) and level N for `MOV x, STATUS` (PIO EXECCTRL_STATUS_SEL/STATUS_N, §3.4.8)
- CRC polynomial, width (1–16) and seed
- Pin init value (section 3)

**Shared between SMs:** instruction memory, 4 IRQ flags for SM-to-SM and SM-to-host signalling, and the open-drain mode bit for each `uio` pin.

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

Each SM maps its OUT, SET, IN and side-set groups onto this space with a base index and count, as in PIO, so one program runs on any suitable pins.

**Pin indexes** are 5 bits. As in PIO, a group starting near the top wraps modulo 32, and indexes that have no pin behave like PIO's missing GPIOs (PIO §3.5.6.2): P20–P31 read as 0 and writes to them are ignored. Writes to the input-only pins P15–P19 are ignored too.

**Reading out-only pins.** P8–P14 have no input buffer, so reading one returns the value the chip is driving, through the same 2-flop synchronizer as every other input.

**Reset state.** At reset, `uo_out` (P8–P14) drives 0, and every `uio` pin (P0–P7) is released: output enable off, open-drain mode off. The `uio` output values reset to 1, so a pin is also released if the host later turns on open-drain mode before any program writes it.

**Pin init value.** Each SM has a 20-bit pin init value. When the host enables the SM, the value is written to the output levels of every pin in that SM's OUT, SET and side-set groups. This is how a UART TX line starts idle-high before the program runs. It applies only to the SM's own groups, it sets levels and not directions, and a restart does not reapply it.

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

**Opcode map.** Bits shown as `0` are unused and must be 0. The assembler enforces this; hardware behavior with them set is undefined, and the reference model stops with an error. Reserved codes are undefined the same way.

| Opcode | Mnemonic | Operand \[9:0\] |
| --- | --- | --- |
| 0000 | `JMP` | cond \[9:6\], addr \[5:0\] |
| 0001 | `WAIT` | mode \[9:8\], polarity \[7\], pin A \[6:4\], pin B \[3:1\] (0 in modes 00 and 01), timeout enable \[0\] |
| 0010 | `IN` | source \[9:7\], `0` \[6:4\], count \[3:0\] (1–15; 16 is encoded as 0) |
| 0011 | `OUT` | dest \[9:7\], `0` \[6:4\], count \[3:0\] (1–15; 16 is encoded as 0) |
| 0100 | `PUSH` / `PULL` | \[9\] 0 = push, 1 = pull; \[8\] block; \[7\] if full / if empty; `0` \[6:0\] |
| 0101 | `MOV` | dest \[9:7\], op \[6:5\], `0` \[4:3\], source \[2:0\] |
| 0110 | `SET` | dest \[9:7\], value \[6:0\] (0–127) |
| 0111 | `IRQ` | op \[9:8\] (set / clear / wait-high / wait-low), `0` \[7:2\], flag \[1:0\] |
| 1000 | `CRC` | \[9\] reset to seed, \[8\] tap OUT bits, \[7\] tap IN bits, `0` \[6:0\] |
| 1001 | `DELAY` | ticks \[9:0\] (0–1023) |
| 1010–1111 | reserved |  |

The 16-as-0 count encoding follows PIO, which encodes a count of 32 as 0 (PIO §3.4.4).

## 5. Instruction set

**JMP** sets PC to addr if the condition holds. Delay cycles apply whether or not the jump is taken (PIO §3.4.2).

| Code | Condition | Code | Condition |
| --- | --- | --- | --- |
| 0 | always | 8 | JMP pin high |
| 1 | X == 0 | 9 | JMP pin low |
| 2 | X != 0, then X-- | 10 | OSR not empty |
| 3 | Y == 0 | 11 | RX FIFO full |
| 4 | Y != 0, then Y-- | 12 | TO set (last timeout WAIT timed out) |
| 5 | X != Y | 13 | TO clear |
| 6 | X == Y | 14 | CRCZ set (CRC is zero) |
| 7 | reserved | 15 | reserved |

- **X--/Y--:** the register always decrements, whether or not the jump is taken; the condition tests the value before the decrement (PIO §3.4.2). A loop that starts with X = n runs n + 1 times and leaves X = 0xFFFF.
- **JMP pin** is the absolute pin index in the SM's JMP pin config (PIO EXECCTRL_JMP_PIN, §3.4.2).
- **OSR not empty** means the output shift count is below the pull threshold, whether or not autopull is on (PIO `!OSRE`, §3.4.2).

**WAIT** stalls until a pin condition holds. Pins A and B are indexes added to the SM's IN base, modulo 32 (PIO §3.4.3).

| Mode \[9:8\] | Waits for | Example |
| --- | --- | --- |
| 00 | Pin A at a level | UART RX idle, `wait 0 pin 0` for a start bit |
| 01 | Edge on pin A | SPI clock edge |
| 10 | Edge on pin A while pin B is high | I2C START: SDA falls while SCL high |
| 11 | Edge on pin A while pin B is low | SPI data change only while CS low |

Polarity \[7\] picks high or rising (1) versus low or falling (0).

- **Level mode** samples pin A on each tick, as in PIO.
- **Edge modes** use an edge detector that runs on every system clock while the WAIT is active, whatever the clock divider. On the WAIT's first tick the detector records pin A's synchronized level. On each later system clock it compares pin A with its level one clock earlier. For modes 10 and 11, pin B is sampled on the same clock as the edge. The first qualifying edge is latched, and the WAIT completes on the first tick at or after it. Edges before the WAIT's first tick do not count, and neither does an edge on the first tick's own clock. A WAIT can complete on the same clock as its edge. Pulses shorter than a tick are still caught.
- **Timeout.** With timeout \[0\] set, TO is cleared when the WAIT starts. Then, on each tick: if the condition is met, the WAIT completes; otherwise, if Y == 0, it completes and sets TO; otherwise Y decrements and the WAIT stays stalled. So Y = n gives up to n + 1 ticks, and Y = 0 checks the condition once. A WAIT without timeout never changes TO or Y. It can stall forever, as in PIO.

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

IN and OUT follow PIO §3.4.4 and §3.4.5, at 16 bits:

- **IN** shifts the ISR by count in the configured direction, then copies the low count bits of the source into the gap. Bit order does not depend on the shift direction. The input shift count increases by count, saturating at 16.
- **OUT** takes count bits from the OSR: the low bits when shifting right, the high bits when shifting left. The output shift count increases by count, saturating at 16.
- OUT ISR also sets the input shift count to count, and OUT PC is an unconditional jump.

**PUSH / PULL** (PIO §3.4.6, §3.4.7):

- **PUSH** moves the ISR into the RX FIFO and clears the ISR and its shift count. With block set, it stalls while the RX FIFO is full. Without block, a PUSH to a full FIFO drops the word but still clears the ISR. With if full, it does nothing unless the input shift count has reached the push threshold.
- **PULL** loads the OSR from the TX FIFO and clears the output shift count. With block set, it stalls while the TX FIFO is empty. Without block, a PULL from an empty FIFO copies X into the OSR. With if empty, it does nothing unless the output shift count has reached the pull threshold. With autopull enabled, PULL is a no-op while the OSR is full, meaning the output shift count is 0 (PIO §3.5.4.2).

**MOV** copies source to destination through an op: 0 none, 1 invert, 2 bit-reverse (bit n to bit 15 − n), 3 reserved. Register codes follow PIO §3.4.8 where PIO has the register, and CRC takes a code PIO leaves unused.

| Code | MOV source | MOV destination |
| --- | --- | --- |
| 0 | PINS (IN mapping, 16 bits) | PINS (OUT mapping, OUT count bits) |
| 1 | X | X |
| 2 | Y | Y |
| 3 | NULL (zeros) | reserved |
| 4 | CRC | CRC (masked to the CRC width) |
| 5 | STATUS | reserved |
| 6 | ISR | ISR (input shift count set to 0) |
| 7 | OSR | OSR (output shift count set to 0) |

STATUS is all ones if the selected FIFO's level (TX or RX, from the STATUS select config) is below N, and all zeros otherwise (PIO §3.4.8).

**SET** writes a 7-bit immediate. Destinations use PIO's codes (PIO §3.4.10): 0 PINS, 1 X, 2 Y, 4 PINDIRS; others are reserved. SET PINS and SET PINDIRS write the SET pin group: up to 7 pins, one per value bit.

**IRQ** works on the 4 shared flags:

- Set raises a flag; clear lowers it.
- Wait-high stalls until the flag is 1, then clears it, like PIO's `WAIT 1 IRQ` (PIO §3.4.3).
- Wait-low stalls until the flag is 0.
- A flag written on one clock is visible to both SMs on the next.

**CRC** updates the CRC unit (section 6):

- Bit \[9\] reloads the seed.
- Bits \[8\] and \[7\] set the tap-OUT and tap-IN enables. These stay in effect until the next `CRC` instruction, so `crc reset` alone also turns tapping off.
- To read the result, use `MOV isr, crc` or `MOV osr, crc` (MOV codes above), or `IN crc, n`.

**DELAY n** takes 1 tick plus n extra ticks, like `nop [n]`, so `DELAY 0` is a 1-tick no-op. Its own delay field adds on top. n plus the delay field must fit the 10-bit delay counter (≤ 1023); the assembler enforces this.

**Decide:** whether to add `INC X` / `INC Y` in a reserved opcode, for protocols that need to count up.

### 5.1 Assembly syntax

The syntax follows pioasm (PIO §3.3), with additions for the new instructions:

```
.side_set <n>                 ; side-set pin count, 0-2 (default 2); checks `side` values fit
.wrap_target                  ; wrap target = next instruction
.wrap                         ; wrap end = previous instruction
label:
    <instr> [side <v>] [[<delay>]]

jmp [<cond>,] <label|addr>    ; cond: !x x-- !y y-- x!=y x==y pin !pin !osre rxfull to !to crcz
wait <pol> pin <a> [timeout]
wait <pol> edge <a> [if_high <b> | if_low <b>] [timeout]
in <src>, <1-16>              ; src: pins x y null isr osr crc
out <dst>, <1-16>             ; dst: pins x y null pindirs pc isr
push [iffull] [block|noblock]
pull [ifempty] [block|noblock]
mov <dst>, [~|!|::]<src>      ; ~ or ! invert, :: bit-reverse; src adds null and status
set <dst>, <0-127>            ; dst: pins pindirs x y
irq set|clear|wait_high|wait_low <0-3>
crc [reset] [tap_out] [tap_in]
delay <0-1023>
nop                           ; = mov y, y
```

Comments start with `;` or `//`. Numbers can be decimal, `0x` hex or `0b` binary. As in pioasm, `side` goes before the `[delay]`, and every instruction has a side-set enable bit, so `side` is always optional.

## 6. Execution model

**Timing.** Each SM runs one instruction per tick. A tick is one pulse of that SM's clock divider, every N system clocks. N is 1 to 65536, and 65536 is written as 0 (PIO §3.5.5). The divider counts down from N and pulses when it reaches 1, so the first tick comes N system clocks after the SM is enabled (PIO §3.5.5). At 50 MHz with N = 1, one instruction takes 20 ns.

**Delay.** After an instruction completes, the delay field holds the PC for 0–7 extra ticks. `DELAY n` holds it for longer (section 5). Delay ticks do not start until a stalled instruction completes (PIO §3.2.4).

**Stalls.** A stalled instruction repeats on the next tick with the PC unchanged, and pins keep their last value (PIO §3.2.4). These instructions stall:

- `WAIT` until its condition holds (or it times out)
- a blocking `PULL` on an empty TX FIFO
- a blocking `PUSH` on a full RX FIFO
- `IRQ` wait-high and wait-low until the flag matches
- an `OUT` with autopull enabled when the OSR has reached its threshold
- an `IN` with autopush enabled whose push would find the RX FIFO full

**Side-set.** If enabled, the side-set value is driven onto the side-set pins on the first tick of the instruction, even if the instruction then stalls (PIO §3.2.4, §3.5.1). This lets `pull side 1` hold a line idle while waiting for data. If side-set and an OUT, SET or MOV to PINS write the same pin on the same tick, side-set wins (PIO §3.5.1).

**Wrap.** After an instruction completes, the next PC is chosen in this order (PIO §3.5.2):

1. the jump target, if the instruction is a jump (JMP with its condition true, or OUT PC);
2. otherwise the wrap target, if PC equals the wrap end;
3. otherwise PC + 1, wrapping from 63 to 0.

Wrapping costs no extra tick.

**Shift counters.** These follow PIO §3.2.3.3. Reset and restart set the input count to 0 and the output count to 16 (OSR empty). A PULL or autopull clears the output count; a PUSH or autopush clears the input count; MOV to OSR or ISR clears that register's count.

**Autopull** follows PIO §3.5.4.2 exactly:

- On an OUT tick, if the output count has reached the threshold, the OSR refills from the TX FIFO if it has data, and the OUT stalls this tick either way. So an OUT that finds the OSR empty takes at least 2 ticks. Otherwise the OUT shifts its bits, and if the count has now reached the threshold and the FIFO has data, the OSR refills on the same tick.
- An OUT that crosses the threshold still shifts all its bits. Once the threshold is reached, any OSR bits beyond it are already zeros.
- On every other tick, including delay ticks and ticks of other stalled instructions, the OSR refills if it has reached the threshold and the FIFO has data. This is checked after that tick's instruction has run.

**Autopush** follows PIO §3.5.4.1. After an IN shifts, if the input count has reached the threshold, the ISR is pushed to the RX FIFO and cleared on the same tick. If the RX FIFO is full, the IN stalls, and it does not shift until the push can happen, so no bits are lost or shifted twice. (PIO's pseudocode doesn't say.)

**Pin writes from two SMs.** Pin levels and pin directions are prioritized separately, as in PIO §3.5.6.1. If both SMs write the same pin's level (or direction) on the same clock, the **lower-numbered** SM wins. This is the reverse of PIO, where the highest-numbered SM wins. A pin nobody writes keeps its value. IRQ flags follow the same rule, so if SM0 and SM1 write one flag on the same clock, SM0's write wins.

**Open-drain pins.** Each `uio` pin has an open-drain bit in the host config. In open-drain mode, writing 0 drives the pin low and writing 1 releases it (output enable off), so the bus pull-up takes it high. PINDIRS has no effect on a pin in open-drain mode. Reading the pin returns the real line level, so a program can detect another device holding it low, such as I2C clock stretching or a lost arbitration.

**Input synchronizers.** Every protocol input passes through a 2-flop synchronizer before any SM sees it (PIO §3.5.6.3), the same reason the hello-world GO input does. This adds 2 system clocks of input latency, which programs must allow for. A pad change between clock edges k−1 and k is captured at edge k, and an SM can first act on it at edge k + 2.

**Output register.** **Decided:** every protocol output level and output enable (P0–P14) is driven straight from a flip-flop, so pins change only on clock edges and never glitch. The flip-flop registers the final value, after SM priority, side-set and open-drain logic. A pin written by an instruction at edge k changes right after edge k + 1. Host changes that affect pins, such as open-drain bits or the pin init value written on enable, also reach the pads one clock later. An SM reading its own output sees the change 4 clocks after writing it, the same as PIO with synchronizers (PIO §3.5.6.1). There is no extra stage on inputs.

**CRC unit.** A bit-serial LFSR with a host-configured polynomial, width (1–16) and seed. It is MSB-first and non-reflected, with no final XOR. For each bit b:

```
feedback = CRC[width-1] XOR b
CRC      = (CRC << 1) masked to width
if feedback: CRC = CRC XOR poly
```

- **Tapping.** While a tap enable is set, every 1-bit OUT or IN feeds its bit to the CRC, whatever the source or destination, including NULL. Programs that shift NULL bits that must not enter the CRC, such as padding or alignment, should turn the taps off first with a `CRC` instruction that leaves them clear. **In v1 only 1-bit shifts update the CRC.** An OUT or IN of 2 or more bits leaves the CRC unchanged, and the assembler warns about such shifts in any program that turns tapping on.
- **Checking.** `JMP crcz` tests a received frame, and `CRC reset` reloads the seed.
- **Reflected CRCs** (LSB-first, such as USB) use a right-shifting OUT or IN to feed bits LSB-first, then `MOV ::` to bit-reverse the result. A w-bit result lands in the top w bits of the destination. A final XOR, if needed, is `MOV ~`.
- **Coverage.** The unit covers UART parity (width 1, poly 1), I2C PEC (CRC-8), CAN (CRC-15) and USB (CRC-5 and CRC-16).

**Enable, restart and reset.**

- **Enable** starts the SM's clock divider and writes its pin init value (section 3). The divider starts on enable, unlike PIO's free-running dividers. To run both SMs in lockstep, enable them with one Enable mask command: SMs that go from disabled to enabled in the same command start their dividers on the same clock, so SMs with the same N tick on the same clocks. This replaces PIO's CLKDIV_RESTART (PIO §3.7, CTRL).
- **Restart** clears what PIO's SM_RESTART clears (PIO §3.7, CTRL): the shift counters (input 0, output 16), the ISR contents, the delay counter and any stalled instruction. It then sets PC to the start address. As in PIO, OSR, X and Y are kept. Restart also clears TO, reloads the CRC seed, turns both taps off and disarms the WAIT edge detector. It leaves the FIFOs and pin levels alone.
- **Reset:** everything is cleared, with shift counts as on restart, PC 0, and pins as in section 3.

**Decide:** whether reset is synchronous, as in the hello-world UART, or asynchronous using the `dfrbpq` reset pin.

## 7. Host interface (outline)

An SPI slave, mode 0, MSB first. SCK, CS\_N and MOSI are synchronized into the 50 MHz clock domain and sampled there, so host SCK must stay well below the system clock; about 1/8 of it (around 6 MHz) is a safe starting limit. Every transaction starts with a command byte.

| Command | Followed by | Does |
| --- | --- | --- |
| Read ID | 2 bytes out | Returns a fixed ID and version; first bring-up test |
| Write instruction | address, 3 bytes | Writes one 20-bit word into instruction memory |
| Read instruction | address, 3 bytes out | Reads it back to verify a load |
| Write config | register, 2 bytes | Pin bases, wrap, divider, open-drain bits, CRC setup, pin init value |
| Read status | 2 bytes out | FIFO levels, IRQ flags, TO, CRCZ, SM enabled |
| Write IRQ | 1 byte | Clear flags (write 1 to clear) or force them set, like PIO's IRQ and IRQ_FORCE registers (PIO §3.7) |
| Write TX FIFO | SM, 2 bytes | Push a word to an SM |
| Read RX FIFO | SM, 2 bytes out | Pop a word from an SM |
| Control | SM, 1 byte | Enable, disable, restart (section 6) |
| Enable mask | 1 byte | Writes both SMs' enable bits at once; SMs it enables start together (section 6) |
| Debug | SM, 1 byte | Single-step; read PC, X, Y, OSR, ISR (stretch) |

Exact opcodes, register addresses and byte formats are still to be defined. The 20-bit pin init value needs two 16-bit config registers.

## 8. Area budget (rough estimate)

First-pass guesses to check that the design fits, to be replaced block by block with synthesis numbers.

**Reference point: the hello-world UART TX** (commit 4195bc5, GDS run on e0fcb6e):

- **Synthesis:** 179 cells, 2,640 µm².
  - 26 flip-flops (`dfrbpq`), 48% of that area
  - 48 tie cells
  - 105 logic, buffer and inverter cells
- **After place and route:** 256 standard cells, 3,776 µm².
  - 146 combinational cells (the 48 tie cells are among them)
  - 26 flip-flops
  - 7 buffers and inverters
  - 7 clock buffers and inverters
  - 70 timing-repair buffers, 55 of them hold buffers
- The 70 repair buffers add 38% on top of the other 186 cells, more than the 30% assumed below.
- **Timing:** +14.05 ns setup slack and +0.13 ns hold slack at 50 MHz.
- **Utilization:** 0.42% of the 6x4 core area of 902,417 µm².

At that design's average placed cell size (14.7 µm²), 65% of the core would hold about 40K cells. The 24K figure in section 1 is therefore conservative, but flip-flop-heavy blocks such as instruction memory have larger average cells, so keep it until real blocks are synthesized.

| Block | Flip-flops | Est. logic cells | Est. total |
| --- | --- | --- | --- |
| Instruction memory, 64 × 20 | 1,280 | \~2,500 (two read ports, one per SM) | \~3,800 |
| State machine, each | \~400 | \~2,000 | \~2,400 |
| State machines, both | \~800 | \~4,000 | \~4,800 |
| Host SPI and config registers | \~150 | \~250 | \~400 |
| Pin block (synchronizers, open-drain) | \~60 | \~60 | \~120 |
| Subtotal |  |  | \~9,100 |
| Hold-fix delay cells and buffers (+30%; hello-world measured +38%) |  |  | \~2,700 |
| Total |  |  | \~11,800 |

About 11,800 of roughly 24,000 cells, around half the budget, leaving room under the 60–70% ceiling. At the measured +38%, the total is about 12,600.

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
- [x] Pin pipeline: one output register stage on protocol outputs and output enables, none on inputs (decided)
- [ ] Stretch: bit-stuffing helper for USB and CAN
- [ ] Stretch: debug commands over the host interface

**Next step:** write the I2C master program twice on paper, once in plain PIO and once in this ISA. The difference in length and clarity is the core of the novelty argument.

## 10. Changes from v0.1

- **Encoding:**
  - IN/OUT count moves to \[3:0\], with 16 encoded as 0.
  - Unused operand bits must be 0.
  - MOV and SET get register codes.
  - The `CRC` move-to-ISR bit \[6\] is removed; use `MOV isr, crc` or `MOV osr, crc`.
- **New config:** JMP pin, separate shift directions, autopull/autopush enables, STATUS select and level, pin init value.
- **WAIT:** edge detection runs every system clock; the timeout and TO rules are defined.
- **CRC:** only 1-bit shifts update it in v1; the LFSR form is defined; reflected CRCs are handled in software.
- **Pins:** the reset state is defined; lower-numbered SM wins pin conflicts; side-set beats OUT/SET/MOV; pin index wrap and missing pins are defined; one output register stage on all protocol outputs.
- **MOV:** NULL is source code 3.
- **Host:** new Enable mask command for starting both SMs in lockstep.
- **Behavior now following PIO:** JMP X--/Y--, `!OSRE`, non-blocking PUSH/PULL, if full/if empty, autopull (including the 2-tick refill-then-OUT), autopush, wrap order, stalls list, IRQ wait-high clearing the flag, clock divider phase, restart, STATUS.
- **New sections:** assembly syntax (5.1); host IRQ access (section 7).
- **Section 8:** real hello-world numbers.
