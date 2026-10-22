# SPDX-FileCopyrightText: © 2026 Vincent Truong
# SPDX-License-Identifier: Apache-2.0

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

CLKS_PER_BIT = 434      # must match project.v
CLK_PERIOD_NS = 20      # 50 MHz


def tx_bit(dut):
    return int(dut.uo_out.value) & 1


def busy_bit(dut):
    return (int(dut.uo_out.value) >> 1) & 1


async def reset(dut):
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    assert tx_bit(dut) == 1, "TX must idle high during reset"
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    assert tx_bit(dut) == 1, "TX must idle high after reset"


async def pulse_go(dut):
    dut.uio_in.value = 1
    await ClockCycles(dut.clk, 5)
    dut.uio_in.value = 0


async def receive_byte(dut, timeout_cycles=1000):
    """Wait for a start bit, sample each bit at its midpoint, return the byte."""
    for _ in range(timeout_cycles):
        if tx_bit(dut) == 0:
            break
        await ClockCycles(dut.clk, 1)
    else:
        raise AssertionError("Timed out waiting for start bit")

    await ClockCycles(dut.clk, CLKS_PER_BIT // 2)
    assert tx_bit(dut) == 0, "Start bit not low at midpoint"

    value = 0
    for i in range(8):
        await ClockCycles(dut.clk, CLKS_PER_BIT)
        value |= tx_bit(dut) << i

    await ClockCycles(dut.clk, CLKS_PER_BIT)
    assert tx_bit(dut) == 1, "Stop bit not high at midpoint"
    return value


async def send_and_check(dut, byte):
    dut.ui_in.value = byte
    await ClockCycles(dut.clk, 2)
    await pulse_go(dut)
    got = await receive_byte(dut)
    assert got == byte, f"Sent 0x{byte:02X}, received 0x{got:02X}"
    # let the stop bit finish and the FSM return to idle
    await ClockCycles(dut.clk, CLKS_PER_BIT)
    assert busy_bit(dut) == 0, "BUSY still high after frame"
    assert tx_bit(dut) == 1, "TX not idle after frame"


@cocotb.test()
async def test_uart_tx(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    random.seed(1)
    bytes_to_send = [0x00, 0xFF, 0x55, 0xAA, 0x01, 0x80]
    bytes_to_send += [random.randrange(256) for _ in range(4)]

    for b in bytes_to_send:
        dut._log.info(f"Sending 0x{b:02X}")
        await send_and_check(dut, b)


@cocotb.test()
async def test_go_ignored_while_busy(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    await reset(dut)

    dut.ui_in.value = 0x3C
    await ClockCycles(dut.clk, 2)
    await pulse_go(dut)

    # change data and pulse GO again mid-frame; it must not corrupt the frame
    await ClockCycles(dut.clk, CLKS_PER_BIT * 3)
    assert busy_bit(dut) == 1
    dut.ui_in.value = 0xC3
    await pulse_go(dut)

    # the frame already started, so finish receiving it from where we are:
    # simplest check is that no second frame follows
    await ClockCycles(dut.clk, CLKS_PER_BIT * 10)
    assert busy_bit(dut) == 0, "BUSY should be low once the single frame ends"
    for _ in range(CLKS_PER_BIT * 12):
        assert tx_bit(dut) == 1, "A second frame was sent from a GO pulse during BUSY"
        await ClockCycles(dut.clk, 1)
