"""Cycle-accurate reference model of the protocol emulator (docs/spec.md v0.1, section 6).

`Emulator.step()` advances one system clock (one rising edge). Within an edge:

  1. Each pad's line level is resolved from the chip's registered outputs and any external driver.
  2. Each enabled SM's clock divider counts; on a tick the SM executes against the *pre-edge* state:
     synchronized inputs, IRQ flags, its own registers and FIFOs.
  3. Pin and IRQ writes from both SMs are applied (SM0 first, then SM1, so SM1 wins a conflict).
  4. The 2-flop input synchronizers shift in the line levels from step 1.

So an external pin change made between edges is captured at edge k, reaches the SMs' view at edge k+1,
and an SM acts on it at edge k+2: 2 system clocks of synchronizer latency. A pin write made by an
instruction on edge k is visible on the output pad right after edge k.

Every behavior the spec leaves open is decided in one place and tagged `AMBIGUITY:` with the matching
number from model/README.md.
"""

from collections import deque
from dataclasses import dataclass

from .asm import (
    OP_CRC, OP_DELAY, OP_IN, OP_IRQ, OP_JMP, OP_MOV, OP_OUT, OP_PUSHPULL, OP_SET, OP_WAIT, IMEM_SIZE,
)

NUM_PINS = 20
UIO_PINS = range(0, 8)      # P0-P7: bidirectional, open-drain capable
OUT_PINS = range(8, 15)     # P8-P14: output only (uo_out[6:0])
IN_PINS = range(15, 20)     # P15-P19: input only (ui_in[7:3])
NUM_SMS = 2
NUM_IRQS = 4
FIFO_DEPTH = 4
MASK16 = 0xFFFF


@dataclass
class SMConfig:
    """Per-SM host configuration (spec section 2). Pin bases are absolute indexes P0-P19."""
    clkdiv: int = 1
    start: int = 0
    wrap_target: int = 0
    wrap_end: int = IMEM_SIZE - 1
    out_base: int = 0
    out_count: int = 0
    set_base: int = 0
    set_count: int = 0
    in_base: int = 0
    sideset_base: int = 0
    sideset_count: int = 0
    # AMBIGUITY 8: JMP pin (conds 8/9) has no config field in the spec; added here as an absolute index.
    jmp_pin: int = 0
    # AMBIGUITY 22: the spec has one "shift direction"; the model has one per shift register.
    out_shift_right: bool = True
    in_shift_right: bool = True
    autopull: bool = False
    pull_thresh: int = 16
    autopush: bool = False
    push_thresh: int = 16
    crc_poly: int = 0
    crc_width: int = 16
    crc_seed: int = 0

    def validate(self):
        if not 1 <= self.clkdiv <= 65535:
            # AMBIGUITY 21: the divider register is 16 bits but N is given as 1-65535; 0 is rejected.
            raise ValueError("clkdiv must be 1-65535")
        for name in ("start", "wrap_target", "wrap_end"):
            if not 0 <= getattr(self, name) < IMEM_SIZE:
                raise ValueError(f"{name} must be 0-{IMEM_SIZE - 1}")
        for name in ("out_base", "set_base", "in_base", "sideset_base", "jmp_pin"):
            if not 0 <= getattr(self, name) < NUM_PINS:
                raise ValueError(f"{name} must be 0-{NUM_PINS - 1}")
        if not 0 <= self.sideset_count <= 2:
            raise ValueError("sideset_count must be 0-2")
        if not 0 <= self.out_count <= 16 or not 0 <= self.set_count <= 7:
            raise ValueError("out_count must be 0-16 and set_count 0-7")
        if not (1 <= self.pull_thresh <= 16 and 1 <= self.push_thresh <= 16):
            raise ValueError("autopull/autopush thresholds must be 1-16")
        if not 1 <= self.crc_width <= 16:
            raise ValueError("crc_width must be 1-16")


def crc_step(crc, bit, poly, width):
    """One step of an MSB-first (non-reflected) LFSR CRC. Width 1 with poly 1 is running parity."""
    mask = (1 << width) - 1
    fb = ((crc >> (width - 1)) & 1) ^ bit
    crc = (crc << 1) & mask
    return crc ^ (poly & mask) if fb else crc


def bitrev16(v):
    return int(f"{v & MASK16:016b}"[::-1], 2)


class StateMachine:
    def __init__(self, emu, index):
        self.emu = emu
        self.index = index
        self.cfg = SMConfig()
        self.enabled = False
        self.tx_fifo = deque()
        self.rx_fifo = deque()
        self.reset()

    def reset(self):
        """Restart at the start address with registers, counters and flags cleared. FIFOs are kept."""
        self.pc = self.cfg.start
        self.x = self.y = 0
        self.osr = 0
        self.osr_count = 16   # OSR starts empty, so autopull fills it before the first OUT
        self.isr = 0
        self.isr_count = 0
        self.crc = self.cfg.crc_seed & ((1 << self.cfg.crc_width) - 1)
        self.tap_out = self.tap_in = False
        self.to = False
        self.div_count = 0
        self.delay_count = 0
        self.stalled = False      # True while the current instruction is stalled (side-set already applied)
        self.prev_in = None       # synchronized pin vector at this SM's previous tick, for WAIT edges
        self.ticks = 0

    @property
    def crcz(self):
        return self.crc == 0

    # ---- pin helpers -------------------------------------------------------------------------

    def _read_pins(self, base, count):
        sync = self.emu.sync_in
        return sum(sync[(base + i) % NUM_PINS] << i for i in range(count))

    def _write_pins(self, writes, base, count, value, kind):
        # AMBIGUITY 24: pin groups that run past P19 wrap to P0, as PIO wraps at 32.
        for i in range(count):
            writes.append((kind, (base + i) % NUM_PINS, (value >> i) & 1))

    # ---- shift helpers -----------------------------------------------------------------------

    def _shift_out(self, n):
        """Take n bits from the OSR in shift order. Returns (value, bits in the order shifted out)."""
        if self.cfg.out_shift_right:
            value = self.osr & ((1 << n) - 1)
            self.osr = (self.osr >> n) & MASK16
            bits = [(value >> i) & 1 for i in range(n)]
        else:
            value = (self.osr >> (16 - n)) & ((1 << n) - 1)
            self.osr = (self.osr << n) & MASK16
            bits = [(value >> (n - 1 - i)) & 1 for i in range(n)]
        self.osr_count = min(16, self.osr_count + n)
        return value, bits

    def _shift_in(self, value, n):
        value &= (1 << n) - 1
        if self.cfg.in_shift_right:
            self.isr = ((self.isr >> n) | (value << (16 - n))) & MASK16
            bits = [(value >> i) & 1 for i in range(n)]
        else:
            self.isr = ((self.isr << n) | value) & MASK16
            bits = [(value >> (n - 1 - i)) & 1 for i in range(n)]
        self.isr_count = min(16, self.isr_count + n)
        return bits

    def _crc_feed(self, bits):
        # AMBIGUITY 15: the unit is called bit-serial, but OUT/IN move up to 16 bits per tick; the
        # model applies every bit in the same tick, in the order the bits are shifted.
        for b in bits:
            self.crc = crc_step(self.crc, b, self.cfg.crc_poly, self.cfg.crc_width)

    # ---- one tick ----------------------------------------------------------------------------

    def tick(self, pin_writes, irq_writes):
        self.ticks += 1
        cur_in = list(self.emu.sync_in)
        prev_in = cur_in if self.prev_in is None else self.prev_in
        self.prev_in = cur_in

        if self.delay_count:
            self.delay_count -= 1
            return

        word = self.emu.imem[self.pc]
        opcode = (word >> 16) & 0xF
        delay = (word >> 13) & 0x7
        side_en = (word >> 12) & 1
        side_val = (word >> 10) & 0x3
        operand = word & 0x3FF

        writes, side_writes = [], []
        if side_en and not self.stalled:
            # Side-set is driven on the first tick of the instruction, even if it then stalls.
            self._write_pins(side_writes, self.cfg.sideset_base, self.cfg.sideset_count, side_val, "out")

        done, jump, extra = self._execute(opcode, operand, writes, irq_writes, cur_in, prev_in)

        # AMBIGUITY 16: side-set and OUT/SET/MOV hitting the same pin on the same tick: side-set wins.
        pin_writes.extend(writes)
        pin_writes.extend(side_writes)

        if not done:
            self.stalled = True
            return
        self.stalled = False
        if jump is not None:
            self.pc = jump
        elif self.pc == self.cfg.wrap_end:
            # AMBIGUITY 17: wrap happens after executing the instruction at wrap_end (PIO's .wrap).
            self.pc = self.cfg.wrap_target
        else:
            self.pc = (self.pc + 1) % IMEM_SIZE
        self.delay_count = delay + extra

    def _execute(self, op, operand, writes, irq_writes, cur_in, prev_in):
        """Run one tick of an instruction. Returns (done, jump_target_or_None, extra_delay_ticks)."""
        cfg = self.cfg

        if op == OP_JMP:
            cond, addr = operand >> 6, operand & 0x3F
            if cond == 0:
                take = True
            elif cond == 1:
                take = self.x == 0
            elif cond == 2:
                # AMBIGUITY 9: "X != 0, then X--": the model follows PIO and decrements whether or not
                # the jump is taken, so X wraps from 0 to 0xFFFF.
                take = self.x != 0
                self.x = (self.x - 1) & MASK16
            elif cond == 3:
                take = self.y == 0
            elif cond == 4:
                take = self.y != 0
                self.y = (self.y - 1) & MASK16
            elif cond == 5:
                take = self.x != self.y
            elif cond == 6:
                take = self.x == self.y
            elif cond == 8:
                take = cur_in[cfg.jmp_pin] == 1
            elif cond == 9:
                take = cur_in[cfg.jmp_pin] == 0
            elif cond == 10:
                # AMBIGUITY 10: "OSR not empty" means fewer than pull_thresh bits shifted out (PIO !OSRE).
                take = self.osr_count < cfg.pull_thresh
            elif cond == 11:
                take = len(self.rx_fifo) == FIFO_DEPTH
            elif cond == 12:
                take = self.to
            elif cond == 13:
                take = not self.to
            elif cond == 14:
                take = self.crcz
            else:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved JMP condition {cond}")
            return True, (addr if take else None), 0

        if op == OP_WAIT:
            mode, pol = operand >> 8, (operand >> 7) & 1
            a, b, timeout = cfg.in_base + ((operand >> 4) & 7), cfg.in_base + ((operand >> 1) & 7), operand & 1
            a, b = a % NUM_PINS, b % NUM_PINS
            if mode == 0:
                met = cur_in[a] == pol
            else:
                # AMBIGUITY 4: an edge is a change between this SM's previous tick and this tick, so
                # with clkdiv > 1 a pulse shorter than a tick can be missed, and an edge in the tick
                # before the WAIT starts still counts.
                met = prev_in[a] == 1 - pol and cur_in[a] == pol
                if mode == 2:
                    met = met and cur_in[b] == 1
                elif mode == 3:
                    met = met and cur_in[b] == 0
            if met:
                # AMBIGUITY 6: TO is rewritten by every WAIT that completes: cleared on success.
                self.to = False
                return True, None, 0
            if timeout:
                # AMBIGUITY 5: each unsatisfied tick decrements Y; when Y reaches 0 (or is 0 on
                # entry) the WAIT ends with TO set. Y = N gives at most N ticks of waiting.
                if self.y != 0:
                    self.y -= 1
                if self.y == 0:
                    self.to = True
                    return True, None, 0
            return False, None, 0

        if op == OP_IN:
            src, n = operand >> 7, operand & 0x1F
            if not 1 <= n <= 16:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: IN count {n} out of range")
            if cfg.autopush and self.isr_count + n >= cfg.push_thresh and len(self.rx_fifo) == FIFO_DEPTH:
                # AMBIGUITY 13: an IN that would trigger autopush with the RX FIFO full stalls
                # before shifting, so the bits are not lost.
                return False, None, 0
            if src == 0:
                value = self._read_pins(cfg.in_base, n)
            elif src == 1:
                value = self.x
            elif src == 2:
                value = self.y
            elif src == 3:
                value = 0
            elif src == 4:
                value = self.isr
            elif src == 5:
                value = self.osr
            elif src == 6:
                value = self.crc
            else:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved IN source 7")
            bits = self._shift_in(value, n)
            if self.tap_in:
                self._crc_feed(bits)
            if cfg.autopush and self.isr_count >= cfg.push_thresh:
                self.rx_fifo.append(self.isr)
                self.isr, self.isr_count = 0, 0
            return True, None, 0

        if op == OP_OUT:
            dst, n = operand >> 7, operand & 0x1F
            if not 1 <= n <= 16:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: OUT count {n} out of range")
            if cfg.autopull and self.osr_count >= cfg.pull_thresh:
                # AMBIGUITY 12: autopull refills when the next OUT finds the OSR empty; if the TX
                # FIFO is also empty the OUT stalls.
                if not self.tx_fifo:
                    return False, None, 0
                self.osr, self.osr_count = self.tx_fifo.popleft(), 0
            value, bits = self._shift_out(n)
            if self.tap_out:
                self._crc_feed(bits)
            jump = None
            if dst == 0:
                self._write_pins(writes, cfg.out_base, n, value, "out")
            elif dst == 1:
                self.x = value
            elif dst == 2:
                self.y = value
            elif dst == 3:
                pass
            elif dst == 4:
                self._write_pins(writes, cfg.out_base, n, value, "dir")
            elif dst == 5:
                jump = value & (IMEM_SIZE - 1)
            elif dst == 6:
                self.isr, self.isr_count = value, n
            else:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved OUT destination 7")
            return True, jump, 0

        if op == OP_PUSHPULL:
            is_pull, block, cond = (operand >> 9) & 1, (operand >> 8) & 1, (operand >> 7) & 1
            # AMBIGUITY 27: "if full / if empty" tests the ISR/OSR shift count against the threshold (PIO),
            # not the FIFO level.
            if is_pull:
                if cond and self.osr_count < cfg.pull_thresh:
                    return True, None, 0
                if not self.tx_fifo:
                    if block:
                        return False, None, 0
                    # AMBIGUITY 11: non-blocking PULL on an empty FIFO copies X into the OSR (PIO).
                    self.osr, self.osr_count = self.x, 0
                    return True, None, 0
                self.osr, self.osr_count = self.tx_fifo.popleft(), 0
            else:
                if cond and self.isr_count < cfg.push_thresh:
                    return True, None, 0
                if len(self.rx_fifo) == FIFO_DEPTH:
                    if block:
                        return False, None, 0
                    # AMBIGUITY 11: non-blocking PUSH on a full FIFO drops the word, ISR still clears (PIO).
                else:
                    self.rx_fifo.append(self.isr)
                self.isr, self.isr_count = 0, 0
            return True, None, 0

        if op == OP_MOV:
            dst, mop, src = operand >> 7, (operand >> 5) & 3, operand & 7
            if src == 0:
                value = self._read_pins(cfg.in_base, 16)
            elif src == 1:
                value = self.x
            elif src == 2:
                value = self.y
            elif src == 3:
                value = self.isr
            elif src == 4:
                value = self.osr
            elif src == 5:
                value = self.crc
            elif src == 6:
                # AMBIGUITY 3: STATUS contents are undefined; the model reads {14'b0, CRCZ, TO}.
                value = (int(self.crcz) << 1) | int(self.to)
            else:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved MOV source 7")
            if mop == 1:
                value = ~value & MASK16
            elif mop == 2:
                value = bitrev16(value)
            elif mop == 3:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved MOV op 3")
            if dst == 0:
                self._write_pins(writes, cfg.out_base, cfg.out_count, value, "out")
            elif dst == 1:
                self.x = value
            elif dst == 2:
                self.y = value
            elif dst == 3:
                self.isr, self.isr_count = value, 0
            elif dst == 4:
                self.osr, self.osr_count = value, 0
            elif dst == 5:
                self.crc = value & ((1 << cfg.crc_width) - 1)
            else:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: MOV destination {dst} not supported")
            return True, None, 0

        if op == OP_SET:
            dst, value = operand >> 7, operand & 0x7F
            if dst == 0:
                self._write_pins(writes, cfg.set_base, cfg.set_count, value, "out")
            elif dst == 1:
                self.x = value
            elif dst == 2:
                self.y = value
            elif dst == 4:
                self._write_pins(writes, cfg.set_base, cfg.set_count, value, "dir")
            else:
                raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved SET destination {dst}")
            return True, None, 0

        if op == OP_IRQ:
            irq_op, flag = operand >> 8, operand & 3
            if irq_op == 0:
                irq_writes.append((flag, 1))
            elif irq_op == 1:
                irq_writes.append((flag, 0))
            else:
                # AMBIGUITY 18: wait-high/wait-low only wait; they do not clear the flag afterwards.
                want = 1 if irq_op == 2 else 0
                if self.emu.irq[flag] != want:
                    return False, None, 0
            return True, None, 0

        if op == OP_CRC:
            width_mask = (1 << cfg.crc_width) - 1
            # AMBIGUITY 14: order within one CRC instruction is move-to-ISR (old value), then reset,
            # then the tap enables are overwritten by bits [8] and [7].
            if operand & (1 << 6):
                self.isr, self.isr_count = self.crc, 0
            if operand & (1 << 9):
                self.crc = cfg.crc_seed & width_mask
            self.tap_out = bool(operand & (1 << 8))
            self.tap_in = bool(operand & (1 << 7))
            return True, None, 0

        if op == OP_DELAY:
            # AMBIGUITY 7: DELAY n takes 1 tick plus n extra ticks (like `nop [n]`), plus its own delay field.
            return True, None, operand

        raise RuntimeError(f"SM{self.index} pc {self.pc}: reserved opcode {op}")


class Emulator:
    """Two SMs, shared instruction memory, shared IRQ flags and the 20-pin protocol pin space."""

    def __init__(self):
        self.imem = [0] * IMEM_SIZE
        self.irq = [0] * NUM_IRQS
        # Chip-side pin registers. AMBIGUITY 19: output values reset to 0 and there is no config
        # for an initial level, so a UART TX pin idles low until the program drives it.
        self.out_val = [0] * NUM_PINS
        self.pindir = [0] * NUM_PINS
        self.open_drain = [0] * len(UIO_PINS)
        # External drivers: None = not driving. Undriven lines read the pull level.
        self.ext = [None] * NUM_PINS
        self.pull = [1] * len(UIO_PINS) + [0] * (NUM_PINS - len(UIO_PINS))
        self.sync1 = [0] * NUM_PINS
        self.sync_in = [0] * NUM_PINS   # second synchronizer flop: what the SMs read
        self.sms = [StateMachine(self, i) for i in range(NUM_SMS)]
        self.cycle = 0

    # ---- host-side helpers -------------------------------------------------------------------

    def load(self, words, base=0):
        if base + len(words) > IMEM_SIZE:
            raise ValueError("program does not fit in instruction memory")
        for i, w in enumerate(words):
            if not 0 <= w < 1 << 20:
                raise ValueError(f"word {i} is not 20 bits")
            self.imem[base + i] = w

    def configure(self, sm, cfg):
        cfg.validate()
        self.sms[sm].cfg = cfg

    def enable(self, sm, restart=True):
        s = self.sms[sm]
        if restart:
            s.reset()
        s.enabled = True

    def disable(self, sm):
        self.sms[sm].enabled = False

    def push_tx(self, sm, word):
        """Host writes the TX FIFO. Returns False (and drops the word) if it is full."""
        f = self.sms[sm].tx_fifo
        if len(f) == FIFO_DEPTH:
            return False
        f.append(word & MASK16)
        return True

    def pop_rx(self, sm):
        """Host reads the RX FIFO. Returns None if it is empty."""
        f = self.sms[sm].rx_fifo
        return f.popleft() if f else None

    # ---- pads --------------------------------------------------------------------------------

    def drive(self, pin, value):
        """Drive a pad from outside (a simulated device). None releases it."""
        self.ext[pin] = value

    def chip_drive(self, pin):
        """What the chip drives onto a pad: 0, 1 or None (output enable off)."""
        if pin in IN_PINS:
            return None
        if pin in OUT_PINS:
            return self.out_val[pin]
        if self.open_drain[pin]:
            # AMBIGUITY 20: in open-drain mode PINDIRS is ignored: 0 drives low, 1 releases.
            return 0 if self.out_val[pin] == 0 else None
        return self.out_val[pin] if self.pindir[pin] else None

    def line(self, pin):
        """Resolved pad level. Drivers wire-AND; an undriven line reads its pull level."""
        if pin in OUT_PINS:
            # AMBIGUITY 23: out-only pins have no input buffer; reading one returns its output value.
            return self.out_val[pin]
        drivers = [d for d in (self.chip_drive(pin), self.ext[pin]) if d is not None]
        if not drivers:
            return self.pull[pin]
        return int(all(drivers))

    # ---- clock -------------------------------------------------------------------------------

    def step(self, n=1):
        for _ in range(n):
            lines = [self.line(p) for p in range(NUM_PINS)]
            pin_writes, irq_writes = [], []
            for sm in self.sms:
                if not sm.enabled:
                    continue
                sm.div_count += 1
                if sm.div_count >= sm.cfg.clkdiv:
                    sm.div_count = 0
                    sm.tick(pin_writes, irq_writes)
            # AMBIGUITY 25: same-edge conflicts between SMs (pins, IRQ set vs clear): SM1's write wins.
            for kind, pin, value in pin_writes:
                if kind == "out":
                    if pin not in IN_PINS:
                        self.out_val[pin] = value
                elif pin in UIO_PINS:
                    self.pindir[pin] = value
            for flag, value in irq_writes:
                self.irq[flag] = value
            self.sync_in = self.sync1
            self.sync1 = lines
            self.cycle += 1

    def run_until(self, cond, max_cycles=1_000_000):
        for _ in range(max_cycles):
            if cond():
                return
            self.step()
        raise TimeoutError(f"condition not met within {max_cycles} cycles")
