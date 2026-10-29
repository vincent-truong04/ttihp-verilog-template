"""Per-instruction and execution-model tests for model/sim.py.

With clkdiv = 1 the SM ticks on every step(), so step k executes the k-th instruction tick.
"""

import pytest

from model.asm import assemble
from model.sim import FIFO_DEPTH, Emulator, ReservedEncoding, SMConfig, crc_step


def ticks_until(emu, cond, limit=1000):
    for n in range(1, limit + 1):
        emu.step()
        if cond():
            return n
    raise AssertionError("condition never met")


# ---- JMP -----------------------------------------------------------------------------------------

def test_jmp_always_skips(make):
    emu, sm = make("jmp end\nset x, 1\nend: set y, 2")
    emu.step(2)
    assert (sm.x, sm.y) == (0, 2)


@pytest.mark.parametrize("cond,x,y,taken", [
    ("!x", 0, 0, True), ("!x", 1, 0, False),
    ("!y", 0, 0, True), ("!y", 0, 5, False),
    ("x!=y", 1, 2, True), ("x!=y", 3, 3, False),
    ("x==y", 3, 3, True), ("x==y", 1, 2, False),
])
def test_jmp_register_conditions(make, cond, x, y, taken):
    emu, sm = make(f"jmp {cond}, hit\nset pins, 0\nhit: set pins, 1")
    sm.x, sm.y = x, y
    emu.step(1)
    assert (sm.pc == 2) == taken


def test_jmp_x_decrement_loop_runs_n_plus_1_times_and_wraps_x(make):
    emu, sm = make("set x, 3\nloop: jmp x--, loop\nset y, 1")
    # set (1) + jmp taken for x=3,2,1 and not taken at x=0 (4) = 5 ticks, then set y on tick 6
    assert ticks_until(emu, lambda: sm.y == 1) == 6
    assert sm.x == 0xFFFF  # spec section 5 JMP (PIO 3.4.2): decrements even when not taken


def test_jmp_y_decrement(make):
    emu, sm = make("set y, 2\nloop: jmp y--, loop\nset x, 1")
    assert ticks_until(emu, lambda: sm.x == 1) == 5
    assert sm.y == 0xFFFF


@pytest.mark.parametrize("level,cond,taken", [(1, "pin", True), (0, "pin", False), (0, "!pin", True), (1, "!pin", False)])
def test_jmp_pin(make, level, cond, taken):
    emu, sm = make(f"jmp {cond}, hit\nnop\nhit: nop", enable=False, jmp_pin=17)
    emu.drive(17, level)
    emu.step(2)  # let the synchronizer settle before the SM starts
    emu.enable(0)
    emu.step()
    assert (sm.pc == 2) == taken


def test_jmp_osr_not_empty(make):
    emu, sm = make("pull\nloop: out null, 4\njmp !osre, loop\nset x, 1", pull_thresh=16)
    emu.push_tx(0, 0xFFFF)
    # pull (1) + 4 x (out + jmp) = 9 ticks; set x on tick 10
    assert ticks_until(emu, lambda: sm.x == 1) == 10


def test_jmp_rx_full(make):
    emu, sm = make("loop: push noblock\njmp rxfull, done\njmp loop\ndone: set x, 1")
    ticks_until(emu, lambda: sm.x == 1)
    assert len(sm.rx_fifo) == FIFO_DEPTH


def test_jmp_crcz(make):
    emu, sm = make("jmp crcz, hit\nnop\nhit: nop", crc_seed=0)
    emu.step()
    assert sm.pc == 2
    emu, sm = make("jmp crcz, hit\nnop\nhit: nop", crc_seed=1)
    emu.step()
    assert sm.pc == 1


# ---- WAIT ----------------------------------------------------------------------------------------

def test_wait_level_has_two_clock_synchronizer_latency(make):
    emu, sm = make("wait 1 pin 2\nset x, 1", in_base=15)
    emu.step(5)
    assert sm.pc == 0
    emu.drive(17, 1)
    # Edge 1 captures the line, edge 2 moves it to the SMs' view, edge 3 the WAIT completes.
    emu.step(2)
    assert sm.pc == 0
    emu.step()
    assert sm.pc == 1
    emu.step()
    assert sm.x == 1


def test_wait_level_low(make):
    emu, sm = make("wait 0 pin 0\nset x, 1", in_base=15)
    emu.drive(15, 1)
    emu.step(10)
    assert sm.pc == 0
    emu.drive(15, 0)
    emu.step(4)
    assert sm.x == 1


@pytest.mark.parametrize("pol,start,end,fires", [(1, 0, 1, True), (1, 1, 0, False), (0, 1, 0, True), (0, 0, 1, False)])
def test_wait_edge(make, pol, start, end, fires):
    emu, sm = make(f"wait {pol} edge 0\nset x, 1", enable=False, in_base=15)
    emu.drive(15, start)
    emu.step(3)
    emu.enable(0)
    emu.step(5)
    assert sm.pc == 0, "a steady level is not an edge"
    emu.drive(15, end)
    emu.step(5)
    assert (sm.x == 1) == fires


@pytest.mark.parametrize("qual,b_level,fires", [("if_high", 1, True), ("if_high", 0, False), ("if_low", 0, True), ("if_low", 1, False)])
def test_wait_qualified_edge(make, qual, b_level, fires):
    # I2C START: SDA (pin A) falls while SCL (pin B) is high
    emu, sm = make(f"wait 0 edge 0 {qual} 1\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 1)
    emu.drive(16, b_level)
    emu.step(3)
    emu.enable(0)
    emu.step(3)
    emu.drive(15, 0)
    emu.step(5)
    assert (sm.x == 1) == fires


def test_wait_qualifier_checked_at_the_edge_not_later(make):
    emu, sm = make("wait 0 edge 0 if_high 1\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 1)
    emu.drive(16, 0)
    emu.step(3)
    emu.enable(0)
    emu.drive(15, 0)   # edge while B low: ignored
    emu.step(5)
    emu.drive(16, 1)   # B goes high afterwards, but A has no new edge
    emu.step(5)
    assert sm.x == 0


def test_wait_timeout_uses_the_dedicated_counter_and_sets_to(make):
    src = """
        set y, 9
        wait 1 pin 0 timeout
        jmp to, timed_out
        set x, 1
    timed_out:
        set x, 2
    """
    emu, sm = make(src, in_base=15, timeout=5)
    emu.step(1)
    n = ticks_until(emu, lambda: sm.pc != 1)
    assert n == 6          # spec v0.3 section 5: reload value N gives up to N + 1 ticks
    assert sm.to and sm.y == 9 and sm.tcount == 0   # Y is not touched
    emu.step(1)
    assert not sm.to       # the JMP that tested TO cleared it
    emu.step(1)
    assert sm.x == 2


def test_wait_timeout_zero_ends_on_first_tick(make):
    emu, sm = make("wait 1 pin 0 timeout\nnop", in_base=15, timeout=0)
    emu.step()
    assert sm.pc == 1 and sm.to


def test_to_is_sticky_across_later_successful_waits(make):
    src = "wait 1 pin 0 timeout\nwait 1 pin 1 timeout\nwait 1 pin 1 timeout\njmp !to, ok\nset x, 1\nok: set x, 2"
    emu, sm = make(src, in_base=15, timeout=3)
    emu.drive(16, 1)
    ticks_until(emu, lambda: sm.pc == 3)
    assert sm.to                  # set by the first WAIT; the two that succeeded did not clear it
    emu.step(2)
    assert sm.x == 1 and not sm.to


def test_timeout_reloads_for_each_wait(make):
    emu, sm = make("wait 1 pin 0 timeout\nwait 1 pin 0 timeout\nnop", in_base=15, timeout=4)
    assert ticks_until(emu, lambda: sm.pc == 2) == 10     # 5 + 5 ticks: each WAIT gets the full budget


def test_wait_without_timeout_leaves_y_alone(make):
    emu, sm = make("set y, 9\nwait 1 pin 0", in_base=15)
    emu.step(50)
    assert sm.y == 9 and sm.pc == 1


# ---- IN ------------------------------------------------------------------------------------------

def test_in_pins(make):
    emu, sm = make("in pins, 5", enable=False, in_base=15, in_shift_right=False)
    for i, v in enumerate([1, 0, 1, 1, 0]):
        emu.drive(15 + i, v)
    emu.step(3)
    emu.enable(0)
    emu.step()
    assert sm.isr == 0b01101 and sm.isr_count == 5


@pytest.mark.parametrize("src,setup,expect", [
    ("x", {"x": 0xABCD}, 0xCD), ("y", {"y": 0x1234}, 0x34), ("null", {}, 0),
    ("osr", {"osr": 0x00A5}, 0xA5), ("isr", {"isr": 0x0003}, 0x0303),
])
def test_in_register_sources_shift_left(make, src, setup, expect):
    emu, sm = make(f"in {src}, 8", in_shift_right=False)
    for k, v in setup.items():
        setattr(sm, k, v)
    emu.step()
    assert sm.isr == expect


def test_in_crc_source(make):
    emu, sm = make("in crc, 16", crc_seed=0xBEEF, in_shift_right=False)
    emu.step()
    assert sm.isr == 0xBEEF


def test_in_shift_right_puts_new_bits_at_top(make):
    emu, sm = make("in x, 4\nin y, 4")
    sm.x, sm.y = 0xA, 0x5
    emu.step(2)
    assert sm.isr == 0x5A00 and sm.isr_count == 8


# ---- OUT -----------------------------------------------------------------------------------------

def test_out_pins_shift_right_lsb_first(make):
    emu, sm = make("pull\nout pins, 3\nout pins, 3", out_base=8, out_count=3)
    emu.push_tx(0, 0b110_101)
    emu.step(2)               # first OUT on edge 2; the pad follows on edge 3 (output register)
    assert [emu.line(p) for p in (8, 9, 10)] == [0, 0, 0]
    emu.step()
    assert [emu.line(p) for p in (8, 9, 10)] == [1, 0, 1]
    emu.step()
    assert [emu.line(p) for p in (8, 9, 10)] == [0, 1, 1]


def test_out_shift_left_msb_first(make):
    emu, sm = make("pull\nout x, 4", out_shift_right=False)
    emu.push_tx(0, 0xC000)
    emu.step(2)
    assert sm.x == 0xC


@pytest.mark.parametrize("dst,attr", [("x", "x"), ("y", "y")])
def test_out_registers(make, dst, attr):
    emu, sm = make(f"pull\nout {dst}, 16")
    emu.push_tx(0, 0x8001)
    emu.step(2)
    assert getattr(sm, attr) == 0x8001 and sm.osr_count == 16


def test_out_null_discards(make):
    emu, sm = make("pull\nout null, 4\nout x, 4")
    emu.push_tx(0, 0x21)
    emu.step(3)
    assert sm.x == 2


def test_out_pindirs(make):
    emu, sm = make("pull\nout pindirs, 4", out_base=0)
    emu.push_tx(0, 0b1010)
    emu.step(2)
    assert emu.pindir[:4] == [0, 1, 0, 1]


def test_out_pc_jumps(make):
    emu, sm = make("pull\nout pc, 6\nset x, 1\nset x, 2\nset x, 3")
    emu.push_tx(0, 4)
    emu.step(3)
    assert sm.x == 3


def test_out_isr_sets_count(make):
    emu, sm = make("pull\nout isr, 5")
    emu.push_tx(0, 0x1F)
    emu.step(2)
    assert (sm.isr, sm.isr_count) == (0x1F, 5)


# ---- PUSH / PULL ---------------------------------------------------------------------------------

def test_pull_blocks_until_host_writes(make):
    emu, sm = make("pull\nmov x, osr")
    emu.step(10)
    assert sm.pc == 0
    emu.push_tx(0, 0x1234)
    emu.step(2)
    assert sm.x == 0x1234


def test_pull_noblock_empty_copies_x(make):
    emu, sm = make("set x, 99\npull noblock\nmov y, osr")
    emu.step(3)
    assert sm.y == 99


def test_pull_ifempty_is_noop_until_threshold(make):
    emu, sm = make("pull\nout null, 4\npull ifempty\nout null, 4\npull ifempty", pull_thresh=8)
    emu.push_tx(0, 0x1111)
    emu.push_tx(0, 0x2222)
    emu.step(3)
    assert sm.osr_count == 4 and len(sm.tx_fifo) == 1   # 4 < 8: no pull
    emu.step(2)
    assert sm.osr == 0x2222 and len(sm.tx_fifo) == 0    # 8 shifted: pulled


def test_push_blocks_when_full(make):
    emu, sm = make("loop: push\njmp loop")
    emu.step(20)
    assert len(sm.rx_fifo) == FIFO_DEPTH and sm.pc == 0
    emu.pop_rx(0)
    emu.step()
    assert len(sm.rx_fifo) == FIFO_DEPTH and sm.pc == 1


def test_push_noblock_full_drops_and_clears_isr(make):
    emu, sm = make("loop: in x, 16\npush noblock\njmp loop")
    sm.x = 7
    emu.step(3 * 5)
    assert len(sm.rx_fifo) == FIFO_DEPTH and sm.isr == 0


def test_push_iffull(make):
    emu, sm = make("in x, 4\npush iffull\nin x, 4\npush iffull", push_thresh=8)
    sm.x = 0xF
    emu.step(2)
    assert not sm.rx_fifo and sm.isr_count == 4
    emu.step(2)
    assert list(sm.rx_fifo) == [0xFF00]


def test_push_moves_isr_and_clears(make):
    emu, sm = make("set x, 5\nin x, 16\npush\nset x, 0")
    emu.step(4)
    assert emu.pop_rx(0) == 5 and sm.isr == 0 and sm.isr_count == 0


# ---- MOV -----------------------------------------------------------------------------------------

def test_mov_invert_and_reverse(make):
    emu, sm = make("set x, 1\nmov y, ~x\nmov isr, ::x\nmov osr, !y")
    emu.step(4)
    assert sm.y == 0xFFFE and sm.isr == 0x8000 and sm.osr == 1


def test_mov_resets_shift_counts(make):
    emu, sm = make("in null, 7\nout null, 3\nmov isr, x\nmov osr, x")
    emu.step(4)
    assert sm.isr_count == 0 and sm.osr_count == 0


def test_mov_pins_both_ways(make):
    # written on edge 2, on the pad after edge 3, captured on edge 4, in the SMs' view after edge 5,
    # read on edge 6: 4 clocks of self-observation (spec section 6)
    emu, sm = make("set x, 5\nmov pins, x [3]\nmov y, pins", out_base=8, out_count=3, in_base=8)
    emu.step(3)
    assert [emu.line(p) for p in (8, 9, 10)] == [1, 0, 1]
    emu.step(3)
    assert sm.y & 0x7 == 5


def test_mov_crc_both_ways(make):
    emu, sm = make("mov crc, x\nmov y, crc\nmov osr, crc\nmov isr, ::crc", crc_width=8)
    sm.x = 0x1FF
    emu.step(4)
    assert sm.crc == 0xFF and sm.y == 0xFF and sm.osr == 0xFF and sm.osr_count == 0
    assert sm.isr == 0xFF00 and sm.isr_count == 0


@pytest.mark.parametrize("status_rx,n,tx_words,rx_words,expect", [
    (False, 2, 1, 0, 0xFFFF), (False, 2, 2, 0, 0), (True, 1, 3, 0, 0xFFFF), (False, 0, 0, 0, 0),
])
def test_mov_status_compares_fifo_level(make, status_rx, n, tx_words, rx_words, expect):
    # spec section 5 MOV, PIO 3.4.8: all ones if the selected FIFO's level is below N
    emu, sm = make("mov x, status", status_rx=status_rx, status_n=n)
    for i in range(tx_words):
        emu.push_tx(0, i)
    emu.step()
    assert sm.x == expect


# ---- SET -----------------------------------------------------------------------------------------

def test_set_pins_and_registers(make):
    emu, sm = make("set pins, 0b101\nset x, 127\nset y, 64", set_base=12, set_count=3)
    emu.step(3)
    assert [emu.line(p) for p in (12, 13, 14)] == [1, 0, 1]
    assert (sm.x, sm.y) == (127, 64)


def test_set_pindirs(make):
    emu, sm = make("set pindirs, 0b11", set_base=6, set_count=2)
    emu.step()
    assert emu.pindir[6:8] == [1, 1]


def test_writes_to_input_only_pins_are_ignored(make):
    emu, sm = make("set pins, 1", set_base=15, set_count=1)
    emu.step()
    assert emu.out_val[15] == 0 and emu.line(15) == 0


# ---- IRQ -----------------------------------------------------------------------------------------

def test_irq_between_state_machines():
    emu = Emulator()
    waiter = assemble("irq wait_high 2\nset x, 1\nirq clear 2")
    setter = assemble("delay 5\nirq set 2\nirq wait_low 2\nset x, 1")
    emu.load(waiter.words, 0)
    emu.load(setter.words, 10)
    emu.configure(0, SMConfig(wrap_end=2))
    emu.configure(1, SMConfig(start=10, wrap_end=13))
    emu.enable(0)
    emu.enable(1)
    sm0, sm1 = emu.sms
    emu.step(6)
    assert emu.irq[2] == 0 and sm0.pc == 0
    emu.step()            # SM1 sets the flag on tick 7
    assert emu.irq[2] == 1 and sm0.pc == 0
    emu.step()            # SM0 sees it on the next edge
    assert sm0.pc == 1
    emu.step(2)           # set x; irq clear
    assert emu.irq[2] == 0 and sm0.x == 1
    emu.step(2)
    assert sm1.x == 1


def test_irq_same_edge_conflict_sm0_wins():
    emu = Emulator()
    emu.load(assemble("irq set 0").words, 0)
    emu.load(assemble("irq clear 0").words, 1)
    emu.configure(0, SMConfig(wrap_end=0))
    emu.configure(1, SMConfig(start=1, wrap_target=1, wrap_end=1))
    emu.enable(0)
    emu.enable(1)
    emu.step()
    assert emu.irq[0] == 1   # spec section 6: SM0's set beats SM1's clear


# ---- CRC -----------------------------------------------------------------------------------------

def _crc_program(bits_per_word):
    # v1 CRC taps only 1-bit shifts (spec section 6), so shift each bit out separately
    return f"""
        crc reset tap_out
    .wrap_target
        pull
        set x, {bits_per_word - 1}
    bit:
        out null, 1
        jmp x--, bit
    .wrap
    """


def _feed_bytes(emu, sm, data):
    for b in data:
        emu.run_until(lambda: len(sm.tx_fifo) < FIFO_DEPTH)
        emu.push_tx(0, b << 8)
    emu.run_until(lambda: not sm.tx_fifo and sm.pc == 1 and sm.stalled, max_cycles=200)


@pytest.mark.parametrize("poly,width,check", [(0x07, 8, 0xF4), (0x1021, 16, 0x31C3)])
def test_crc_known_vectors(make, poly, width, check):
    # CRC-8/SMBUS (I2C PEC) and CRC-16/XMODEM of "123456789", MSB first
    emu, sm = make(_crc_program(8), out_shift_right=False, crc_poly=poly, crc_width=width)
    _feed_bytes(emu, sm, b"123456789")
    assert sm.crc == check


def test_crc_parity_width_1(make):
    emu, sm = make(_crc_program(8), out_shift_right=False, crc_poly=1, crc_width=1)
    _feed_bytes(emu, sm, [0b1011_0001])
    assert sm.crc == 0   # even number of ones
    _feed_bytes(emu, sm, [0b0000_0001])
    assert sm.crc == 1


def test_crc_tap_in_mov_to_isr_and_crcz_check(make):
    src = """
        pull
        crc reset tap_in
        set y, 7
    bit:
        out x, 1               ; next bit of '1', MSB first
        in x, 1                ; tapped
        jmp y--, bit
        mov isr, crc           ; spec section 5: replaces the removed move-to-ISR bit
        push
        crc reset
        jmp crcz, zero
        set y, 1
    zero:
        set y, 2
    """
    emu, sm = make(src, out_shift_right=False, in_shift_right=False, crc_poly=0x07, crc_width=8)
    emu.push_tx(0, 0x31 << 8)
    emu.run_until(lambda: sm.rx_fifo, max_cycles=100)
    assert emu.pop_rx(0) == crc_step_bytes(b"1", 0x07, 8)
    emu.step(3)
    assert sm.y == 2


def test_crc_ignores_multibit_shifts(make):
    emu, sm = make("crc tap_out tap_in\npull\nout null, 8\nin x, 4\nout null, 1",
                   crc_poly=0x07, crc_width=8, crc_seed=0)
    emu.push_tx(0, 0xFFFF)
    emu.step(4)
    assert sm.crc == 0
    emu.step()
    assert sm.crc == 0x07   # the 1-bit OUT (a 1) updates it


def test_crc_taps_persist_until_next_crc(make):
    emu, sm = make("crc tap_out\npull\nout null, 1\nnop\nout null, 1\ncrc\nout null, 1",
                   out_shift_right=True, crc_poly=1, crc_width=1)
    emu.push_tx(0, 0b111)
    emu.step(3)
    assert sm.crc == 1
    emu.step(2)
    assert sm.crc == 0
    emu.step(2)
    assert sm.crc == 0 and not sm.tap_out   # `crc` with no flags turned tapping off


def test_crc_seed_and_tap_off_by_default(make):
    emu, sm = make("set x, 0xF\nin x, 4\nout null, 4", crc_seed=0x55, crc_width=8, crc_poly=0x07)
    emu.step(3)
    assert sm.crc == 0x55


def crc_step_bytes(data, poly, width):
    crc = 0
    for byte in data:
        for i in range(7, -1, -1):
            crc = crc_step(crc, (byte >> i) & 1, poly, width)
    return crc


# ---- DELAY, delay field, clock divider -----------------------------------------------------------

def test_delay_instruction_takes_one_plus_n_ticks(make):
    emu, sm = make("delay 10\nset x, 1")
    assert ticks_until(emu, lambda: sm.x == 1) == 12


def test_delay_instruction_adds_its_delay_field(make):
    emu, sm = make("delay 10 [3]\nset x, 1")
    assert ticks_until(emu, lambda: sm.x == 1) == 15


def test_delay_field(make):
    emu, sm = make("set x, 1 [3]\nset y, 1")
    emu.step()
    assert sm.x == 1
    assert ticks_until(emu, lambda: sm.y == 1) == 4


def test_delay_starts_after_stall_completes(make):
    emu, sm = make("pull [2]\nset x, 1")
    emu.step(10)
    emu.push_tx(0, 1)
    assert ticks_until(emu, lambda: sm.x == 1) == 4   # pull completes, 2 delay ticks, then set


def test_clock_divider(make):
    emu, sm = make("set x, 1\nset x, 2\nset x, 3", clkdiv=3)
    emu.step(2)
    assert sm.x == 0
    emu.step()
    assert sm.x == 1
    emu.step(3)
    assert sm.x == 2


def test_clock_divider_scales_delay(make):
    emu, sm = make("set x, 1 [2]\nset y, 1", clkdiv=4)
    assert ticks_until(emu, lambda: sm.y == 1) == 4 * 4


# ---- side-set ------------------------------------------------------------------------------------

def test_side_set_applies_on_first_tick_of_stalled_instruction(make):
    emu, sm = make(".side_set 1\npull side 1\nnop side 0", sideset_base=8, sideset_count=1)
    emu.step()
    assert sm.stalled and emu.out_val[8] == 1 and emu.line(8) == 0   # pad follows next edge
    emu.step()
    assert emu.line(8) == 1
    emu.step(20)
    assert emu.line(8) == 1
    emu.push_tx(0, 0)
    emu.step(3)               # pull completes, nop side 0, then the pad follows
    assert emu.line(8) == 0


def test_side_set_two_pins(make):
    emu, sm = make("nop side 2\nnop side 1", sideset_base=9, sideset_count=2)
    emu.step(2)
    assert (emu.line(9), emu.line(10)) == (0, 1)
    emu.step()
    assert (emu.line(9), emu.line(10)) == (1, 0)


def test_side_set_wins_over_out_on_same_pin(make):
    emu, sm = make("set pins, 0 side 1", set_base=8, set_count=1, sideset_base=8, sideset_count=1)
    emu.step(2)
    assert emu.line(8) == 1


def test_side_set_holds_through_delay(make):
    emu, sm = make("nop side 1 [5]\nnop side 0", sideset_base=8, sideset_count=1)
    emu.step()
    for _ in range(6):        # ticks 1-6 hold side 1; the pad shows it on edges 2-7
        emu.step()
        assert emu.line(8) == 1
    emu.step()
    assert emu.line(8) == 0


# ---- wrap ----------------------------------------------------------------------------------------

def test_wrap_loops_without_jmp_cost(make):
    src = "set x, 0\n.wrap_target\nin null, 1\nset y, 1\n.wrap\nset x, 9"
    emu, sm = make(src)
    emu.step(1 + 2 * 3)
    assert sm.x == 0 and sm.isr_count == 3 and sm.pc == 1


def test_pc_wraps_at_end_of_memory():
    emu = Emulator()
    emu.load([assemble("set x, 1").words[0]], 63)
    emu.load(assemble("set y, 1").words, 0)
    emu.configure(0, SMConfig(start=63))
    emu.enable(0)
    emu.step(2)
    assert emu.sms[0].pc == 1 and emu.sms[0].y == 1


# ---- autopull / autopush -------------------------------------------------------------------------

def test_autopull_refills_and_stalls_on_empty(make):
    emu, sm = make("out x, 8", autopull=True, pull_thresh=16)
    emu.push_tx(0, 0xBBAA)
    emu.push_tx(0, 0xDDCC)
    seen = []
    for _ in range(5):
        emu.step()
        seen.append(sm.x)
    # tick 1: OSR empty, so refill and stall (PIO 3.5.4.2: cannot refill and OUT on the same tick);
    # tick 3 empties the OSR and refills it on the same tick, so tick 4 does not stall
    assert seen == [0, 0xAA, 0xBB, 0xCC, 0xDD]
    emu.step(5)
    assert sm.stalled and sm.x == 0xDD
    emu.push_tx(0, 0x00EE)
    emu.step()
    assert sm.x == 0xDD and sm.osr == 0x00EE   # refill tick, still stalled
    emu.step()
    assert sm.x == 0xEE


def test_autopull_threshold(make):
    emu, sm = make("out x, 4", autopull=True, pull_thresh=8)
    emu.push_tx(0, 0xFF21)
    emu.push_tx(0, 0x0043)
    seen = []
    for _ in range(5):
        emu.step()
        seen.append(sm.x)
    assert seen == [0, 1, 2, 3, 4]   # upper byte of the first word never shifts out


def test_autopull_out_crossing_threshold_shifts_all_bits(make):
    # spec section 6: count 6 + 5 = 11 >= 8, so all 5 bits shift, then the OSR refills on the same tick
    emu, sm = make("out x, 3\nout x, 3\nout x, 5\nout y, 3", autopull=True, pull_thresh=8)
    emu.push_tx(0, 0x00FF)
    emu.push_tx(0, 0x1234)
    emu.step(4)
    assert sm.x == 0b00011 and sm.osr == 0x1234 and sm.osr_count == 0
    emu.step()
    assert sm.y == 4


def test_autopull_background_refill_on_non_out_ticks(make):
    emu, sm = make("nop [3]\nnop", autopull=True)
    emu.step(2)
    emu.push_tx(0, 0xAB)
    emu.step()               # a delay tick
    assert sm.osr == 0xAB and sm.osr_count == 0 and not sm.tx_fifo


def test_pull_is_noop_when_autopull_and_osr_full(make):
    emu, sm = make("nop\npull\nmov x, osr", autopull=True)
    emu.push_tx(0, 1)
    emu.push_tx(0, 2)
    emu.step(3)
    assert sm.x == 1 and list(sm.tx_fifo) == [2]


def test_autopush(make):
    emu, sm = make("in x, 4", autopush=True, push_thresh=8, in_shift_right=False)
    sm.x = 0x3
    emu.step(4)
    assert list(sm.rx_fifo) == [0x33, 0x33] and sm.isr_count == 0


def test_autopush_stalls_when_rx_full(make):
    emu, sm = make("in x, 16", autopush=True, push_thresh=16)
    sm.x = 1
    emu.step(FIFO_DEPTH + 3)
    assert len(sm.rx_fifo) == FIFO_DEPTH and sm.stalled and sm.isr_count == 0
    emu.pop_rx(0)
    emu.step()
    assert len(sm.rx_fifo) == FIFO_DEPTH and not sm.stalled


# ---- pins: open-drain, synchronizer, SM conflicts ------------------------------------------------

def test_open_drain_releases_and_reads_real_level(make):
    # I2C clock stretching: we release SCL, a device holds it low, the program sees it low
    src = "set pins, 1\nwait 1 pin 0\nset x, 1"
    emu, sm = make(src, enable=False, set_base=3, set_count=1, in_base=3)
    emu.open_drain[3] = 1
    emu.drive(3, 0)                  # device stretches the clock
    emu.enable(0)
    emu.step(2)
    assert emu.chip_drive(3) is None # released, not driven high
    assert emu.line(3) == 0
    emu.step(20)
    assert sm.pc == 1
    emu.drive(3, None)               # device lets go; pull-up takes it high
    assert emu.line(3) == 1
    emu.step(4)
    assert sm.x == 1


def test_open_drain_drives_low_regardless_of_pindir(make):
    emu, sm = make("set pins, 0", set_base=2, set_count=1)
    emu.open_drain[2] = 1
    emu.out_val[2] = 1
    emu.step(2)
    assert emu.chip_drive(2) == 0 and emu.pindir[2] == 0 and emu.line(2) == 0


def test_push_pull_uio_needs_pindir(make):
    emu, sm = make("set pins, 0\nset pindirs, 1", set_base=1, set_count=1)
    emu.step(2)
    assert emu.chip_drive(1) is None and emu.line(1) == 1   # input: reads the pull-up
    emu.step()
    assert emu.line(1) == 0


def test_sm0_wins_pin_conflict():
    emu = Emulator()
    emu.load(assemble("set pins, 1").words, 0)
    emu.load(assemble("set pins, 0").words, 1)
    emu.configure(0, SMConfig(wrap_end=0, set_base=8, set_count=1))
    emu.configure(1, SMConfig(start=1, wrap_target=1, wrap_end=1, set_base=8, set_count=1))
    emu.enable(0)
    emu.enable(1)
    emu.step(2)
    assert emu.line(8) == 1   # spec section 6: the lower-numbered SM wins (opposite of PIO)


def test_pin_conflict_levels_and_directions_are_separate():
    emu = Emulator()
    emu.load(assemble("set pins, 0").words, 0)
    emu.load(assemble("set pindirs, 1").words, 1)
    emu.configure(0, SMConfig(wrap_end=0, set_base=2, set_count=1))
    emu.configure(1, SMConfig(start=1, wrap_target=1, wrap_end=1, set_base=2, set_count=1))
    emu.enable(0)
    emu.enable(1)
    emu.step(2)
    assert emu.out_val[2] == 0 and emu.pindir[2] == 1 and emu.line(2) == 0


def test_reset_state():
    emu = Emulator()
    assert [emu.line(p) for p in range(8, 15)] == [0] * 7          # uo_out drives 0
    assert all(emu.chip_drive(p) is None for p in range(8))        # uio released
    emu.open_drain = [1] * 8                                       # still released (output values are 1)
    emu.step()
    assert all(emu.chip_drive(p) is None for p in range(8))


def test_pin_init_applied_on_enable_to_own_groups(make):
    emu, sm = make("nop", enable=False, out_base=8, out_count=1, sideset_base=9, sideset_count=1,
                   set_base=4, set_count=1, pin_init=(1 << 8) | (1 << 9) | (1 << 12))
    emu.out_val[4] = 0
    emu.enable(0)
    assert emu.out_val[8] == 1 and emu.line(8) == 0   # the pad follows on the next edge
    emu.step()
    assert emu.line(8) == 1 and emu.line(9) == 1
    assert emu.out_val[4] == 0      # in the SET group, init bit 0
    assert emu.line(12) == 0        # not in any of this SM's groups


# ---- host interface helpers ----------------------------------------------------------------------

def test_tx_fifo_full_rejects():
    emu = Emulator()
    assert all(emu.push_tx(0, i) for i in range(FIFO_DEPTH))
    assert not emu.push_tx(0, 99)
    assert emu.pop_rx(0) is None


def test_reserved_opcode_raises():
    emu = Emulator()
    emu.load([0xA << 16])
    emu.enable(0)
    with pytest.raises(RuntimeError, match="reserved opcode"):
        emu.step()


@pytest.mark.parametrize("field,value", [
    ("clkdiv", 65536), ("sideset_count", 3), ("crc_width", 17), ("in_base", 32), ("pull_thresh", 0),
    ("pin_init", 1 << 20), ("status_n", 16),
])
def test_config_validation(field, value):
    with pytest.raises(ValueError):
        Emulator().configure(0, SMConfig(**{field: value}))


# ---- v0.2 rules ----------------------------------------------------------------------------------

def _edge_wait(make, clkdiv=1, pol=1, nop_delay=0):
    emu, sm = make(f"nop [{nop_delay}]\nwait {pol} edge 0\nset x, 1", enable=False, in_base=15, clkdiv=clkdiv)
    emu.drive(15, 1 - pol)
    emu.step(3)
    return emu, sm


# Timing reminder: a pad change made before step k reaches the SMs' view at step k + 2.

def test_wait_edge_before_the_previous_instruction_completes_does_not_count(make):
    emu, sm = make("nop [3]\nnop\nwait 1 edge 0\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 0)
    emu.step(3)
    emu.enable(0)
    emu.drive(15, 1)          # SMs see the rise at step 3, during the first nop's delay ticks
    emu.step(30)              # the detector arms when the second nop completes (step 5): too late
    assert sm.pc == 2 and sm.x == 0


def test_wait_edge_during_the_previous_instructions_delay_counts(make):
    emu, sm = make("nop [3]\nwait 1 edge 0\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 0)
    emu.step(3)
    emu.enable(0)
    emu.drive(15, 1)          # seen at step 3, after the nop completed at step 1
    emu.step(8)
    assert sm.x == 1


def test_wait_edge_on_the_arming_clock_does_not_count(make):
    emu, sm = make("nop\nwait 1 edge 0\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 0)
    emu.step(1)
    emu.drive(15, 1)
    emu.step(2)               # driven before overall step 2, seen at step 4 = enabled step 1: the nop's
                              # own (arming) clock
    emu.enable(0)
    emu.step(30)
    assert sm.pc == 1 and sm.x == 0


def test_wait_edge_as_the_first_instruction_arms_on_its_first_tick(make):
    emu, sm = make("wait 1 edge 0\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 0)
    emu.step(3)
    emu.enable(0)
    emu.step(2)
    emu.drive(15, 1)
    emu.step(6)
    assert sm.x == 1


@pytest.mark.parametrize("clkdiv", [1, 3, 4, 9])
def test_wait_catches_an_edge_its_own_previous_instruction_caused(make, clkdiv):
    # spec v0.3 section 5: the 3-clock pin round trip lands on or before the WAIT's first tick when
    # clkdiv >= 3; arming when the SET completes still catches it
    src = "set pindirs, 1\nset pins, 0 [7]\nset pins, 1\nwait 1 edge 0 timeout\njmp to, missed\nset x, 1\nmissed: nop"
    emu, sm = make(src, set_base=2, set_count=1, in_base=2, clkdiv=clkdiv, timeout=50)
    emu.run_until(lambda: sm.pc == 6, max_cycles=200 * clkdiv)
    assert sm.x == 1


def test_wait_edge_one_clock_after_first_tick_counts(make):
    emu, sm = make("nop\nwait 1 edge 0\nset x, 1", enable=False, in_base=15)
    emu.drive(15, 0)
    emu.step(3)
    emu.enable(0)
    emu.drive(15, 1)          # SMs see it at step 3, the WAIT's second tick
    emu.step(2)
    assert sm.pc == 1
    emu.step(1)
    assert sm.pc == 2


def test_wait_edge_runs_every_system_clock_with_clkdiv(make):
    # a 2-clock pulse between two ticks of a clkdiv=8 SM is still caught (spec section 5)
    emu, sm = _edge_wait(make, clkdiv=8)
    emu.enable(0)
    emu.step(16)              # nop ticks at clock 8, the WAIT's first tick at clock 16
    assert sm.pc == 1
    emu.step(2)               # clocks 17-18
    emu.drive(15, 1)          # in the SMs' view at clocks 21-22
    emu.step(2)
    emu.drive(15, 0)
    emu.step(3)               # clocks 21-23: pulse comes and goes between ticks
    assert sm.pc == 1 and emu.sync_in[15] == 0
    emu.step(1)               # clock 24: next tick sees the latched edge
    assert sm.pc == 2


def test_wait_qualifier_sampled_on_the_edge_clock(make):
    emu, sm = make("wait 0 edge 0 if_high 1\nset x, 1", enable=False, in_base=15, clkdiv=10)
    emu.drive(15, 1)
    emu.drive(16, 1)
    emu.step(3)
    emu.enable(0)
    emu.step(10)              # WAIT's first tick
    emu.drive(15, 0)          # edge while B high...
    emu.step(1)
    emu.drive(16, 0)          # ...B drops before the next tick, but the edge was already qualified
    emu.step(30)
    assert sm.x == 1


def test_to_is_not_cleared_when_a_timeout_wait_starts(make):
    src = "wait 1 pin 0 timeout\nwait 1 pin 0 timeout\nnop"
    emu, sm = make(src, in_base=15, timeout=0)
    emu.step()
    assert sm.to
    emu.drive(15, 1)
    emu.step(10)
    assert sm.to                              # v0.3: only a JMP clears it


def test_wait_without_timeout_leaves_to(make):
    emu, sm = make("wait 1 pin 0 timeout\nwait 1 pin 1\nnop", in_base=15)
    emu.drive(16, 1)
    emu.step(1)
    assert sm.to and sm.pc == 1
    emu.run_until(lambda: sm.pc == 2, max_cycles=10)
    assert sm.to


def test_irq_wait_high_clears_flag():
    emu = Emulator()
    emu.load(assemble("irq wait_high 1\nnop").words)
    emu.configure(0, SMConfig(wrap_end=1))
    emu.enable(0)
    emu.write_irq(force=0b0010)
    emu.step()
    assert emu.sms[0].pc == 1 and emu.irq[1] == 0


def test_host_irq_write():
    emu = Emulator()
    emu.write_irq(force=0b1010)
    assert emu.irq == [0, 1, 0, 1]
    emu.write_irq(clear=0b0010)
    assert emu.irq == [0, 0, 0, 1]


def test_clkdiv_zero_means_65536(make):
    emu, sm = make("set x, 1", clkdiv=0)
    emu.step(65535)
    assert sm.x == 0
    emu.step()
    assert sm.x == 1


@pytest.mark.parametrize("word,msg", [
    ((2 << 16) | (1 << 7) | (1 << 5) | 4, "IN: unused operand bits"),
    ((3 << 16) | (7 << 7) | 1, "reserved OUT destination 7"),
    ((4 << 16) | (1 << 8) | 1, "PUSH/PULL: unused"),
    ((5 << 16) | (1 << 7) | (1 << 3) | 2, "MOV: unused"),
    ((5 << 16) | (5 << 7) | 1, "reserved MOV destination 5"),
    ((7 << 16) | (1 << 4), "IRQ: unused"),
    ((8 << 16) | (1 << 6), "CRC: unused"),
    ((1 << 16) | (1 << 8) | (3 << 1), "WAIT pin B"),
])
def test_reserved_encodings_raise(word, msg):
    emu = Emulator()
    emu.load([word])
    emu.enable(0)
    with pytest.raises(ReservedEncoding, match=msg):
        emu.step()


def test_missing_pins_read_zero_and_ignore_writes(make):
    # in_base 18: pins 18, 19, then P20-P31 read 0, then P0.. wrap at 32
    emu, sm = make("mov x, pins\nset pins, 0b11", enable=False, in_base=18, set_base=19, set_count=2)
    for p in (18, 19):
        emu.drive(p, 1)
    emu.step(3)
    emu.enable(0)
    emu.step(2)
    assert sm.x == 0b11 | (1 << 14) | (1 << 15)   # P18, P19, then P0, P1 (uio, pulled up) at bits 14-15
    assert emu.out_val[19] == 0                    # P19 is input-only; P20 does not exist


def test_restart_keeps_x_y_osr_and_clears_the_rest(make):
    emu, sm = make("set x, 5\nset y, 6\npull\nin null, 3\nnop [7]", crc_seed=0x12, crc_width=8)
    emu.push_tx(0, 0xBEEF)
    emu.step(5)
    sm.crc, sm.to, sm.tap_out = 0x99, True, True
    emu.restart(0)
    assert (sm.x, sm.y, sm.osr) == (5, 6, 0xBEEF)
    assert (sm.isr, sm.isr_count, sm.osr_count, sm.delay_count, sm.pc) == (0, 0, 16, 0, 0)
    assert sm.crc == 0x12 and not sm.to and not sm.tap_out   # spec section 6 restart extras


# ---- v0.2 final: MOV NULL, enable mask, output register ------------------------------------------

def test_mov_null_source(make):
    emu, sm = make("set x, 9\nmov x, null\nmov y, ~null")
    emu.step(3)
    assert sm.x == 0 and sm.y == 0xFFFF


def _lockstep_emu(clkdiv):
    emu = Emulator()
    emu.load(assemble("loop: jmp x--, loop").words, 0)
    emu.load(assemble("loop: jmp x--, loop").words, 1)
    emu.configure(0, SMConfig(clkdiv=clkdiv, wrap_end=0))
    emu.configure(1, SMConfig(clkdiv=clkdiv, start=1, wrap_target=1, wrap_end=1))
    return emu


def test_enable_mask_starts_both_sms_on_the_same_clock():
    emu = _lockstep_emu(clkdiv=3)
    emu.enable_mask(0b11)
    for _ in range(30):
        emu.step()
        assert emu.sms[0].ticks == emu.sms[1].ticks
    assert emu.sms[0].ticks == 10


def test_enable_mask_leaves_a_running_sm_alone():
    emu = _lockstep_emu(clkdiv=3)
    emu.enable(0)
    emu.step(1)
    emu.enable_mask(0b11)     # SM0 is already running: its divider keeps its phase
    emu.step(2)
    assert (emu.sms[0].ticks, emu.sms[1].ticks) == (1, 0)
    emu.enable_mask(0b10)     # disables SM0, keeps SM1
    assert not emu.sms[0].enabled and emu.sms[1].enabled


def test_enable_mask_pin_init_overlap_sm0_wins():
    emu = Emulator()
    emu.load(assemble("nop").words, 0)
    emu.configure(0, SMConfig(wrap_end=0, out_base=8, out_count=1, pin_init=1 << 8))
    emu.configure(1, SMConfig(wrap_end=0, out_base=8, out_count=1, pin_init=0))
    emu.enable_mask(0b11)
    assert emu.out_val[8] == 1


def test_output_register_pads_change_one_edge_after_the_write(make):
    emu, sm = make("set pins, 1\nset pins, 0", set_base=10, set_count=1)
    levels = []
    for _ in range(4):
        emu.step()
        levels.append(emu.line(10))
    # set on edges 1, 2, 3, 4 -> pad 1, 0, 1 after edges 2, 3, 4
    assert levels == [0, 1, 0, 1]


# ---- v0.3: OSR-bit JMP, long delay, host jump/clear/byte FIFO, synchronizer bypass ----------------

@pytest.mark.parametrize("cond,bits,taken", [("osr1", 0b1, True), ("osr1", 0b0, False), ("osr0", 0b0, True), ("osr0", 0b1, False)])
def test_jmp_on_osr_bit(make, cond, bits, taken):
    emu, sm = make(f"pull\njmp {cond}, hit\nnop\nhit: nop", out_shift_right=True)
    emu.push_tx(0, 0xFFF0 | bits)
    emu.step(2)
    assert (sm.pc == 3) == taken
    assert sm.osr_count == 1 and sm.osr == (0xFFF0 | bits) >> 1


def test_jmp_on_osr_bit_shift_left_takes_msb(make):
    emu, sm = make("pull\njmp osr1, hit\nnop\nhit: nop", out_shift_right=False)
    emu.push_tx(0, 0x8000)
    emu.step(2)
    assert sm.pc == 3


def test_jmp_on_osr_bit_uses_autopull_like_out(make):
    emu, sm = make("loop: jmp osr1, one\njmp loop\none: set x, 1", autopull=True, pull_thresh=4)
    emu.step(3)
    assert sm.stalled and sm.pc == 0          # OSR empty, FIFO empty: stalls like OUT
    emu.push_tx(0, 0b0100)
    emu.step(1)
    assert sm.stalled and sm.osr == 0b0100    # refill tick, still stalled (as OUT)
    ticks_until(emu, lambda: sm.x == 1)
    assert sm.osr_count == 3


def test_jmp_on_osr_bit_is_not_tapped_by_the_crc(make):
    emu, sm = make("crc tap_out\npull\njmp osr1, a\na: nop", crc_poly=1, crc_width=1)
    emu.push_tx(0, 1)
    emu.step(3)
    assert sm.crc == 0


def test_long_delay_without_side_set(make):
    emu, sm = make("set x, 1 [20]\nset y, 1")
    emu.step()
    assert ticks_until(emu, lambda: sm.y == 1) == 21


def test_host_jump_cancels_stall_and_delay(make):
    emu, sm = make("pull\nnop [30]\nset x, 1\nset x, 2")
    emu.step(3)
    assert sm.stalled
    emu.jump(0, 1)
    emu.step(2)
    assert sm.delay_count > 0
    emu.jump(0, 3)
    emu.step()
    assert sm.x == 2 and not sm.stalled


def test_host_jump_disarms_the_edge_detector(make):
    emu, sm = make("nop\nwait 1 edge 0\nset x, 1", in_base=15)
    emu.step(1)
    assert sm.edge_armed
    emu.jump(0, 1)
    assert not sm.edge_armed


def test_host_clear_fifos():
    emu = Emulator()
    emu.push_tx(0, 1)
    emu.sms[0].rx_fifo.append(2)
    emu.clear_fifos(0)
    assert not emu.sms[0].tx_fifo and not emu.sms[0].rx_fifo


@pytest.mark.parametrize("right,word", [(True, 0x00A5), (False, 0xA500)])
def test_host_tx_byte_is_placed_by_out_direction(make, right, word):
    emu, sm = make("nop", out_shift_right=right)
    emu.push_tx_byte(0, 0xA5)
    assert list(sm.tx_fifo) == [word]


@pytest.mark.parametrize("right,word", [(True, 0xA500), (False, 0x00A5)])
def test_host_rx_byte_is_taken_by_in_direction(make, right, word):
    emu, sm = make("nop", in_shift_right=right)
    sm.rx_fifo.append(word)
    assert emu.pop_rx_byte(0) == 0xA5 and emu.pop_rx_byte(0) is None


@pytest.mark.parametrize("bypass,latency", [(0, 2), (1, 0)])
def test_sync_bypass_removes_input_latency(make, bypass, latency):
    emu, sm = make("wait 1 pin 0\nset x, 1", in_base=15)
    emu.sync_bypass[15] = bypass
    emu.step(5)
    emu.drive(15, 1)
    n = ticks_until(emu, lambda: sm.pc == 1)
    assert n == latency + 1
