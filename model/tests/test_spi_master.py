"""Run programs/spi_master.pasm against a simulated mode 0 SPI slave that echoes the previous byte."""

import random
from pathlib import Path

import pytest

from model.asm import assemble_file
from model.devices import Bench, SpiEchoSlave
from model.sim import FIFO_DEPTH, Emulator, SMConfig

PROGRAM = Path(__file__).resolve().parent.parent / "programs" / "spi_master.pasm"
SCK, MOSI, MISO = 8, 9, 15      # uo_out[0], uo_out[1], ui_in[3]


def transfer(data, clkdiv, first=0x00, bypass_miso=False):
    prog = assemble_file(PROGRAM)
    emu = Emulator()
    emu.load(prog.words)
    emu.configure(0, SMConfig(
        clkdiv=clkdiv, wrap_target=prog.wrap_target, wrap_end=prog.wrap,
        out_base=MOSI, out_count=1, sideset_base=SCK, sideset_count=1, in_base=MISO,
        out_shift_right=False, in_shift_right=False,
        autopull=True, pull_thresh=8, autopush=True, push_thresh=8,
    ))
    emu.sync_bypass[MISO] = int(bypass_miso)
    slave = SpiEchoSlave(SCK, MOSI, MISO, first=first)
    bench = Bench(emu, slave)
    pending, received = list(data), []

    def host():
        while pending and len(emu.sms[0].tx_fifo) < FIFO_DEPTH:
            emu.push_tx_byte(0, pending.pop(0))       # spec v0.3: placed in [15:8] for a left shift
        while (b := emu.pop_rx_byte(0)) is not None:
            received.append(b)

    bench.hooks.append(host)
    bench.step(4)
    emu.enable(0)
    bench.run_until(lambda: len(received) == len(data), max_cycles=40 * clkdiv * (len(data) + 4))
    bench.step(8 * clkdiv)    # the last byte ends on the falling edge the next (stalled) OUT makes
    return received, slave, emu


@pytest.mark.parametrize("clkdiv", [2, 3, 8])
def test_full_duplex_echo(clkdiv):
    rng = random.Random(clkdiv)
    data = [0xA5, 0x00, 0xFF, 0x01, 0x80] + [rng.randrange(256) for _ in range(30)]
    received, slave, emu = transfer(data, clkdiv, first=0x3C)
    assert slave.received == data                     # slave got every MOSI byte
    assert received == [0x3C] + data[:-1]             # and echoed each one a byte later
    assert slave.mosi_glitches == 0                   # MOSI never changes while SCK is high
    assert emu.line(SCK) == 0                         # idles low between transfers (mode 0)


def test_sck_is_continuous_while_the_fifo_has_data():
    clkdiv = 2
    received, slave, emu = transfer(list(range(12)), clkdiv)
    gaps = {b - a for a, b in zip(slave.rising_cycles, slave.rising_cycles[1:])}
    assert gaps == {4 * clkdiv}                       # SCK = f_clk / (4 * clkdiv), no gaps between bytes


def test_sck_stays_low_while_stalled_on_an_empty_fifo():
    received, slave, emu = transfer([0x12], 2)
    for _ in range(100):
        emu.step()
        assert emu.line(SCK) == 0
    assert emu.sms[0].stalled


def test_clkdiv_1_is_too_fast_for_the_miso_round_trip():
    # Documents a limit, not a bug. At clkdiv 1 the IN samples MISO 2 ticks after the falling edge
    # that changed it, but the change needs 1 clock (output register) + 1 clock (slave) + 2 clocks
    # (synchronizer) to come back. The slave receives correctly; the master reads stale MISO bits.
    data = [0xA5, 0x5A, 0xF0, 0x0F]
    received, slave, emu = transfer(data, 1, first=0x3C)
    assert slave.received == data
    assert received != [0x3C] + data[:-1]


@pytest.mark.parametrize("clkdiv", [1, 2])
def test_bypassing_the_miso_synchronizer_allows_clkdiv_1(clkdiv):
    # spec v0.3 section 6: MISO is synchronous to our own SCK, so bypassing its synchronizer is safe,
    # and removes 2 clocks from the round trip: full duplex at SCK = f/4
    rng = random.Random(11)
    data = [rng.randrange(256) for _ in range(20)]
    received, slave, emu = transfer(data, clkdiv, first=0x3C, bypass_miso=True)
    assert slave.received == data
    assert received == [0x3C] + data[:-1]
