"""Cycle-accurate reference model of the protocol emulator (docs/spec.md v0.3, section 6).

`Emulator.step()` advances one system clock (one rising edge). Within an edge:

  1. Each pad's line level is resolved from the chip's registered outputs and any external driver. The SMs'
     view of each input is its second synchronizer flop, or the pad itself if that pin's bypass bit is set.
  2. Each enabled SM's WAIT edge detector (if armed) compares this clock's view of the inputs with the last.
  3. Each enabled SM's clock divider counts; on a tick the SM executes against the *pre-edge* state:
     synchronized inputs, IRQ flags, its own registers and FIFOs.
  4. Pin and IRQ writes are applied: SM1's first, then SM0's, so the lower-numbered SM wins a conflict.
  5. The 2-flop input synchronizers shift in the line levels from step 1.

  6. The output register captures each pad's drive from the pre-edge pin state (spec section 6).

So an external pin change made between edges is captured at edge k, reaches the SMs' view at edge k+1,
and an SM acts on it at edge k+2. A pin write made by an instruction on edge k reaches the pad right after
edge k+1, through the output register.

Every place the v0.1 spec was ambiguous is tagged `AMBIGUITY <n>` with the spec section that now resolves it.
"""

from collections import deque
from dataclasses import dataclass

from .asm import (
    OP_CRC, OP_DELAY, OP_IN, OP_IRQ, OP_JMP, OP_MOV, OP_OUT, OP_PUSHPULL, OP_SET, OP_WAIT, IMEM_SIZE,
)

NUM_PINS = 20
PIN_SPACE = 32              # 5-bit pin indexes; P20-P31 do not exist
UIO_PINS = range(0, 8)      # P0-P7: bidirectional, open-drain capable
OUT_PINS = range(8, 15)     # P8-P14: output only (uo_out[6:0])
IN_PINS = range(15, 20)     # P15-P19: input only (ui_in[7:3])
NUM_SMS = 2
NUM_IRQS = 4
FIFO_DEPTH = 4
MASK16 = 0xFFFF


@dataclass
class SMConfig:
    """Per-SM host configuration (spec section 2). Pin bases and jmp_pin are absolute 5-bit indexes."""
    clkdiv: int = 1             # AMBIGUITY 21 (resolved, spec section 6 Timing): 0 means 65536, as in PIO
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
    jmp_pin: int = 0            # AMBIGUITY 8 (resolved, spec section 2 config / 5 JMP)
    # AMBIGUITY 22 (resolved, spec section 2 config): separate shift directions and enables, as in PIO
    out_shift_right: bool = True
    in_shift_right: bool = True
    autopull: bool = False
    pull_thresh: int = 16
    autopush: bool = False
    push_thresh: int = 16
    status_rx: bool = False     # STATUS compares the RX FIFO level if True, else TX (spec section 5 MOV)
    status_n: int = 0
    crc_poly: int = 0
    crc_width: int = 16
    crc_seed: int = 0
    pin_init: int = 0           # AMBIGUITY 19 (resolved, spec section 3): written to the SM's pins on enable
    timeout: int = 0            # timeout reload value N: a timeout WAIT allows up to N + 1 ticks (spec v0.3)

    def validate(self):
        if not 0 <= self.clkdiv <= 65535:
            raise ValueError("clkdiv must be 0-65535 (0 means 65536)")
        for name in ("start", "wrap_target", "wrap_end"):
            if not 0 <= getattr(self, name) < IMEM_SIZE:
                raise ValueError(f"{name} must be 0-{IMEM_SIZE - 1}")
        for name in ("out_base", "set_base", "in_base", "sideset_base", "jmp_pin"):
            if not 0 <= getattr(self, name) < PIN_SPACE:
                raise ValueError(f"{name} must be 0-{PIN_SPACE - 1}")
        if not 0 <= self.sideset_count <= 2:
            raise ValueError("sideset_count must be 0-2")
        if not 0 <= self.out_count <= 16 or not 0 <= self.set_count <= 7:
            raise ValueError("out_count must be 0-16 and set_count 0-7")
        if not (1 <= self.pull_thresh <= 16 and 1 <= self.push_thresh <= 16):
            raise ValueError("autopull/autopush thresholds must be 1-16")
        if not 0 <= self.timeout <= 0xFFFF:
            raise ValueError("timeout must be 0-65535")
        if not 0 <= self.status_n <= 15:
            raise ValueError("status_n must be 0-15")
        if not 1 <= self.crc_width <= 16:
            raise ValueError("crc_width must be 1-16")
        if not 0 <= self.pin_init < 1 << NUM_PINS:
            raise ValueError("pin_init is 20 bits")

    @property
    def period(self):
        return self.clkdiv or 65536


def crc_step(crc, bit, poly, width):
    """One step of the MSB-first, non-reflected LFSR (spec section 6). Width 1 with poly 1 is parity."""
    mask = (1 << width) - 1
    fb = ((crc >> (width - 1)) & 1) ^ bit
    crc = (crc << 1) & mask
    return crc ^ (poly & mask) if fb else crc


def bitrev16(v):
    return int(f"{v & MASK16:016b}"[::-1], 2)


class ReservedEncoding(RuntimeError):
    """An instruction used a reserved code or set an unused operand bit (spec section 4)."""


class StateMachine:
    def __init__(self, emu, index):
        self.emu = emu
        self.index = index
        self.cfg = SMConfig()
        self.enabled = False
        self.tx_fifo = deque()
        self.rx_fifo = deque()
        # Power-on reset (spec section 6, Reset): everything cleared, shift counts as on restart, PC 0
        self.x = self.y = 0
        self.osr = 0
        self.div_count = 0
        self.ticks = 0
        self.restart()
        self.pc = 0

    def restart(self):
        """Spec section 6, Restart: PIO's SM_RESTART state, plus TO, CRC, taps and edge detector, then PC = start.

        AMBIGUITY 30 (resolved, spec section 6): X, Y, OSR and the FIFOs are kept, as in PIO.
        """
        self.isr = 0
        self.isr_count = 0
        self.osr_count = 16       # OSR empty, so autopull fills it before the first OUT
        self.delay_count = 0
        self.stalled = False      # True while the current instruction is stalled (side-set already applied)
        # Restart also clears TO, reloads the CRC seed, turns taps off and disarms the edge detector
        self.to = False
        self.crc = self.cfg.crc_seed & ((1 << self.cfg.crc_width) - 1)
        self.tap_out = self.tap_in = False
        self.edge_armed = False
        self.edge_prev = 0
        self.edge_seen = False
        self.tcount = 0           # dedicated timeout counter (spec v0.3 section 5)
        self.pc = self.cfg.start

    @property
    def crcz(self):
        return self.crc == 0

    # ---- pin helpers -------------------------------------------------------------------------

    def _pin_in(self, index):
        # AMBIGUITY 24 (resolved, spec section 3): indexes wrap mod 32, and P20-P31 read as 0
        index %= PIN_SPACE
        return self.emu.view[index] if index < NUM_PINS else 0

    def _read_pins(self, base, count):
        return sum(self._pin_in(base + i) << i for i in range(count))

    def _write_pins(self, writes, base, count, value, kind):
        for i in range(count):
            writes.append((kind, (base + i) % PIN_SPACE, (value >> i) & 1))

    def pin_group(self):
        """Absolute pins in this SM's OUT, SET and side-set groups."""
        cfg = self.cfg
        pins = set()
        for base, count in ((cfg.out_base, cfg.out_count), (cfg.set_base, cfg.set_count),
                            (cfg.sideset_base, cfg.sideset_count)):
            pins.update((base + i) % PIN_SPACE for i in range(count))
        return pins

    # ---- shift helpers -----------------------------------------------------------------------

    def _shift_out(self, n):
        """Take n bits from the OSR (PIO 3.4.5). Returns (value, bits in the order shifted out)."""
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
        """Shift n bits into the ISR (PIO 3.4.4): bit order does not depend on the shift direction."""
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
        # AMBIGUITY 15 (resolved, spec section 6 CRC unit): in v1 only 1-bit shifts update the CRC
        if len(bits) == 1:
            self.crc = crc_step(self.crc, bits[0], self.cfg.crc_poly, self.cfg.crc_width)

    def _autopull_background(self):
        # AMBIGUITY 12 (resolved, spec section 6 Autopull, PIO 3.5.4.2): refill on any non-OUT tick
        if self.cfg.autopull and self.osr_count >= self.cfg.pull_thresh and self.tx_fifo:
            self.osr, self.osr_count = self.tx_fifo.popleft(), 0

    # ---- WAIT edge detector ------------------------------------------------------------------

    def edge_clock(self):
        """Run every system clock while an edge-mode WAIT is active.

        AMBIGUITY 4 (resolved, spec section 5 WAIT): the detector runs per system clock regardless of
        clkdiv, pin B is sampled on the same clock as the edge, and the first qualifying edge is latched.
        """
        if not self.edge_armed:
            return
        operand = self.emu.imem[self.pc] & 0x3FF
        mode, pol = operand >> 8, (operand >> 7) & 1
        cur = self._pin_in(self.cfg.in_base + ((operand >> 4) & 7))
        if not self.edge_seen and self.edge_prev == 1 - pol and cur == pol:
            b = self._pin_in(self.cfg.in_base + ((operand >> 1) & 7))
            self.edge_seen = mode == 1 or (mode == 2 and b == 1) or (mode == 3 and b == 0)
        self.edge_prev = cur

    # ---- one tick ----------------------------------------------------------------------------

    def tick(self, pin_writes, irq_writes):
        self.ticks += 1

        if self.delay_count:
            self.delay_count -= 1
            self._autopull_background()
            return

        word = self.emu.imem[self.pc]
        opcode = (word >> 16) & 0xF
        side_en = (word >> 12) & 1
        delay = (word >> 13) & 0x7
        if not side_en:
            delay |= ((word >> 10) & 0x3) << 3     # spec v0.3 section 4: 5-bit delay without side-set
        side_val = (word >> 10) & 0x3
        operand = word & 0x3FF

        first_tick = not self.stalled
        writes, side_writes = [], []
        if side_en and first_tick:
            # Side-set is driven on the first tick of the instruction, even if it then stalls (PIO 3.2.4).
            self._write_pins(side_writes, self.cfg.sideset_base, self.cfg.sideset_count, side_val, "out")

        done, jump, extra = self._execute(opcode, operand, writes, irq_writes, first_tick)

        # AMBIGUITY 16 (resolved, spec section 6 Side-set, PIO 3.5.1): side-set wins over OUT/SET/MOV
        pin_writes.extend(writes)
        pin_writes.extend(side_writes)

        # A JMP on an OSR bit counts as an OUT for autopull (spec v0.3 section 6)
        if opcode != OP_OUT and not (opcode == OP_JMP and (operand >> 6) in (7, 15)):
            self._autopull_background()

        if not done:
            self.stalled = True
            return
        self.stalled = False
        # AMBIGUITY 17 (resolved, spec section 6 Wrap, PIO 3.5.2): jump, else wrap at wrap_end, else PC + 1
        if jump is not None:
            self.pc = jump
        elif self.pc == self.cfg.wrap_end:
            self.pc = self.cfg.wrap_target
        else:
            self.pc = (self.pc + 1) % IMEM_SIZE
        self.delay_count = delay + extra
        self._prearm_edge()

    def _prearm_edge(self):
        """Spec v0.3 section 5: an edge-mode WAIT's detector arms when the instruction before it completes."""
        word = self.emu.imem[self.pc]
        if word >> 16 == OP_WAIT and (word >> 8) & 3:
            self.edge_armed = True
            self.edge_prev = self._pin_in(self.cfg.in_base + ((word >> 4) & 7))
            self.edge_seen = False

    def _reserved(self, what):
        raise ReservedEncoding(f"SM{self.index} pc {self.pc}: {what}")

    def _check_zero(self, operand, mask, what):
        # AMBIGUITY 26 (resolved, spec section 4): unused operand bits must be 0
        if operand & mask:
            self._reserved(f"{what}: unused operand bits set ({operand & mask:#05x})")

    def _execute(self, op, operand, writes, irq_writes, first_tick):
        """Run one tick of an instruction. Returns (done, jump_target_or_None, extra_delay_ticks)."""
        cfg = self.cfg

        if op == OP_JMP:
            cond, addr = operand >> 6, operand & 0x3F
            if cond == 0:
                take = True
            elif cond == 1:
                take = self.x == 0
            elif cond == 2:
                # AMBIGUITY 9 (resolved, spec section 5 JMP, PIO 3.4.2): always decrements
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
                take = self._pin_in(cfg.jmp_pin) == 1
            elif cond == 9:
                take = self._pin_in(cfg.jmp_pin) == 0
            elif cond == 10:
                # AMBIGUITY 10 (resolved, spec section 5 JMP, PIO 3.4.2): shift count below pull threshold
                take = self.osr_count < cfg.pull_thresh
            elif cond == 11:
                take = len(self.rx_fifo) == FIFO_DEPTH
            elif cond in (12, 13):
                # spec v0.3 section 5: TO is sticky until a JMP tests it
                take = self.to if cond == 12 else not self.to
                self.to = False
            elif cond == 14:
                take = self.crcz
            elif cond in (7, 15):
                # spec v0.3 section 5: shift one OSR bit out exactly like OUT NULL, 1 (autopull included),
                # and branch on it. The bit is not fed to the CRC.
                if cfg.autopull and self.osr_count >= cfg.pull_thresh:
                    if self.tx_fifo:
                        self.osr, self.osr_count = self.tx_fifo.popleft(), 0
                    return False, None, 0
                _, bits = self._shift_out(1)
                if cfg.autopull and self.osr_count >= cfg.pull_thresh and self.tx_fifo:
                    self.osr, self.osr_count = self.tx_fifo.popleft(), 0
                take = bits[0] == (1 if cond == 7 else 0)
            else:
                self._reserved(f"reserved JMP condition {cond}")
            return True, (addr if take else None), 0

        if op == OP_WAIT:
            mode, pol, timeout = operand >> 8, (operand >> 7) & 1, operand & 1
            if mode < 2:
                self._check_zero(operand, 0b1110, "WAIT pin B in mode 00/01")
            if first_tick and timeout:
                # spec v0.3 section 5: the dedicated counter reloads at the start of each timeout WAIT
                self.tcount = cfg.timeout
            if mode == 0:
                met = self._pin_in(cfg.in_base + ((operand >> 4) & 7)) == pol
            elif not self.edge_armed:
                # Nothing completed just before this WAIT (enable, restart, host jump): arm on its first tick
                self.edge_armed = True
                self.edge_prev = self._pin_in(cfg.in_base + ((operand >> 4) & 7))
                self.edge_seen = False
                met = False
            else:
                met = self.edge_seen
            if met:
                self.edge_armed = False
                return True, None, 0
            if timeout:
                # AMBIGUITY 5 (resolved; spec v0.3 section 5): counter == 0 times out and sets TO (sticky)
                if self.tcount == 0:
                    self.to = True
                    self.edge_armed = False
                    return True, None, 0
                self.tcount -= 1
            return False, None, 0

        if op == OP_IN:
            src, n = operand >> 7, (operand & 0xF) or 16
            self._check_zero(operand, 0x70, "IN")
            if cfg.autopush and self.isr_count + n >= cfg.push_thresh and len(self.rx_fifo) == FIFO_DEPTH:
                # AMBIGUITY 13 (resolved, spec section 6 Autopush, PIO 3.5.4.1): the stalled IN does not
                # shift until the push can happen
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
                self._reserved("reserved IN source 7")
            bits = self._shift_in(value, n)
            if self.tap_in:
                self._crc_feed(bits)
            if cfg.autopush and self.isr_count >= cfg.push_thresh:
                self.rx_fifo.append(self.isr)
                self.isr, self.isr_count = 0, 0
            return True, None, 0

        if op == OP_OUT:
            dst, n = operand >> 7, (operand & 0xF) or 16
            self._check_zero(operand, 0x70, "OUT")
            if dst == 7:
                self._reserved("reserved OUT destination 7")
            if cfg.autopull and self.osr_count >= cfg.pull_thresh:
                # AMBIGUITY 12 (resolved, spec section 6 Autopull, PIO 3.5.4.2): refill if the FIFO has data,
                # and stall this tick either way
                if self.tx_fifo:
                    self.osr, self.osr_count = self.tx_fifo.popleft(), 0
                return False, None, 0
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
            elif dst == 4:
                self._write_pins(writes, cfg.out_base, n, value, "dir")
            elif dst == 5:
                jump = value & (IMEM_SIZE - 1)
            elif dst == 6:
                self.isr, self.isr_count = value, n
            if cfg.autopull and self.osr_count >= cfg.pull_thresh and self.tx_fifo:
                self.osr, self.osr_count = self.tx_fifo.popleft(), 0
            return True, jump, 0

        if op == OP_PUSHPULL:
            is_pull, block, cond = (operand >> 9) & 1, (operand >> 8) & 1, (operand >> 7) & 1
            self._check_zero(operand, 0x7F, "PUSH/PULL")
            # AMBIGUITY 27 (resolved, spec section 5 PUSH/PULL, PIO 3.4.6-3.4.7): if full / if empty compare
            # the shift count with the threshold
            if is_pull:
                if cfg.autopull and self.osr_count == 0:
                    # PIO 3.5.4.2: with autopull, PULL is a no-op while the OSR is full, meaning the output
                    # shift count is 0 (spec section 5)
                    return True, None, 0
                if cond and self.osr_count < cfg.pull_thresh:
                    return True, None, 0
                if not self.tx_fifo:
                    if block:
                        return False, None, 0
                    # AMBIGUITY 11 (resolved, spec section 5 PUSH/PULL, PIO 3.4.7): copies X
                    self.osr, self.osr_count = self.x, 0
                    return True, None, 0
                self.osr, self.osr_count = self.tx_fifo.popleft(), 0
            else:
                if cond and self.isr_count < cfg.push_thresh:
                    return True, None, 0
                if len(self.rx_fifo) == FIFO_DEPTH:
                    if block:
                        return False, None, 0
                    # AMBIGUITY 11 (resolved, spec section 5 PUSH/PULL, PIO 3.4.6): drop, ISR still clears
                else:
                    self.rx_fifo.append(self.isr)
                self.isr, self.isr_count = 0, 0
            return True, None, 0

        if op == OP_MOV:
            # AMBIGUITY 2 (resolved, spec section 5 MOV): PIO's register codes, CRC at 4
            dst, mop, src = operand >> 7, (operand >> 5) & 3, operand & 7
            self._check_zero(operand, 0x18, "MOV")
            if src == 0:
                value = self._read_pins(cfg.in_base, 16)
            elif src == 1:
                value = self.x
            elif src == 2:
                value = self.y
            elif src == 3:
                value = 0                       # NULL (spec section 5 MOV)
            elif src == 4:
                value = self.crc
            elif src == 5:
                # AMBIGUITY 3 (resolved, spec section 5 MOV, PIO 3.4.8): all ones if FIFO level < N
                level = len(self.rx_fifo if cfg.status_rx else self.tx_fifo)
                value = MASK16 if level < cfg.status_n else 0
            elif src == 6:
                value = self.isr
            elif src == 7:
                value = self.osr
            else:
                self._reserved(f"reserved MOV source {src}")
            if mop == 1:
                value = ~value & MASK16
            elif mop == 2:
                value = bitrev16(value)
            elif mop == 3:
                self._reserved("reserved MOV op 3")
            if dst == 0:
                self._write_pins(writes, cfg.out_base, cfg.out_count, value, "out")
            elif dst == 1:
                self.x = value
            elif dst == 2:
                self.y = value
            elif dst == 4:
                self.crc = value & ((1 << cfg.crc_width) - 1)
            elif dst == 6:
                self.isr, self.isr_count = value, 0
            elif dst == 7:
                self.osr, self.osr_count = value, 0
            else:
                self._reserved(f"reserved MOV destination {dst}")
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
                self._reserved(f"reserved SET destination {dst}")
            return True, None, 0

        if op == OP_IRQ:
            irq_op, flag = operand >> 8, operand & 3
            self._check_zero(operand, 0xFC, "IRQ")
            if irq_op == 0:
                irq_writes.append((flag, 1))
            elif irq_op == 1:
                irq_writes.append((flag, 0))
            elif irq_op == 2:
                # AMBIGUITY 18 (resolved, spec section 5 IRQ, PIO 3.4.3): wait-high clears the flag when met
                if self.emu.irq[flag] != 1:
                    return False, None, 0
                irq_writes.append((flag, 0))
            elif self.emu.irq[flag] != 0:
                return False, None, 0
            return True, None, 0

        if op == OP_CRC:
            # AMBIGUITY 14 (resolved, spec section 5 CRC): no move-to-ISR bit; taps persist until the next CRC
            self._check_zero(operand, 0x7F, "CRC")
            if operand & (1 << 9):
                self.crc = cfg.crc_seed & ((1 << cfg.crc_width) - 1)
            self.tap_out = bool(operand & (1 << 8))
            self.tap_in = bool(operand & (1 << 7))
            return True, None, 0

        if op == OP_DELAY:
            # AMBIGUITY 7 (resolved, spec section 5 DELAY): 1 tick plus n, plus its delay field
            return True, None, operand

        self._reserved(f"reserved opcode {op}")


class Emulator:
    """Two SMs, shared instruction memory, shared IRQ flags and the 20-pin protocol pin space."""

    def __init__(self):
        self.imem = [0] * IMEM_SIZE
        self.irq = [0] * NUM_IRQS
        # AMBIGUITY 19 (resolved, spec section 3 Reset state): uo_out drives 0 and uio is released, with
        # output values at 1 so turning on open-drain mode leaves them released.
        self.out_val = [1 if p in UIO_PINS else 0 for p in range(NUM_PINS)]
        self.pindir = [0] * NUM_PINS
        self.open_drain = [0] * len(UIO_PINS)
        # External drivers: None = not driving. Undriven lines read the pull level.
        self.ext = [None] * NUM_PINS
        self.pull = [1] * len(UIO_PINS) + [0] * (NUM_PINS - len(UIO_PINS))
        self.sync1 = [0] * NUM_PINS
        self.sync_in = [0] * NUM_PINS   # second synchronizer flop
        self.sync_bypass = [0] * NUM_PINS
        self.view = list(self.sync_in)  # what the SMs read: sync_in, or the pad where bypassed
        self.sms = [StateMachine(self, i) for i in range(NUM_SMS)]
        self.pad = [self._resolve(p) for p in range(NUM_PINS)]   # output register: 0, 1 or None (released)
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

    def restart(self, sm):
        self.sms[sm].restart()

    def enable(self, sm, restart=True):
        """Enable an SM (spec section 6): restart it (by default), start its divider, write its pin init value."""
        s = self.sms[sm]
        if restart:
            s.restart()
        s.div_count = 0
        s.enabled = True
        for pin in s.pin_group():
            if pin < NUM_PINS and pin not in IN_PINS:
                self.out_val[pin] = (s.cfg.pin_init >> pin) & 1

    def disable(self, sm):
        self.sms[sm].enabled = False

    def enable_mask(self, mask, restart=True):
        """Host Enable mask command (spec sections 6-7): write both SMs' enable bits at once.

        SMs going from disabled to enabled start their dividers on the same clock. SM1's pin init value is
        written first, so SM0's wins where their groups overlap.
        """
        newly = [i for i in range(NUM_SMS) if mask >> i & 1 and not self.sms[i].enabled]
        for i in range(NUM_SMS):
            if not mask >> i & 1:
                self.sms[i].enabled = False
        for i in reversed(newly):
            self.enable(i, restart=restart)

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

    def push_tx_byte(self, sm, byte):
        """Host 1-byte TX write (spec v0.3 section 7): placed where the SM shifts it out first."""
        return self.push_tx(sm, (byte & 0xFF) if self.sms[sm].cfg.out_shift_right else (byte & 0xFF) << 8)

    def pop_rx_byte(self, sm):
        """Host 1-byte RX read (spec v0.3 section 7): the byte from where IN shifted it."""
        w = self.pop_rx(sm)
        if w is None:
            return None
        return (w >> 8) & 0xFF if self.sms[sm].cfg.in_shift_right else w & 0xFF

    def jump(self, sm, addr):
        """Host Jump (spec v0.3 section 6): set PC; cancel a stall, any delay and an armed edge detector."""
        s = self.sms[sm]
        s.pc = addr % IMEM_SIZE
        s.stalled = False
        s.delay_count = 0
        s.edge_armed = False

    def clear_fifos(self, sm):
        """Host Clear FIFOs (spec v0.3 section 6)."""
        self.sms[sm].tx_fifo.clear()
        self.sms[sm].rx_fifo.clear()

    def write_irq(self, clear=0, force=0):
        """Host IRQ access (spec section 7): write-1-to-clear and force-set masks."""
        for i in range(NUM_IRQS):
            if clear >> i & 1:
                self.irq[i] = 0
            if force >> i & 1:
                self.irq[i] = 1

    # ---- pads --------------------------------------------------------------------------------

    def drive(self, pin, value):
        """Drive a pad from outside (a simulated device). None releases it."""
        self.ext[pin] = value

    def chip_drive(self, pin):
        """What the chip drives onto a pad right now: the output register's 0, 1 or None (released)."""
        return self.pad[pin]

    def _resolve(self, pin):
        """The drive the output register will capture at the next edge, from the current pin state."""
        if pin in IN_PINS:
            return None
        if pin in OUT_PINS:
            return self.out_val[pin]
        if self.open_drain[pin]:
            # AMBIGUITY 20 (resolved, spec section 6 Open-drain): PINDIRS has no effect
            return 0 if self.out_val[pin] == 0 else None
        return self.out_val[pin] if self.pindir[pin] else None

    def line(self, pin):
        """Resolved pad level. Drivers wire-AND; an undriven line reads its pull level."""
        if pin in OUT_PINS:
            # AMBIGUITY 23 (resolved, spec section 3): out-only pins read back their driven value
            return self.pad[pin]
        drivers = [d for d in (self.chip_drive(pin), self.ext[pin]) if d is not None]
        if not drivers:
            return self.pull[pin]
        return int(all(drivers))

    # ---- clock -------------------------------------------------------------------------------

    def step(self, n=1):
        for _ in range(n):
            lines = [self.line(p) for p in range(NUM_PINS)]
            # spec v0.3 section 6: a bypassed input is seen straight from the pad
            self.view = [lines[p] if self.sync_bypass[p] else self.sync_in[p] for p in range(NUM_PINS)]
            # Spec section 6, Output register: pads change one edge after the pin state does
            next_pad = [self._resolve(p) for p in range(NUM_PINS)]
            per_sm = []
            for sm in self.sms:
                pin_writes, irq_writes = [], []
                if sm.enabled:
                    sm.edge_clock()
                    sm.div_count += 1
                    if sm.div_count >= sm.cfg.period:
                        sm.div_count = 0
                        sm.tick(pin_writes, irq_writes)
                per_sm.append((pin_writes, irq_writes))
            # AMBIGUITY 25 (resolved, spec section 6 Pin writes): the lower-numbered SM wins, so apply SM1's
            # writes first. Levels and directions are separate, and IRQ flags follow the same rule.
            for pin_writes, irq_writes in reversed(per_sm):
                for kind, pin, value in pin_writes:
                    if pin >= NUM_PINS or pin in IN_PINS:
                        continue
                    if kind == "out":
                        self.out_val[pin] = value
                    elif pin in UIO_PINS:
                        self.pindir[pin] = value
                for flag, value in irq_writes:
                    self.irq[flag] = value
            self.pad = next_pad
            self.sync_in = self.sync1
            self.sync1 = lines
            self.cycle += 1

    def run_until(self, cond, max_cycles=1_000_000):
        for _ in range(max_cycles):
            if cond():
                return
            self.step()
        raise TimeoutError(f"condition not met within {max_cycles} cycles")
