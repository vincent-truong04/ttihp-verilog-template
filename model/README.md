# Protocol emulator reference model

A Python reference model for the ISA in [`docs/spec.md`](../docs/spec.md) (v0.2 draft). It has three parts:

- [`asm.py`](asm.py): an assembler that turns `.pasm` source into 20-bit machine words (spec sections 4–5.1).
- [`sim.py`](sim.py): a cycle-accurate simulator of the 2-SM emulator (spec section 6). `Emulator.step()` advances one system clock.
- [`programs/`](programs/): example programs. `uart_tx.pasm` is an 8N1 UART transmitter.

## Running

From the repo root:

```sh
.venv/bin/python -m pytest model/tests -q                   # all tests (CI runs these on every push)
.venv/bin/python -m model.asm model/programs/uart_tx.pasm   # assemble, print hex words and any warnings
```

## Using the simulator

```python
from model.asm import assemble_file
from model.sim import Emulator, SMConfig

prog = assemble_file("model/programs/uart_tx.pasm")
emu = Emulator()
emu.load(prog.words)
emu.configure(0, SMConfig(clkdiv=54, wrap_target=prog.wrap_target, wrap_end=prog.wrap,
                          out_base=8, out_count=1, sideset_base=8, sideset_count=1,
                          pin_init=1 << 8))   # TX idles high from enable
emu.enable(0)
emu.push_tx(0, 0x41)
for _ in range(5000):
    emu.step()
    level = emu.line(8)          # TX pad level after this clock
```

Simulated devices connect through `emu.drive(pin, 0 | 1 | None)` and `emu.line(pin)`. Pads wire-AND, and an
undriven line reads its pull level: high on `uio` (P0–P7, assuming external pull-ups), low on `ui_in` (P15–P19).
The host side is `push_tx`, `pop_rx`, `write_irq`, `configure`, `enable`, `enable_mask`, `disable` and `restart`.

A word with a reserved code or a nonzero unused bit raises `ReservedEncoding` when it executes (spec section 4).

**Timing.** An instruction that writes a pin on clock edge *k* changes the pad right after edge *k* + 1, through
the output register (spec section 6). An external pin change is captured by the first synchronizer flop at the
next edge, so an SM first acts on it 2 edges later. An SM reading its own output sees the change 4 clocks after
writing it, the same as PIO with synchronizers.

## Ambiguities from v0.1, resolved in v0.2

Each was tagged `AMBIGUITY <n>` in the code. The tags now say "resolved" and name the spec section. In the
"Source" column:
- **PIO** means the PIO rule applies where the spec was silent; the RP2040 datasheet section is in the spec.
- **Decided** means you decided it.
- **Ruling** means it's one of the v0.2 rulings below, now accepted.

| # | Issue | Resolution | Spec | Source |
| --- | --- | --- | --- | --- |
| 1 | No assembly syntax | pioasm-style syntax plus the new instructions | 5.1 | PIO §3.3 |
| 2 | No SET/MOV codes | PIO's codes; CRC at MOV code 4 | 5 SET, MOV | PIO §3.4.8, §3.4.10 |
| 3 | STATUS undefined | All ones if the TX/RX FIFO level is below N; source only | 5 MOV | PIO §3.4.8 |
| 4 | WAIT edge reference | Detector runs every system clock while the WAIT is active; edges before it starts don't count | 5 WAIT | Decided |
| 5 | Timeout off-by-one | Each tick: met → done; Y == 0 → done, TO set; else Y-- | 5 WAIT | Decided |
| 6 | When TO clears | When each timeout WAIT starts | 5 WAIT | Decided |
| 7 | DELAY tick count | 1 + n ticks, plus its delay field | 5 DELAY | Ruling |
| 8 | No JMP pin config | Absolute-index JMP pin field | 2, 5 JMP | PIO §3.4.2 |
| 9 | X-- semantics | Always decrements | 5 JMP | PIO §3.4.2 |
| 10 | "OSR not empty" | Shift count below the pull threshold | 5 JMP | PIO §3.4.2 |
| 11 | Non-blocking PUSH/PULL | PULL copies X; PUSH drops the word and clears the ISR | 5 PUSH/PULL | PIO §3.4.6, §3.4.7 |
| 12 | Autopull timing | PIO's pseudocode exactly, including the refill-then-stall tick | 6 Autopull | Decided (PIO §3.5.4.2) |
| 13 | Autopush, RX full | Stall | 6 Autopush | PIO §3.5.4.1 |
| 14 | `CRC` instruction | Taps persist until the next `CRC`; move-to-ISR bit removed | 5 CRC | Decided |
| 15 | Bit-serial CRC vs multi-bit shifts | Only 1-bit shifts update it; MSB-first, non-reflected; assembler warns | 6 CRC unit | Decided |
| 16 | Side-set vs OUT/SET | Side-set wins | 6 Side-set | Decided (= PIO §3.5.1) |
| 17 | Wrap point | After executing wrap end, unless jumping | 6 Wrap | PIO §3.5.2 |
| 18 | IRQ wait and host IRQ access | Wait-high clears the flag; host gets clear/force | 5 IRQ, 7 | PIO §3.4.3, §3.7 |
| 19 | Pin reset level | uo_out drives 0, uio released; per-SM pin init value on enable | 3 | Decided |
| 20 | Open-drain vs PINDIRS | PINDIRS ignored in open-drain mode | 6 Open-drain | Ruling |
| 21 | clkdiv 0 and divider phase | 0 = 65536; first tick N clocks after enable | 6 Timing | PIO §3.5.5 |
| 22 | Shift-direction config | Separate directions and enables | 2 | PIO §3.7 SHIFTCTRL |
| 23 | Reading out-only pins | Returns the driven value | 3 | Ruling |
| 24 | Pin index range | Wrap mod 32; P20–P31 read 0, writes ignored | 3 | PIO §3.5.6.2 |
| 25 | Two SMs on one pin | Lower-numbered SM wins (levels and directions separately) | 6 Pin writes | Decided (PIO is the reverse) |
| 26 | Count encoding, unused bits | 16 encoded as 0; unused bits must be 0, assembler-enforced | 4 | Decided (= PIO §3.4.4) |
| 27 | If full / if empty | Shift count vs threshold | 5 PUSH/PULL | PIO §3.4.6, §3.4.7 |
| 28 | Stall list incomplete | Full list | 6 Stalls | PIO §3.2.4 |
| 29 | Section 8 numbers | Real hello-world numbers | 8 | Decided |
| 30 | What restart clears | PIO's SM_RESTART list, then PC = start | 6 Restart | PIO §3.7 CTRL |

## v0.2 rulings (accepted)

Applying the rules above raised these. All are now accepted and written into the spec as settled rules:

1. **DELAY n** takes 1 + n ticks, plus its delay field (§5).
2. **Open-drain mode ignores PINDIRS** (§6).
3. **`uio` output values reset to 1**, so turning on open-drain mode leaves the pins released (§3).
4. **Out-only pins (P8–P14) read their driven value** through the synchronizer (§3).
5. **The pin init value covers only the SM's own OUT, SET and side-set groups.** It sets levels, not
   directions, and isn't reapplied on restart (§3).
6. **IRQ flag conflicts:** the lower-numbered SM wins, as for pins (§6).
7. **A stalled autopush IN doesn't shift** until the push can happen (§6).
8. **"OSR full" for PULL under autopull** means a shift count of 0 (§5).
9. **Restart also clears TO, reloads the CRC seed, turns taps off and disarms the edge detector** (§6).
10. **CRC taps apply to every 1-bit shift**, NULL included. The spec tells programs to turn taps off before
    shifting NULL bits that must not enter the CRC (§6).
11. **A WAIT without timeout leaves TO alone** (§5).
12. **Edge-detector boundaries:** an edge on the WAIT's first-tick clock doesn't count, and a WAIT can complete
    on the same clock as its edge (§5).
13. **The divider starts on enable.** A new host **Enable mask** command enables both SMs on the same clock,
    so they tick in lockstep, in place of PIO's CLKDIV_RESTART (§6, §7). The model's version is
    `Emulator.enable_mask`.
14. **One output register stage** on every protocol output level and output enable, and none on inputs.
    Pins change only on clock edges, one edge after the write (§6).
15. **MOV source 3 is NULL** (§5). MOV destination 5 stays reserved.

Two notes on the tools and numbers:
- The assembler's CRC warning is a static whole-program check. It warns on every multi-bit IN or OUT in a
  program that turns tapping on anywhere, even when tapping is off at that point.
- Section 8 shows the 6x4 core holds roughly 40K cells at the hello-world's average cell size. The 24K figure
  in section 1 and the area budget are unchanged.
