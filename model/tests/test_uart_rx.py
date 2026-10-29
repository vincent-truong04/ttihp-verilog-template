"""Run programs/uart_rx.pasm against a simulated UART sender."""

import random
from pathlib import Path

import pytest

from model.asm import assemble_file
from model.devices import Bench, UartSender
from model.sim import Emulator, SMConfig

PROGRAM = Path(__file__).resolve().parent.parent / "programs" / "uart_rx.pasm"
RX_PIN = 15            # P15 = ui_in[3]
TICKS_PER_BIT = 8


def start_uart_rx(clkdiv, sender):
    prog = assemble_file(PROGRAM)
    emu = Emulator()
    emu.load(prog.words)
    emu.configure(0, SMConfig(
        clkdiv=clkdiv, wrap_target=prog.wrap_target, wrap_end=prog.wrap,
        in_base=RX_PIN, jmp_pin=RX_PIN, in_shift_right=True,
    ))
    bench = Bench(emu, sender)
    received = []
    bench.hooks.append(lambda: received.extend(_drain(emu)))
    bench.step(4)            # line idle high through the synchronizer before the SM starts
    emu.enable(0)
    return emu, bench, received


def _drain(emu):
    while (w := emu.pop_rx(0)) is not None:
        yield w >> 8         # the byte arrives in bits [15:8]


def receive(clkdiv, data, baud_error=0.0, start_offset=0.0):
    bit = TICKS_PER_BIT * clkdiv * (1 + baud_error)
    sender = UartSender(RX_PIN, bit)
    emu, bench, received = start_uart_rx(clkdiv, sender)
    end = sender.queue(data, start=emu.cycle + 10 + start_offset)
    bench.run_until(lambda: emu.cycle > end + 2 * bit, max_cycles=int(end + 3 * bit))
    return emu, received


@pytest.mark.parametrize("clkdiv", [2, 4, 7])
def test_back_to_back_random_bytes(clkdiv):
    rng = random.Random(clkdiv)
    data = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80] + [rng.randrange(256) for _ in range(40)]
    emu, received = receive(clkdiv, data)
    assert received == data
    assert emu.irq[0] == 0


@pytest.mark.parametrize("baud_error", [-0.02, +0.02])
def test_sender_baud_off_by_2_percent(baud_error):
    # back-to-back frames, so the error accumulates across the whole frame and the gap is zero
    rng = random.Random(7)
    data = [0x00, 0xFF] + [rng.randrange(256) for _ in range(40)]
    emu, received = receive(4, data, baud_error=baud_error)
    assert received == data
    assert emu.irq[0] == 0


@pytest.mark.parametrize("offset", [0.0, 0.3, 1.7, 3.5, 5.9])
def test_start_bit_at_any_phase_of_the_tick(offset):
    emu, received = receive(6, [0xA5, 0x3C], start_offset=offset)
    assert received == [0xA5, 0x3C]


def test_real_115200_at_50mhz():
    # sender at exactly 115200 baud (434.03 clocks/bit); receiver at clkdiv 54 (432 clocks/bit)
    data = [0x48, 0x69, 0x21]
    sender = UartSender(RX_PIN, 50e6 / 115200)
    emu, bench, received = start_uart_rx(54, sender)
    end = sender.queue(data, start=emu.cycle + 100)
    bench.run_until(lambda: emu.cycle > end + 1000, max_cycles=int(end + 2000))
    assert received == data


def test_framing_error_raises_irq_and_drops_the_byte():
    bit = TICKS_PER_BIT * 4
    sender = UartSender(RX_PIN, bit)
    emu, bench, received = start_uart_rx(4, sender)
    t = sender.queue([0x11], start=emu.cycle + 10)
    t = sender.queue([0x22], start=t + bit, bad_stop=True)       # stop bit low
    end = sender.queue([0x33], start=t + 3 * bit)
    bench.run_until(lambda: emu.cycle > end + 2 * bit, max_cycles=int(end + 3 * bit))
    assert received == [0x11, 0x33]
    assert emu.irq[0] == 1


def test_break_waits_for_idle():
    # a long break (line low for 3 frames): one framing error, nothing pushed, then recovery
    bit = TICKS_PER_BIT * 4
    sender = UartSender(RX_PIN, bit)
    emu, bench, received = start_uart_rx(4, sender)
    t0 = emu.cycle + 10
    sender.edges += [(t0, 0), (t0 + 30 * bit, 1)]
    end = sender.queue([0x5A], start=t0 + 32 * bit)
    bench.run_until(lambda: emu.cycle > end + 2 * bit, max_cycles=int(end + 3 * bit))
    assert received == [0x5A]
    assert emu.irq[0] == 1
