"""Load all four programs into the 64-slot memory at once and run them in pairs on the two SMs."""

import random
from pathlib import Path

import pytest

from model.asm import assemble, assemble_file, relocate
from model.devices import Bench, I2CTarget, SpiEchoSlave, Wire
from model.sim import FIFO_DEPTH, IMEM_SIZE, Emulator, SMConfig

PROGRAMS = Path(__file__).resolve().parent.parent / "programs"
ORDER = ["uart_tx", "uart_rx", "spi_master", "i2c_master"]


def load_all():
    emu, layout, base = Emulator(), {}, 0
    for name in ORDER:
        prog = assemble_file(PROGRAMS / f"{name}.pasm")
        emu.load(relocate(prog.words, base), base)
        layout[name] = (base, prog)
        base += len(prog.words)
    return emu, layout, base


def config(layout, name, **kw):
    base, prog = layout[name]
    entry = prog.labels.get("fetch", 0)
    return SMConfig(start=base + entry, wrap_target=base + prog.wrap_target, wrap_end=base + prog.wrap, **kw)


def test_relocate_moves_only_jmp_targets():
    prog = assemble("top: set x, 3\nloop: jmp x--, loop\njmp top")
    words = relocate(prog.words, 40)
    assert words[0] == prog.words[0]
    assert words[1] & 0x3F == 41 and words[2] & 0x3F == 40
    assert words[1] >> 6 == prog.words[1] >> 6


def test_all_four_fit():
    emu, layout, used = load_all()
    assert used == 4 + 9 + 2 + 29 == 44 and used <= IMEM_SIZE


def _host(emu, sm, pending, received, shift=0, mask=0xFF):
    def host():
        while pending and len(emu.sms[sm].tx_fifo) < FIFO_DEPTH:
            emu.push_tx(sm, pending.pop(0))
        while (w := emu.pop_rx(sm)) is not None:
            received.append((w >> shift) & mask)
    return host


def test_uart_tx_to_uart_rx_loopback_on_two_sms():
    emu, layout, _ = load_all()
    tx, rx = 8, 15
    emu.configure(0, config(layout, "uart_tx", clkdiv=3, out_base=tx, out_count=1, sideset_base=tx,
                            sideset_count=1, pin_init=1 << tx))
    emu.configure(1, config(layout, "uart_rx", clkdiv=3, in_base=rx, jmp_pin=rx))
    rng = random.Random(1)
    data = [rng.randrange(256) for _ in range(20)]
    pending, received = list(data), []
    bench = Bench(emu, Wire(tx, rx))
    bench.hooks += [_host(emu, 0, pending, []), _host(emu, 1, [], received, shift=8)]
    # TX first: uo_out resets low and the pin init value reaches the pad one edge after enable, so an
    # RX enabled on the same clock would take that reset-low level for a start bit
    emu.enable(0)
    bench.step(4)
    emu.enable(1)
    bench.run_until(lambda: len(received) == len(data), max_cycles=400 * len(data))
    assert received == data and emu.irq[0] == 0


def test_spi_and_i2c_masters_at_the_same_time():
    emu, layout, _ = load_all()
    sck, mosi, miso, sda, scl = 10, 11, 16, 0, 1
    emu.open_drain[sda] = emu.open_drain[scl] = 1
    emu.configure(0, config(layout, "spi_master", clkdiv=2, out_base=mosi, out_count=1,
                            sideset_base=sck, sideset_count=1, in_base=miso,
                            out_shift_right=False, in_shift_right=False,
                            autopull=True, pull_thresh=8, autopush=True, push_thresh=8))
    emu.configure(1, config(layout, "i2c_master", out_base=sda, out_count=1, set_base=sda, set_count=1,
                            sideset_base=scl, sideset_count=1, in_base=sda, jmp_pin=sda,
                            out_shift_right=False, in_shift_right=False, autopush=True, push_thresh=8,
                            pin_init=(1 << sda) | (1 << scl), timeout=2000))
    spi_data = list(range(0x30, 0x40))
    spi_pending, spi_rx = [b << 8 for b in spi_data], []
    wr = lambda b: 0x8000 | b << 7 | 1 << 6 | 1 << 5
    i2c_pending, i2c_rx = [0x4000, wr(0x50 << 1), wr(0xAB), wr(0xCD), 0x0000], []
    slave, target = SpiEchoSlave(sck, mosi, miso), I2CTarget(0x50, sda=sda, scl=scl)
    bench = Bench(emu, slave, target)
    bench.hooks += [_host(emu, 0, spi_pending, spi_rx), _host(emu, 1, i2c_pending, i2c_rx)]
    bench.step(4)
    emu.enable_mask(0b11)
    bench.run_until(lambda: len(spi_rx) == len(spi_data) and target.events[-1:] == ["STOP"],
                    max_cycles=20_000)
    assert spi_rx == [0x00] + spi_data[:-1]
    assert target.written == [0xAB, 0xCD] and emu.irq[:2] == [0, 0]
