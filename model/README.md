# Protocol emulator reference model

A Python reference model for the ISA in [`docs/spec.md`](../docs/spec.md) (v0.1 draft). It has three parts:

- [`asm.py`](asm.py): an assembler that turns `.pasm` source into 20-bit machine words (spec sections 4–5). The syntax is documented at the top of the file.
- [`sim.py`](sim.py): a cycle-accurate simulator of the 2-SM emulator (spec section 6). `Emulator.step()` advances one system clock.
- [`programs/`](programs/): example programs. `uart_tx.pasm` is an 8N1 UART transmitter.

## Running

From the repo root:

```sh
.venv/bin/python -m pytest model/tests -q        # all tests
.venv/bin/python -m model.asm model/programs/uart_tx.pasm   # assemble and print hex words
```

## Using the simulator

```python
from model.asm import assemble_file
from model.sim import Emulator, SMConfig

prog = assemble_file("model/programs/uart_tx.pasm")
emu = Emulator()
emu.load(prog.words)
emu.configure(0, SMConfig(clkdiv=54, wrap_target=prog.wrap_target, wrap_end=prog.wrap,
                          out_base=8, out_count=1, sideset_base=8, sideset_count=1))
emu.enable(0)
emu.push_tx(0, 0x41)
for _ in range(5000):
    emu.step()
    level = emu.line(8)          # TX pad level after this clock
```

Simulated devices connect through `emu.drive(pin, 0 | 1 | None)` and `emu.line(pin)`. Pads wire-AND, and an
undriven line reads its pull level: high on `uio` (P0–P7, assuming external pull-ups), low on `ui_in` (P15–P19).

**Timing.** An instruction that writes a pin on clock edge *k* changes the pad right after edge *k*. An external
pin change is captured by the first synchronizer flop at the next edge, so an SM first acts on it 2 edges later.
That gives the 2 clocks of synchronizer latency in spec section 6.

## Ambiguities

The spec leaves the following open or contradicts itself. The model makes one choice for each so it can run;
every choice is tagged `AMBIGUITY <n>` in the code. None of them is settled until you decide it in the spec.

### Encoding (sections 4–5)

1. **No assembly syntax is defined.** The only hints are `wait 0 pin 0` and `pull side 1`. The model uses
   PIO-style syntax: `side <v>` then `[delay]` after the operands, `.wrap_target`/`.wrap`, `.side_set <n>`.
2. **SET and MOV have no operand codes.** SET lists PINS, PINDIRS, X, Y, and MOV lists PINS, X, Y, ISR, OSR, CRC,
   STATUS, but neither gives bit values. The model reuses the IN/OUT codes for SET (PINS 0, X 1, Y 2, PINDIRS 4)
   and numbers MOV registers 0–6 in the order listed. There are only 3 MOV ops (none, invert, bit-reverse) for a
   2-bit field, so code 3 is left reserved.
3. **STATUS is undefined.** Its contents as a MOV source, and what writing it as a destination would do, aren't
   specified. The model reads it as `{14'b0, CRCZ, TO}` and the assembler rejects it as a destination. Also,
   CRCZ is listed among the 2 flag bits but is described as "CRC is zero", which is a derived value, not a
   stored flag.
26. **The IN/OUT count field is 5 bits holding "1–16".** The spec doesn't say how 16 encodes. The model stores
    the count directly, so 0 and 17–31 never encode. Unused operand bits (IN/OUT [6:5], MOV [4:3], IRQ [7:2],
    CRC [5:0], pin B in WAIT modes 00/01) aren't required to be zero; the assembler emits zeros.

### Execution (section 6)

4. **WAIT edges aren't defined relative to any earlier sample.** The model compares the synchronized pin at
   this SM tick against the previous SM tick. Consequences: with `clkdiv > 1`, a pulse shorter than one tick
   is missed, and an edge that happened during the tick before the WAIT still counts. A per-system-clock edge
   detector would behave differently. For the qualified modes, pin B is sampled on the same tick as A's new
   level.
5. **The WAIT timeout off-by-one is unspecified.** "Y counts down once per tick … if it reaches zero" doesn't
   say whether Y is checked before or after the decrement, or what happens when Y is already 0. In the model,
   each unsatisfied tick decrements Y and the WAIT ends with TO set when Y reaches 0, so Y = N allows at most
   N ticks. Y = 0 on entry times out on the first tick if the condition is false.
6. **When TO clears is unspecified.** The model rewrites TO on every WAIT completion: it's set on a timeout
   and cleared on success, including WAITs without a timeout. The other choices would be: only
   timeout-enabled WAITs touch TO, or `JMP to` clears it.
7. **DELAY's tick count is unspecified.** "Holds it for n ticks" could mean n ticks total or 1 + n. The model
   uses 1 + n, like `nop [n]`, so `delay 0` is a 1-tick no-op. It's also unspecified whether DELAY's own delay
   field adds on top. The model adds it, and the assembler rejects n + d > 1023 because the delay counter is
   10 bits.
8. **No JMP pin is configured.** Conditions 8/9 test "JMP pin", but section 2's config list has no field for
   it. The model adds `jmp_pin` as an absolute pin index.
9. **"X != 0, then X--" has two readings.** It could decrement only when X != 0, or always (PIO). The model
   decrements always, so the loop exits with X = 0xFFFF. Loop counts are the same either way; only the final
   X differs. Same for Y.
10. **"OSR not empty" is undefined.** The model follows PIO's `!OSRE`: fewer than `pull_thresh` bits have
    shifted out, so it depends on the autopull threshold even when autopull is off.
11. **Non-blocking PUSH/PULL are undefined.** The model follows PIO: PULL noblock on an empty FIFO copies X
    into the OSR; PUSH noblock on a full FIFO drops the word but still clears the ISR.
12. **Autopull timing is unspecified.** "The OSR refills after a set number of bits have shifted out" doesn't
    say when or what happens if the FIFO is empty. The model refills when the next OUT finds the OSR at
    threshold, and that OUT stalls if the TX FIFO is empty. It's also unspecified what happens when one OUT
    crosses the threshold, e.g. `out x, 5` with 6 bits left before an 8-bit threshold. The model shifts all n
    bits and refills on the next OUT.
13. **Autopush with a full RX FIFO is unspecified.** The model stalls the IN *before* shifting, so no bits are
    lost and the IN repeats cleanly once there's room.
14. **The `CRC` instruction is underspecified, and contradicts itself.**
    - It's unspecified whether tap bits [8]/[7] set a persistent mode, toggle it, or apply only to the next
      shift. The model treats them as a persistent mode overwritten by every `CRC`, so `crc reset` also turns
      tapping off.
    - The order when several bits are set is unspecified. The model moves to ISR first (old value), then
      resets, then sets the taps.
    - The ISR shift count after "move CRC to ISR" is unspecified. The model sets it to 0, like `mov isr`.
    - **Contradiction:** the spec says CRC "can … copy it into the ISR *to send*", but the ISR goes to the RX
      FIFO (to the host), not to pins. Sending a CRC on the wire needs it in the OSR. That would need a path
      like `mov osr, crc`, which the model allows as a register move but which isn't in the spec text.
    - This is also the third way to read the CRC, alongside `in crc, n` and `mov x, crc`. Worth checking it
      earns its operand bit.
15. **The CRC unit is called "bit-serial", but OUT/IN move up to 16 bits per tick.** Hardware would need up to
    16 LFSR steps per tick (a 16-deep XOR network per SM), or OUT/IN would have to take extra ticks. The model
    applies all bits in one tick, in the order they shift. Also unspecified:
    - which LFSR form is used. The model uses MSB-first, non-reflected, with no final XOR.
    - whether OUT NULL and IN NULL bits are tapped. The model taps them.
    USB CRC-5/CRC-16 are reflected (LSB-first) CRCs. Feeding bits in wire order gives the right remainder, but
    bit-reversed, and MOV's bit-reverse is fixed at 16 bits, so a 5-bit result can't be reversed in one step.
    The spec's claim that the unit covers USB depends on how this is resolved.
16. **Side-set and OUT/SET/MOV hitting the same pin on the same tick has no priority rule.** The model gives
    side-set priority, as PIO does.
17. **"When the PC reaches the wrap end" has two readings:** wrap after executing the wrap-end instruction
    (PIO), or skip it. The model wraps after executing it.
18. **IRQ wait-high/wait-low don't say whether they clear the flag afterwards.** PIO's `wait irq` clears it.
    The model doesn't clear it. The host side of "SM-to-host signalling" isn't specified either: section 7's
    status read shows the flags, but nothing lets the host clear or set them.
19. **Output pins have no initial or idle level.** All pin outputs reset to 0, so a UART TX pin sits low (a
    break condition) from reset until the program's first `side 1`. Real UARTs would see a framing error. This
    probably needs per-pin reset values in the host config.
20. **The interaction between open-drain mode and PINDIRS is unspecified.** The model ignores PINDIRS in
    open-drain mode: writing 0 drives low and writing 1 releases.
21. **The divider register is 16 bits but N is given as 1–65535.** The model rejects 0; PIO treats 0 as
    65536. The divider phase is also unspecified. The model's first tick comes N clocks after enable.
22. **Shift-direction config is underspecified.** The spec lists one "shift direction" but there are two
    shift registers; the model has one per register. Autopull/autopush enable bits aren't listed either, only
    thresholds; the model has separate enables.
23. **Out-only pins (P8–P14) have no input path**, yet IN/MOV/WAIT can address them by index. The model
    returns the output value through the synchronizer. Returning 0 would also be reasonable.
24. **Pin indexes are 5 bits, but only P0–P19 exist.** Groups can also run past P19, e.g. `in_base` 15 with
    `in pins, 8`, or `mov y, pins`, which reads 16 pins. The model wraps P19 → P0, as PIO wraps at 32, and
    rejects bases ≥ 20.
25. **No rule covers both SMs writing the same pin or IRQ flag on the same clock.** The model lets SM1 win, as
    PIO lets the higher-numbered SM win. Set-vs-clear on an IRQ follows the same rule.
27. **"If full / if empty" on PUSH/PULL [7] could test the FIFO or the shift count.** The model follows PIO:
    the ISR/OSR shift count against the threshold.

### Spec text only (not modeled)

28. **Section 6's stall list is incomplete.** It names only WAIT, blocking PULL and blocking PUSH, but IRQ
    wait-high/wait-low and autopull/autopush also stall.
29. **Section 8's reference numbers don't match the hello-world GDS run** (e0fcb6e). The spec says "208 cells
    for 26 flip-flops, 55 of them delay cells". The run had 179 cells after synthesis and 256 standard cells
    after place-and-route, 55 of them hold buffers, and the flip-flops aren't delay cells.
30. **Restart behavior is unspecified.** Section 7's `Control: restart` doesn't say whether the FIFOs, X/Y or
    the CRC clear. The model's `enable(restart=True)` clears registers, flags and counters, and keeps the
    FIFOs.
