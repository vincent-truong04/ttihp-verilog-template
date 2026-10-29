"""Run programs/i2c_master.pasm against a simulated I2C target on an open-drain, pulled-up bus."""

from pathlib import Path

import pytest

from model.asm import assemble_file
from model.devices import Bench, I2CTarget
from model.sim import FIFO_DEPTH, Emulator, SMConfig

PROGRAM = Path(__file__).resolve().parent.parent / "programs" / "i2c_master.pasm"
SDA, SCL = 0, 1             # uio[0], uio[1]
ADDR = 0x50
TIMEOUT_TICKS = 65536       # START loads Y = 0xFFFF

START, STOP = 0x4000, 0x0000


def wr(byte):
    return 0x8000 | byte << 7 | 1 << 6 | 1 << 5       # release the ACK slot; a NACK is an error


def rd(ack):
    return 0x8000 | 0xFF << 7 | (0 if ack else 1) << 6  # release SDA; ACK or NACK it ourselves


def run(commands, target, clkdiv=1, max_cycles=200_000):
    prog = assemble_file(PROGRAM)
    emu = Emulator()
    emu.load(prog.words)
    emu.open_drain[SDA] = emu.open_drain[SCL] = 1
    emu.configure(0, SMConfig(
        clkdiv=clkdiv, wrap_target=prog.wrap_target, wrap_end=prog.wrap,
        out_base=SDA, out_count=1, set_base=SCL, set_count=1, sideset_base=SDA, sideset_count=1,
        in_base=SDA, jmp_pin=SDA, out_shift_right=False, in_shift_right=False,
        autopush=True, push_thresh=8, pin_init=(1 << SDA) | (1 << SCL),
    ))
    bench = Bench(emu, target)
    pending, received = list(commands), []

    def host():
        while pending and len(emu.sms[0].tx_fifo) < FIFO_DEPTH:
            emu.push_tx(0, pending.pop(0))
        while (w := emu.pop_rx(0)) is not None:
            received.append(w & 0xFF)

    bench.hooks.append(host)
    bench.step(4)
    emu.enable(0)
    sm, fetch = emu.sms[0], prog.labels["fetch"]
    bench.run_until(lambda: not pending and not sm.tx_fifo and sm.pc == fetch and sm.stalled,
                    max_cycles=max_cycles)
    bench.step(10)
    return emu, received


def bus_idle(emu):
    return emu.line(SDA) == 1 and emu.line(SCL) == 1


def test_write():
    t = I2CTarget(ADDR)
    emu, received = run([START, wr(ADDR << 1), wr(0x12), wr(0x34), wr(0x56), STOP], t)
    assert t.written == [0x12, 0x34, 0x56]
    assert t.events == ["START", f"ADDR 0x{ADDR:02x} W", "STOP"]
    assert received == [ADDR << 1, 0x12, 0x34, 0x56]          # every byte on the bus is pushed
    assert emu.irq[:2] == [0, 0] and bus_idle(emu)


def test_read_acks_all_but_the_last_byte():
    t = I2CTarget(ADDR, read_data=[0xDE, 0xAD, 0xBE])
    emu, received = run([START, wr(ADDR << 1 | 1), rd(True), rd(True), rd(False), STOP], t)
    assert received == [ADDR << 1 | 1, 0xDE, 0xAD, 0xBE]
    assert t.events == ["START", f"ADDR 0x{ADDR:02x} R", "ACK master", "ACK master", "NACK master", "STOP"]
    assert emu.irq[:2] == [0, 0] and bus_idle(emu)


def test_register_read_with_repeated_start():
    t = I2CTarget(ADDR, read_data=[0x42, 0x43])
    cmds = [START, wr(ADDR << 1), wr(0x07), START, wr(ADDR << 1 | 1), rd(True), rd(False), STOP]
    emu, received = run(cmds, t)
    assert t.written == [0x07]
    assert received[-2:] == [0x42, 0x43]
    assert t.events.count("START") == 2 and t.events[-1] == "STOP"
    assert emu.irq[:2] == [0, 0] and bus_idle(emu)


def test_nack_on_bad_address_skips_to_stop_and_raises_irq0():
    t = I2CTarget(ADDR)
    emu, received = run([START, wr(0x51 << 1), wr(0x99), wr(0x98), STOP], t)
    assert emu.irq[0] == 1 and emu.irq[1] == 0
    assert t.written == []
    assert received == [0x51 << 1]                            # the data bytes were never sent
    assert t.events == ["START", "ADDR 0x51 ignored", "STOP"]  # the STOP still ran
    assert bus_idle(emu)


def test_next_transaction_works_after_a_nack():
    t = I2CTarget(ADDR)
    cmds = [START, wr(0x51 << 1), wr(0x99), STOP, START, wr(ADDR << 1), wr(0x77), STOP]
    emu, received = run(cmds, t)
    assert emu.irq[0] == 1
    assert t.written == [0x77]
    assert bus_idle(emu)


@pytest.mark.parametrize("stretch,stretch_bits", [(300, 0), (0, 40), (500, 25)])
def test_clock_stretching(stretch, stretch_bits):
    t = I2CTarget(ADDR, read_data=[0x5A], stretch=stretch, stretch_bits=stretch_bits)
    cmds = [START, wr(ADDR << 1), wr(0x11), wr(0x22), START, wr(ADDR << 1 | 1), rd(False), STOP]
    emu, received = run(cmds, t)
    assert t.stretched > 0
    assert t.written == [0x11, 0x22]
    assert received[-1] == 0x5A
    assert emu.irq[:2] == [0, 0] and bus_idle(emu)


def test_stretching_longer_than_the_budget_times_out():
    t = I2CTarget(ADDR, stretch=TIMEOUT_TICKS + 1000)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), STOP], t, max_cycles=300_000)
    assert emu.irq[1] == 1


@pytest.mark.parametrize("stuck", ["sda", "scl"])
def test_stuck_low_bus_times_out_instead_of_hanging(stuck):
    t = I2CTarget(ADDR, stuck_sda=stuck == "sda", stuck_scl=stuck == "scl")
    cmds = [START, wr(ADDR << 1), wr(0x11), STOP, START, wr(ADDR << 1), STOP]
    emu, received = run(cmds, t, max_cycles=3 * TIMEOUT_TICKS)
    # run() returning at all means the SM got back to `fetch` with every command consumed
    assert emu.irq[1] == 1
    assert t.written == []
    assert emu.cycle < 2 * TIMEOUT_TICKS + 5000      # one budget per transaction, then fail fast


def test_stuck_sda_is_caught_at_start_before_any_byte_is_clocked():
    # the START's qualified-edge WAIT sees no SDA fall, so no address or data bits go out:
    # the only SCL rises are each STOP's (SCL is already high at the first START)
    t = I2CTarget(ADDR, stuck_sda=True)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), STOP], t, max_cycles=2 * TIMEOUT_TICKS)
    assert emu.irq[1] == 1
    assert t.scl_rises == 1 and received == []


def test_start_and_stop_are_confirmed_by_qualified_edge_waits_at_any_clkdiv():
    # the side-set WAIT makes the edge itself, so it is never missed, even with a slow tick
    for clkdiv in (1, 3, 8):
        t = I2CTarget(ADDR)
        emu, received = run([START, wr(ADDR << 1), wr(0xC3), STOP], t, clkdiv=clkdiv)
        assert t.written == [0xC3] and emu.irq[:2] == [0, 0], clkdiv
