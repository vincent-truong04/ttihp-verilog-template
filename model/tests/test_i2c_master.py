"""Run programs/i2c_master.pasm against a simulated I2C target on an open-drain, pulled-up bus."""

from pathlib import Path

import pytest

from model.asm import assemble_file
from model.devices import Bench, I2CTarget
from model.sim import FIFO_DEPTH, Emulator, SMConfig

PROGRAM = Path(__file__).resolve().parent.parent / "programs" / "i2c_master.pasm"
SDA, SCL = 0, 1             # uio[0], uio[1]
ADDR = 0x50
TIMEOUT = 2000              # timeout reload value: each WAIT gives up after TIMEOUT + 1 ticks

START, STOP = 0x4000, 0x0000


def wr(byte):
    return 0x8000 | byte << 7 | 1 << 6 | 1 << 5       # release the ACK slot; a NACK is an error


def rd(ack):
    return 0x8000 | 0xFF << 7 | (0 if ack else 1) << 6  # release SDA; ACK or NACK it ourselves


def run(commands, target, clkdiv=1, max_cycles=200_000, timeout=TIMEOUT):
    """Run commands; return (emu, received bytes, errors). The host recovers from errors as the
    program header says: clear the FIFOs, drop the rest of the transaction, clear the IRQ flag and
    jump the SM to do_stop. If the recovery STOP fails too, the host gives up and leaves it halted."""
    prog = assemble_file(PROGRAM)
    emu = Emulator()
    emu.load(prog.words)
    emu.open_drain[SDA] = emu.open_drain[SCL] = 1
    emu.configure(0, SMConfig(
        clkdiv=clkdiv, start=prog.labels["fetch"], wrap_target=prog.wrap_target, wrap_end=prog.wrap,
        out_base=SDA, out_count=1, set_base=SDA, set_count=1, sideset_base=SCL, sideset_count=1,
        in_base=SDA, jmp_pin=SDA, out_shift_right=False, in_shift_right=False,
        autopush=True, push_thresh=8, pin_init=(1 << SDA) | (1 << SCL), timeout=timeout,
    ))
    bench = Bench(emu, target)
    sm, labels = emu.sms[0], prog.labels
    pending, sent, received, errors = list(commands), [], [], []
    state = {"recovering": False, "gave_up": False}

    def host():
        if sm.pc == labels["halt"] and (emu.irq[0] or emu.irq[1]) and not state["gave_up"]:
            errors.append("nack" if emu.irq[0] else "timeout")
            if state["recovering"]:
                state["gave_up"] = True                   # the recovery STOP failed as well
                return
            # The host knows what it sent and, from the status read, how many words are still
            # queued; those were not consumed. Drop the rest of the failed transaction (unless the
            # SM already took its STOP) and re-queue everything after it.
            level = len(sm.tx_fifo)
            consumed, unconsumed = sent[:len(sent) - level], sent[len(sent) - level:]
            emu.clear_fifos(0)
            rest = unconsumed + pending
            if not consumed or consumed[-1] != STOP:
                while rest and rest.pop(0) != STOP:
                    pass
            pending[:] = rest
            sent.clear()
            emu.write_irq(clear=0b11)
            emu.jump(0, labels["do_stop"])
            state["recovering"] = True
            return
        if sm.pc == labels["fetch"]:
            state["recovering"] = False
        while pending and len(sm.tx_fifo) < FIFO_DEPTH:
            sent.append(pending[0])
            emu.push_tx(0, pending.pop(0))
        while (b := emu.pop_rx_byte(0)) is not None:
            received.append(b)

    bench.hooks.append(host)
    bench.step(4)
    emu.enable(0)
    done = lambda: state["gave_up"] or (not pending and not sm.tx_fifo and sm.pc == labels["fetch"] and sm.stalled)
    bench.run_until(done, max_cycles=max_cycles)
    bench.step(10)
    emu.errors = errors
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


def test_nack_on_bad_address_halts_with_irq0_and_host_recovers_with_stop():
    t = I2CTarget(ADDR)
    emu, received = run([START, wr(0x51 << 1), wr(0x99), wr(0x98), STOP], t)
    assert emu.errors == ["nack"]
    assert t.written == []
    assert received == [0x51 << 1]                            # the data bytes were never sent
    assert t.events == ["START", "ADDR 0x51 ignored", "STOP"]  # the STOP still ran
    assert bus_idle(emu)


def test_next_transaction_works_after_a_nack():
    t = I2CTarget(ADDR)
    cmds = [START, wr(0x51 << 1), wr(0x99), STOP, START, wr(ADDR << 1), wr(0x77), STOP]
    emu, received = run(cmds, t)
    assert emu.errors == ["nack"]
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


def test_stretching_longer_than_the_timeout_times_out():
    t = I2CTarget(ADDR, stretch=TIMEOUT + 1000)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), STOP], t)
    assert emu.errors[0] == "timeout"
    assert t.written == []             # the stretch starts after the address ACK; the data byte fails


def test_stretching_just_under_the_timeout_is_fine():
    t = I2CTarget(ADDR, stretch=TIMEOUT - 50)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), STOP], t)
    assert emu.errors == [] and t.written == [0x11]


@pytest.mark.parametrize("stuck", ["sda", "scl"])
def test_stuck_low_bus_times_out_instead_of_hanging(stuck):
    t = I2CTarget(ADDR, stuck_sda=stuck == "sda", stuck_scl=stuck == "scl")
    cmds = [START, wr(ADDR << 1), wr(0x11), STOP]
    emu, received = run(cmds, t, max_cycles=20 * TIMEOUT)
    # the START times out, the host's recovery STOP times out too, and the SM is left halted
    assert emu.errors == ["timeout", "timeout"]
    assert emu.sms[0].pc == assemble_file(PROGRAM).labels["halt"]
    assert t.written == []
    assert emu.cycle < 5 * (TIMEOUT + 1) + 500       # a few timeouts, not a hang


def test_stuck_sda_is_caught_at_start_before_any_byte_is_clocked():
    # the START's qualified-edge WAIT sees no SDA fall, so no address or data bits go out:
    # the only SCL rises are each STOP's (SCL is already high at the first START)
    t = I2CTarget(ADDR, stuck_sda=True)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), STOP], t, max_cycles=20 * TIMEOUT)
    assert emu.errors[0] == "timeout"
    assert t.scl_rises == 1 and received == []      # only the recovery STOP's SCL rise


def test_start_and_stop_are_confirmed_by_qualified_edge_waits_at_any_clkdiv():
    # the side-set WAIT makes the edge itself, so it is never missed, even with a slow tick
    # spec v0.3: the detector arms when the SET before the WAIT completes, so the edge that SET
    # causes is never missed, even when the 3-clock round trip is longer than a tick
    for clkdiv in (1, 3, 8, 20):
        t = I2CTarget(ADDR)
        emu, received = run([START, wr(ADDR << 1), wr(0xC3), STOP], t, clkdiv=clkdiv)
        assert t.written == [0xC3] and emu.errors == [], clkdiv
        assert t.events == ["START", f"ADDR 0x{ADDR:02x} W", "STOP"], clkdiv


def test_timeout_in_the_ack_slot_is_reported_as_a_timeout_not_a_nack():
    # The target stretches SCL past the timeout right before the ACK clock and then does not ACK:
    # SDA is high in the ACK slot, but SCL never went high, so that "NACK" means nothing.
    t = I2CTarget(ADDR, stretch_before_ack=TIMEOUT + 500, nack_writes=True)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), STOP], t)
    assert emu.errors[0] == "timeout"


def test_target_refusing_data_is_a_nack():
    t = I2CTarget(ADDR, nack_writes=True)
    emu, received = run([START, wr(ADDR << 1), wr(0x11), wr(0x22), STOP], t)
    assert emu.errors == ["nack"] and "NACK data" in t.events
    assert bus_idle(emu)
