"""Per-instruction and execution-model tests for model/sim.py.

With clkdiv = 1 the SM ticks on every step(), so step k executes the k-th instruction tick.
"""

import pytest

from model.asm import assemble
from model.sim import FIFO_DEPTH, Emulator, SMConfig, crc_step


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
    assert sm.x == 0xFFFF  # AMBIGUITY 9: decrements even when not taken


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


def test_wait_timeout_counts_y_and_sets_to(make):
    src = """
        set y, 5
        wait 1 pin 0 timeout
        jmp to, timed_out
        set x, 1
    timed_out:
        set x, 2
    """
    emu, sm = make(src, in_base=15)
    emu.step(1)
    n = ticks_until(emu, lambda: sm.pc != 1)
    assert n == 5          # Y = 5 gives 5 ticks of waiting
    assert sm.to and sm.y == 0
    emu.step(2)
    assert sm.x == 2


def test_wait_timeout_with_y_zero_ends_on_first_tick(make):
    emu, sm = make("wait 1 pin 0 timeout\nnop", in_base=15)
    emu.step()
    assert sm.pc == 1 and sm.to


def test_wait_success_clears_to_and_keeps_remaining_y(make):
    src = "set y, 3\nwait 1 pin 0 timeout\nset y, 20\nwait 1 pin 0 timeout\njmp !to, ok\nset x, 1\nok: set x, 2"
    emu, sm = make(src, in_base=15)
    ticks_until(emu, lambda: sm.pc == 2)
    assert sm.to
    emu.drive(15, 1)
    ticks_until(emu, lambda: sm.x != 0)
    assert sm.x == 2 and not sm.to
    assert 0 < sm.y < 20


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
    emu.step(2)
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
    # written on edge 2, captured on edge 3, in the SMs' view after edge 4, read on edge 5
    emu, sm = make("set x, 5\nmov pins, x [2]\nmov y, pins", out_base=8, out_count=3, in_base=8)
    emu.step(2)
    assert [emu.line(p) for p in (8, 9, 10)] == [1, 0, 1]
    emu.step(3)
    assert sm.y & 0x7 == 5


def test_mov_crc_and_status(make):
    emu, sm = make("set x, 0\nmov crc, x\nmov y, status\nset x, 3\nmov crc, x\nmov y, status", crc_width=8)
    emu.step(3)
    assert sm.y == 0b10       # CRCZ set, TO clear
    emu.step(3)
    assert sm.crc == 3 and sm.y == 0


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


def test_irq_same_edge_conflict_sm1_wins():
    emu = Emulator()
    emu.load(assemble("irq set 0").words, 0)
    emu.load(assemble("irq clear 0").words, 1)
    emu.configure(0, SMConfig(wrap_end=0))
    emu.configure(1, SMConfig(start=1, wrap_target=1, wrap_end=1))
    emu.enable(0)
    emu.enable(1)
    emu.step()
    assert emu.irq[0] == 0


# ---- CRC -----------------------------------------------------------------------------------------

def _crc_program(bits_per_word):
    return f"""
        crc reset tap_out
    .wrap_target
        pull
        out null, {bits_per_word}
    .wrap
    """


def _feed_bytes(emu, sm, data):
    for b in data:
        emu.run_until(lambda: len(sm.tx_fifo) < FIFO_DEPTH)
        emu.push_tx(0, b << 8)
    emu.run_until(lambda: not sm.tx_fifo and sm.pc == 1 and not sm.stalled, max_cycles=100)
    emu.step(5)


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


def test_crc_reset_to_isr_and_crcz_check(make):
    src = """
        crc reset tap_in
        set x, 0x31            ; '1'
        in x, 8
        crc to_isr             ; copy, stop tapping
        push
        mov x, isr             ; ISR was cleared by push
        crc reset
        jmp crcz, zero
        set y, 1
    zero:
        set y, 2
    """
    emu, sm = make(src, in_shift_right=False, crc_poly=0x07, crc_width=8, crc_seed=0)
    emu.step(10)
    assert emu.pop_rx(0) == crc_step_bytes(b"1", 0x07, 8)
    assert sm.y == 2


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
    assert emu.line(8) == 1 and sm.stalled
    emu.step(20)
    assert emu.line(8) == 1
    emu.push_tx(0, 0)
    emu.step(2)
    assert emu.line(8) == 0


def test_side_set_two_pins(make):
    emu, sm = make("nop side 2\nnop side 1", sideset_base=9, sideset_count=2)
    emu.step()
    assert (emu.line(9), emu.line(10)) == (0, 1)
    emu.step()
    assert (emu.line(9), emu.line(10)) == (1, 0)


def test_side_set_wins_over_out_on_same_pin(make):
    emu, sm = make("set pins, 0 side 1", set_base=8, set_count=1, sideset_base=8, sideset_count=1)
    emu.step()
    assert emu.line(8) == 1


def test_side_set_holds_through_delay(make):
    emu, sm = make("nop side 1 [5]\nnop side 0", sideset_base=8, sideset_count=1)
    for _ in range(6):
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
    for _ in range(4):
        emu.step()
        seen.append(sm.x)
    assert seen == [0xAA, 0xBB, 0xCC, 0xDD]
    emu.step(5)
    assert sm.stalled and sm.x == 0xDD
    emu.push_tx(0, 0x00EE)
    emu.step()
    assert sm.x == 0xEE


def test_autopull_threshold(make):
    emu, sm = make("out x, 4", autopull=True, pull_thresh=8)
    emu.push_tx(0, 0xFF21)
    emu.push_tx(0, 0x0043)
    seen = []
    for _ in range(4):
        emu.step()
        seen.append(sm.x)
    assert seen == [1, 2, 3, 4]   # upper byte of the first word never shifts out


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
    emu.step()
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
    emu.step()
    assert emu.chip_drive(2) == 0 and emu.pindir[2] == 0 and emu.line(2) == 0


def test_push_pull_uio_needs_pindir(make):
    emu, sm = make("set pins, 0\nset pindirs, 1", set_base=1, set_count=1)
    emu.step()
    assert emu.chip_drive(1) is None and emu.line(1) == 1   # input: reads the pull-up
    emu.step()
    assert emu.line(1) == 0


def test_sm1_wins_pin_conflict():
    emu = Emulator()
    emu.load(assemble("set pins, 1").words, 0)
    emu.load(assemble("set pins, 0").words, 1)
    emu.configure(0, SMConfig(wrap_end=0, set_base=8, set_count=1))
    emu.configure(1, SMConfig(start=1, wrap_target=1, wrap_end=1, set_base=8, set_count=1))
    emu.enable(0)
    emu.enable(1)
    emu.step()
    assert emu.line(8) == 0


def test_output_pins_reset_low():
    # AMBIGUITY 19: no configured idle level, so a UART TX pin starts low
    assert Emulator().line(8) == 0


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


@pytest.mark.parametrize("field,value", [("clkdiv", 0), ("sideset_count", 3), ("crc_width", 17), ("in_base", 20), ("pull_thresh", 0)])
def test_config_validation(field, value):
    with pytest.raises(ValueError):
        Emulator().configure(0, SMConfig(**{field: value}))
