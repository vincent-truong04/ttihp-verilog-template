from pathlib import Path

import pytest

from model.asm import AsmError, assemble, assemble_file, encode

PROGRAMS = Path(__file__).resolve().parent.parent / "programs"


def one(line, side_set=None):
    header = f".side_set {side_set}\n" if side_set is not None else ""
    return assemble(header + line).words[0]


def fields(word):
    return {
        "opcode": word >> 16, "delay": (word >> 13) & 7, "side_en": (word >> 12) & 1,
        "side": (word >> 10) & 3, "operand": word & 0x3FF,
    }


# ---- field layout --------------------------------------------------------------------------------

def test_field_layout_matches_spec_section_4():
    w = one("set x, 7 side 2 [5]")
    assert fields(w) == {"opcode": 0b0110, "delay": 5, "side_en": 1, "side": 2, "operand": (1 << 7) | 7}
    assert w < 1 << 20


def test_no_side_means_side_enable_clear():
    assert fields(one("set x, 7"))["side_en"] == 0
    assert fields(one("set x, 7 side 0"))["side_en"] == 1


# ---- per-instruction encodings -------------------------------------------------------------------

@pytest.mark.parametrize("cond,code", [
    ("", 0), ("!x,", 1), ("x--,", 2), ("!y,", 3), ("y--,", 4), ("x!=y,", 5), ("x==y,", 6),
    ("pin,", 8), ("!pin,", 9), ("!osre,", 10), ("rxfull,", 11), ("to,", 12), ("!to,", 13), ("crcz,", 14),
])
def test_jmp_conditions(cond, code):
    assert one(f"jmp {cond} 37") == encode(0, (code << 6) | 37)


def test_jmp_labels_forward_and_backward():
    prog = assemble("top: jmp fwd\n nop\nfwd: jmp top")
    assert prog.labels == {"top": 0, "fwd": 2}
    assert prog.words[0] & 0x3F == 2 and prog.words[2] & 0x3F == 0


@pytest.mark.parametrize("src,operand", [
    ("wait 0 pin 0", (0 << 8) | (0 << 7) | (0 << 4)),
    ("wait 1 pin 5 timeout", (0 << 8) | (1 << 7) | (5 << 4) | 1),
    ("wait 1 edge 3", (1 << 8) | (1 << 7) | (3 << 4)),
    ("wait 0 edge 1 if_high 2", (2 << 8) | (0 << 7) | (1 << 4) | (2 << 1)),
    ("wait 1 edge 7 if_low 6 timeout", (3 << 8) | (1 << 7) | (7 << 4) | (6 << 1) | 1),
])
def test_wait_modes(src, operand):
    assert one(src) == encode(1, operand)


@pytest.mark.parametrize("src,code", [("pins", 0), ("x", 1), ("y", 2), ("null", 3), ("isr", 4), ("osr", 5), ("crc", 6)])
def test_in_sources(src, code):
    assert one(f"in {src}, 16") == encode(2, (code << 7) | 16)


@pytest.mark.parametrize("dst,code", [("pins", 0), ("x", 1), ("y", 2), ("null", 3), ("pindirs", 4), ("pc", 5), ("isr", 6)])
def test_out_destinations(dst, code):
    assert one(f"out {dst}, 1") == encode(3, (code << 7) | 1)


@pytest.mark.parametrize("src,operand", [
    ("push", 0b0_1_0 << 7), ("push iffull", 0b0_1_1 << 7), ("push noblock", 0b0_0_0 << 7),
    ("pull", 0b1_1_0 << 7), ("pull ifempty noblock", 0b1_0_1 << 7), ("pull block", 0b1_1_0 << 7),
])
def test_push_pull(src, operand):
    assert one(src) == encode(4, operand)


@pytest.mark.parametrize("src,operand", [
    ("mov x, y", (1 << 7) | 2), ("mov isr, ~osr", (3 << 7) | (1 << 5) | 4),
    ("mov pins, !x", (0 << 7) | (1 << 5) | 1), ("mov crc, ::isr", (5 << 7) | (2 << 5) | 3),
    ("mov y, status", (2 << 7) | 6), ("nop", (2 << 7) | 2),
])
def test_mov(src, operand):
    assert one(src) == encode(5, operand)


@pytest.mark.parametrize("dst,code", [("pins", 0), ("x", 1), ("y", 2), ("pindirs", 4)])
def test_set(dst, code):
    assert one(f"set {dst}, 127") == encode(6, (code << 7) | 127)


@pytest.mark.parametrize("op,code", [("set", 0), ("clear", 1), ("wait_high", 2), ("wait_low", 3)])
def test_irq(op, code):
    assert one(f"irq {op} 3") == encode(7, (code << 8) | 3)


def test_crc_flags_any_order():
    assert one("crc") == encode(8, 0)
    assert one("crc to_isr reset tap_in tap_out") == encode(8, 0b1111 << 6)
    assert one("crc tap_out") == encode(8, 1 << 8)


def test_delay():
    assert one("delay 1023") == encode(9, 1023)
    assert one("delay 1016 [7]") == encode(9, 1016, delay=7)


# ---- directives ----------------------------------------------------------------------------------

def test_wrap_directives():
    prog = assemble("nop\n.wrap_target\nnop\nnop\n.wrap\nnop")
    assert (prog.wrap_target, prog.wrap) == (1, 2)


def test_wrap_defaults_to_whole_program():
    prog = assemble("nop\nnop\nnop")
    assert (prog.wrap_target, prog.wrap) == (0, 2)


def test_comments_and_blank_lines():
    prog = assemble("; header\n\n  nop // trailing\nlbl:   ; label only\n  nop\n")
    assert len(prog.words) == 2 and prog.labels == {"lbl": 1}
    assert prog.lines == [3, 5]


def test_uart_tx_program_assembles():
    prog = assemble_file(PROGRAMS / "uart_tx.pasm")
    assert len(prog.words) == 4
    assert (prog.wrap_target, prog.wrap, prog.side_set) == (0, 3, 1)


# ---- errors --------------------------------------------------------------------------------------

@pytest.mark.parametrize("src,msg", [
    ("frob x", "unknown instruction"),
    ("in pins, 0", "bit count must be 1-16"),
    ("out x, 17", "bit count must be 1-16"),
    ("in pc, 1", "unknown `in` source"),
    ("out crc, 1", "unknown `out` destination"),
    ("set x, 128", "set value must be 0-127"),
    ("set osr, 1", "unknown set destination"),
    ("nop [8]", "delay field must be 0-7"),
    (".side_set 1\nnop side 2", "side-set value must be 0-1"),
    (".side_set 0\nnop side 0", ".side_set is 0"),
    ("nop side 4", "side-set value must be 0-3"),
    ("jmp nowhere", "undefined label"),
    ("jmp 64", "jmp address must be 0-63"),
    ("jmp x++, 0", "unknown jmp condition"),
    ("mov status, x", "mov to status is not supported"),
    ("mov x, pc", "unknown mov source"),
    ("wait 2 pin 0", "wait polarity must be 0-1"),
    ("wait 1 pin 8", "wait pin A must be 0-7"),
    ("wait 1 level 0", "wait syntax"),
    ("wait 1 pin 0 if_high 1", "wait syntax"),
    ("irq toggle 0", "unknown irq operation"),
    ("irq set 4", "irq flag must be 0-3"),
    ("crc reset reset", "repeated"),
    ("crc parity", "unknown crc option"),
    ("push ifempty", "unknown `push` option"),
    ("pull block noblock", "both given"),
    ("delay 1024", "delay ticks must be 0-1023"),
    ("delay 1020 [4]", "exceed the 10-bit delay counter"),
    ("a: nop\na: nop", "defined twice"),
    ("nop [1] side 0", "must come after the operands"),
    (".wrap\nnop", ".wrap must follow"),
    (".bogus", "unknown or malformed directive"),
    ("nop\n.side_set 1", "before the first instruction"),
    ("", "no instructions"),
    ("nop\n" * 65, "instruction memory holds 64"),
])
def test_errors(src, msg):
    with pytest.raises(AsmError, match=msg.replace("[", r"\[").replace(".", r"\.")):
        assemble(src)


def test_error_reports_line_number():
    with pytest.raises(AsmError) as e:
        assemble("nop\nnop\nset x, 999\n")
    assert e.value.lineno == 3
    assert "line 3" in str(e.value)
