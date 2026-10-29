"""Run programs/uart_tx.pasm on the model and decode the TX pin like a UART receiver would."""

import random
from pathlib import Path

import pytest

from model.asm import assemble_file
from model.sim import FIFO_DEPTH, Emulator, SMConfig

PROGRAM = Path(__file__).resolve().parent.parent / "programs" / "uart_tx.pasm"
TX_PIN = 8           # P8 = uo_out[0]
TICKS_PER_BIT = 8


def start_uart(clkdiv):
    prog = assemble_file(PROGRAM)
    emu = Emulator()
    emu.load(prog.words)
    emu.configure(0, SMConfig(
        clkdiv=clkdiv, wrap_target=prog.wrap_target, wrap_end=prog.wrap,
        out_base=TX_PIN, out_count=1, sideset_base=TX_PIN, sideset_count=1, out_shift_right=True,
    ))
    emu.enable(0)
    return emu


def send_and_capture(emu, data, idle_after):
    """Feed bytes through the TX FIFO as space allows, recording the TX line after every clock."""
    trace, pending = [], list(data)
    sm = emu.sms[0]
    while pending or sm.tx_fifo or sm.pc != 0 or not sm.stalled:
        while pending and len(sm.tx_fifo) < FIFO_DEPTH:
            emu.push_tx(0, pending.pop(0))
        emu.step()
        trace.append(emu.line(TX_PIN))
    for _ in range(idle_after):
        emu.step()
        trace.append(emu.line(TX_PIN))
    return trace


def decode_8n1(trace, bit_clocks):
    """Find each start-bit falling edge and sample every bit at its midpoint."""
    out, i = [], 1
    while i < len(trace):
        if trace[i - 1] == 1 and trace[i] == 0:
            mid = i + bit_clocks // 2
            if mid + 9 * bit_clocks >= len(trace):
                raise AssertionError(f"frame starting at clock {i} is cut off")
            assert trace[mid] == 0, f"start bit not low at its midpoint (clock {mid})"
            byte = sum(trace[mid + (k + 1) * bit_clocks] << k for k in range(8))
            assert trace[mid + 9 * bit_clocks] == 1, f"stop bit not high (frame at clock {i})"
            out.append(byte)
            i = mid + 9 * bit_clocks
        else:
            i += 1
    return out


@pytest.mark.parametrize("clkdiv", [1, 3, 54])
def test_uart_tx_random_bytes(clkdiv):
    rng = random.Random(clkdiv)
    data = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80] + [rng.randrange(256) for _ in range(26 if clkdiv < 54 else 6)]
    emu = start_uart(clkdiv)
    bit_clocks = TICKS_PER_BIT * clkdiv
    emu.step(2 * clkdiv)  # first tick drives the line high (it resets low, see AMBIGUITY 19)
    trace = send_and_capture(emu, data, idle_after=2 * bit_clocks)
    assert decode_8n1(trace, bit_clocks) == data


def test_uart_tx_exact_bit_timing():
    """Every bit lasts exactly 8 ticks, including back-to-back frames (stop bit is 8 ticks, not more)."""
    clkdiv = 2
    emu = start_uart(clkdiv)
    emu.step(clkdiv)
    trace = send_and_capture(emu, [0x0F, 0xF0], idle_after=0)
    runs, prev, n = [], trace[0], 0
    for v in trace:
        if v == prev:
            n += 1
        else:
            runs.append((prev, n))
            prev, n = v, 1
    runs.append((prev, n))
    bit = TICKS_PER_BIT * clkdiv
    # runs[0] is the leading idle. Frame 1: start, 0x0F LSB first (1111 0000), stop.
    # Frame 2 starts straight after the 1-bit stop: start + 0000 (one run of 5 bits), then 1111.
    assert runs[1:6] == [(0, bit), (1, 4 * bit), (0, 4 * bit), (1, bit), (0, 5 * bit)], runs
    assert runs[6][0] == 1 and runs[6][1] >= 4 * bit   # last four 1s run into the stop bit / idle


def test_uart_tx_idles_high_and_busy_waits_on_empty_fifo():
    emu = start_uart(1)
    emu.step(1)
    for _ in range(500):
        emu.step()
        assert emu.line(TX_PIN) == 1
    assert emu.sms[0].pc == 0 and emu.sms[0].stalled


def test_uart_tx_baud_at_50mhz():
    # 50 MHz / (8 ticks * 54) = 115740.7 baud, within 0.5% of 115200
    baud = 50e6 / (TICKS_PER_BIT * 54)
    assert abs(baud - 115200) / 115200 < 0.005
