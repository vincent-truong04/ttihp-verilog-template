"""Simulated external devices for testing protocol programs on the model.

A device's `clock(emu)` runs after every `Emulator.step()`. It sees the resolved pad levels from
`emu.line()` and drives pads with `emu.drive()`; what it drives takes effect from the next step.
`Bench` steps the emulator and its devices together.
"""


def _od(bit):
    """Open-drain drive for a data bit: 0 pulls low, 1 releases (None)."""
    return None if bit else 0


class Bench:
    def __init__(self, emu, *devices):
        self.emu = emu
        self.devices = list(devices)
        self.hooks = []          # extra callables run every clock, e.g. a host draining the RX FIFO

    def step(self, n=1):
        for _ in range(n):
            self.emu.step()
            for d in self.devices:
                d.clock(self.emu)
            for h in self.hooks:
                h()

    def run_until(self, cond, max_cycles):
        for _ in range(max_cycles):
            if cond():
                return
            self.step()
        raise TimeoutError(f"condition not met within {max_cycles} cycles")


class Wire:
    """Connects one pad to another, e.g. a TX output looped back to an RX input."""

    def __init__(self, src, dst):
        self.src, self.dst = src, dst

    def clock(self, emu):
        emu.drive(self.dst, emu.line(self.src))


class UartSender:
    """8N1 UART transmitter on one pad. bit_clocks may be fractional, to model baud-rate error."""

    def __init__(self, pin, bit_clocks):
        self.pin = pin
        self.bit_clocks = bit_clocks
        self.edges = []          # (cycle, level), in time order
        self.level = 1
        self.next_free = 0.0

    def queue(self, data, start=None, gap_bits=0.0, bad_stop=False):
        """Queue bytes back to back, from `start` (default: when the line is next free)."""
        t = max(self.next_free, 0.0) if start is None else start
        for byte in data:
            bits = [0] + [(byte >> i) & 1 for i in range(8)] + [0 if bad_stop else 1]
            for i, b in enumerate(bits):
                self.edges.append((t + i * self.bit_clocks, b))
            t += (10 + gap_bits) * self.bit_clocks
            if bad_stop:
                self.edges.append((t, 1))           # back to idle after the bad frame
        self.next_free = t
        return t

    def clock(self, emu):
        while self.edges and self.edges[0][0] <= emu.cycle:
            self.level = self.edges.pop(0)[1]
        emu.drive(self.pin, self.level)


class SpiEchoSlave:
    """SPI mode 0 slave that sends back the previous byte it received (the first reply is `first`).

    Samples MOSI on SCK rising, changes MISO on SCK falling. Records any MOSI change while SCK is high.
    """

    def __init__(self, sck, mosi, miso, first=0x00):
        self.sck, self.mosi, self.miso = sck, mosi, miso
        self.tx = first
        self.rx = 0
        self.bits = 0
        self.miso_bit = (first >> 7) & 1      # changes only on SCK falling edges (mode 0)
        self.received = []
        self.prev = None
        self.mosi_glitches = 0
        self.rising_cycles = []

    def clock(self, emu):
        sck, mosi = emu.line(self.sck), emu.line(self.mosi)
        if self.prev is not None:
            psck, pmosi = self.prev
            if not psck and sck:
                self.rx = (self.rx << 1) | mosi
                self.bits += 1
                self.rising_cycles.append(emu.cycle)
            elif psck and not sck:
                if self.bits == 8:
                    self.received.append(self.rx)
                    self.tx, self.rx, self.bits = self.rx, 0, 0
                self.miso_bit = (self.tx >> (7 - self.bits)) & 1
            elif psck and sck and mosi != pmosi:
                self.mosi_glitches += 1
        self.prev = (sck, mosi)
        emu.drive(self.miso, self.miso_bit)


class I2CTarget:
    """I2C target on an open-drain bus. Writes go to `written`; reads come from `read_data`.

    Options: `stretch` holds SCL low for that many clocks after every byte's ACK clock,
    `stretch_bits` after every data bit too, and `stretch_before_ack` just before each written data
    byte's ACK clock.
    `nack_writes` makes it refuse (NACK) every data byte written to it.
    `stuck_sda` / `stuck_scl` hold a line low forever.
    """

    def __init__(self, address, sda=0, scl=1, read_data=(), stretch=0, stretch_bits=0,
                 stretch_before_ack=0, nack_writes=False, stuck_sda=False, stuck_scl=False):
        self.address = address
        self.sda, self.scl = sda, scl
        self.read_data = list(read_data)
        self.stretch, self.stretch_bits = stretch, stretch_bits
        self.stretch_before_ack, self.nack_writes = stretch_before_ack, nack_writes
        self.stuck_sda, self.stuck_scl = stuck_sda, stuck_scl
        self.written = []
        self.events = []         # "START", "STOP", "ADDR 0x50 W", "NACK master", ...
        self.state = "idle"
        self.shift = self.bits = 0
        self.rw = 0
        self.sda_out = None      # None = released
        self.hold = 0            # clocks left to hold SCL low
        self.stretched = 0       # total clocks spent stretching
        self.scl_rises = 0       # SCL rising edges seen on the bus
        self.prev = None

    def _start(self):
        self.events.append("START")
        self.state, self.shift, self.bits, self.sda_out = "addr", 0, 0, None

    def _rise(self, sda):
        if self.state in ("addr", "write"):
            self.shift = (self.shift << 1) | sda
            self.bits += 1
        elif self.state == "read":
            self.bits += 1
        elif self.state == "read_ack":
            self.master_ack = sda == 0
            self.events.append("ACK master" if self.master_ack else "NACK master")
        if self.state in ("addr_ack", "write_ack", "read_ack"):
            self.seen_ack_clock = True

    def _fall(self):
        st = self.state
        if st in ("addr", "write") and self.bits == 8:
            if st == "addr":
                addr, self.rw = self.shift >> 1, self.shift & 1
                if addr != self.address:
                    self.events.append(f"ADDR 0x{addr:02x} ignored")
                    self.state = "ignore"
                    return
                self.events.append(f"ADDR 0x{addr:02x} {'R' if self.rw else 'W'}")
                self.state = "addr_ack"
            else:
                self.written.append(self.shift & 0xFF)
                self.state = "write_ack"
            self.sda_out, self.seen_ack_clock = 0, False
            if st == "write" and self.nack_writes:
                self.written.pop()
                self.events.append("NACK data")
                self.sda_out = None
            if st == "write":
                self.hold = self.stretch_before_ack
        elif st in ("addr_ack", "write_ack") and self.seen_ack_clock:
            self.sda_out = None
            self.hold = self.stretch
            if st == "addr_ack" and self.rw:
                self._load_read_byte()
            else:
                self.state, self.shift, self.bits = "write", 0, 0
        elif st == "read":
            if self.bits == 8:
                self.sda_out, self.state, self.seen_ack_clock = None, "read_ack", False
            else:
                self.sda_out = _od((self.cur >> (7 - self.bits)) & 1)
                self.hold = self.stretch_bits
        elif st == "read_ack" and self.seen_ack_clock:
            self.hold = self.stretch
            if self.master_ack:
                self._load_read_byte()
            else:
                self.state = "ignore"
        elif st in ("addr", "write"):
            self.hold = self.stretch_bits

    def _load_read_byte(self):
        self.cur = self.read_data.pop(0) if self.read_data else 0xFF
        self.state, self.bits = "read", 0
        self.sda_out = _od((self.cur >> 7) & 1)

    def clock(self, emu):
        sda, scl = emu.line(self.sda), emu.line(self.scl)
        if self.prev is not None:
            psda, pscl = self.prev
            if pscl and scl and psda and not sda:
                self._start()
            elif pscl and scl and not psda and sda:
                self.events.append("STOP")
                self.state, self.sda_out = "idle", None
            elif not pscl and scl:
                self.scl_rises += 1
                self._rise(sda)
            elif pscl and not scl:
                self._fall()
        self.prev = (sda, scl)
        if self.hold:
            self.hold -= 1
            self.stretched += 1
        emu.drive(self.sda, 0 if self.stuck_sda else (0 if self.sda_out == 0 else None))
        emu.drive(self.scl, 0 if (self.stuck_scl or self.hold) else None)
