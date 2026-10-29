"""Assembler for the protocol emulator ISA (docs/spec.md v0.2, sections 4-5).

Turns .pasm source into 20-bit machine words:

    [19:16] opcode  [15:13] delay  [12:10] side-set (enable, 2 values)  [9:0] operand

Syntax (spec section 5.1; AMBIGUITY 1, resolved there):

    ; comment            // comment also works
    .side_set <n>        number of side-set pins (0-2), checks `side` values fit
    .wrap_target         wrap target is the next instruction
    .wrap                wrap end is the previous instruction
    label:
        <instr> [side <v>] [[<delay>]]

    jmp [<cond>,] <label|addr>      cond: !x x-- !y y-- x!=y x==y pin !pin !osre rxfull to !to crcz
    wait <pol> pin <a> [timeout]                    mode 00, level
    wait <pol> edge <a> [timeout]                   mode 01, edge (pol 1 = rising)
    wait <pol> edge <a> if_high <b> [timeout]       mode 10
    wait <pol> edge <a> if_low <b> [timeout]        mode 11
    in <src>, <n>        src: pins x y null isr osr crc        n: 1-16
    out <dst>, <n>       dst: pins x y null pindirs pc isr     n: 1-16
    push [iffull] [block|noblock]
    pull [ifempty] [block|noblock]
    mov <dst>, [~|!|::]<src>         pins x y isr osr crc, plus null and status as sources
    set <dst>, <v>       dst: pins pindirs x y                 v: 0-127
    irq set|clear|wait_high|wait_low <flag>                    flag: 0-3
    crc [reset] [tap_out] [tap_in]
    delay <n>            n: 0-1023 (plus any [d] must stay <= 1023)
    nop                  alias for `mov y, y`

Numbers may be decimal, 0x hex or 0b binary. Unused operand bits are always emitted as 0 (spec section 4).

In v1 the CRC only updates on 1-bit OUT/IN shifts (spec section 6), so a program that turns tapping on gets
a warning for every multi-bit OUT (with tap_out) or IN (with tap_in). The check is static: it does not know
whether tapping is actually on when that shift runs.
"""

import re
import sys
from dataclasses import dataclass, field

OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_PUSHPULL, OP_MOV, OP_SET, OP_IRQ, OP_CRC, OP_DELAY = range(10)

JMP_CONDS = {
    "": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "x==y": 6,
    "pin": 8, "!pin": 9, "!osre": 10, "rxfull": 11, "to": 12, "!to": 13, "crcz": 14,
}
IN_SRCS = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 4, "osr": 5, "crc": 6}
OUT_DSTS = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6}
# AMBIGUITY 2 (resolved, spec section 5 SET and MOV): PIO's codes, with CRC at 4, which PIO leaves unused.
SET_DSTS = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}
MOV_SRCS = {"pins": 0, "x": 1, "y": 2, "null": 3, "crc": 4, "status": 5, "isr": 6, "osr": 7}
MOV_DSTS = {"pins": 0, "x": 1, "y": 2, "crc": 4, "isr": 6, "osr": 7}
MOV_OPS = {"": 0, "~": 1, "!": 1, "::": 2}
IRQ_OPS = {"set": 0, "clear": 1, "wait_high": 2, "wait_low": 3}
WAIT_QUALS = {"if_high": 2, "if_low": 3}
CRC_FLAGS = {"reset": 9, "tap_out": 8, "tap_in": 7}

MAX_DELAY_COUNTER = 1023  # 10-bit delay counter
IMEM_SIZE = 64


class AsmError(Exception):
    def __init__(self, msg, lineno=None, line=None):
        self.lineno = lineno
        where = f"line {lineno}: " if lineno is not None else ""
        text = f"{where}{msg}"
        if line is not None:
            text += f"\n    {line.strip()}"
        super().__init__(text)


@dataclass
class Program:
    words: list
    labels: dict
    wrap_target: int
    wrap: int
    side_set: int
    lines: list = field(default_factory=list)     # source line number for each word
    warnings: list = field(default_factory=list)  # "line N: message" strings


def encode(opcode, operand, delay=0, side=None):
    word = (opcode << 16) | (delay << 13) | operand
    if side is not None:
        word |= (1 << 12) | (side << 10)
    return word


def _num(tok, what):
    try:
        return int(tok, 0)
    except ValueError:
        raise AsmError(f"expected a number for {what}, got {tok!r}")


def _range(value, lo, hi, what):
    if not lo <= value <= hi:
        raise AsmError(f"{what} must be {lo}-{hi}, got {value}")
    return value


def _strip_comment(line):
    for marker in (";", "//"):
        i = line.find(marker)
        if i >= 0:
            line = line[:i]
    return line.strip()


_SUFFIX_RE = re.compile(r"^(?P<body>.*?)(?:\s+side\s+(?P<side>\S+))?(?:\s*\[(?P<delay>[^\]]*)\])?\s*$", re.I)


def _split_suffixes(text):
    """Split 'body side v [d]' (in that order) into (body, side, delay)."""
    m = _SUFFIX_RE.match(text)
    body, side, delay = m.group("body"), m.group("side"), m.group("delay")
    if re.search(r"\bside\b|\[", body, re.I):
        raise AsmError("`side` and `[delay]` must come after the operands, in that order")
    return body.strip(), side, delay


def _tokens(body):
    return [t for t in re.split(r"[\s,]+", body.strip().lower()) if t]


def _encode_body(mnem, args, labels, lineno):
    def need(n_min, n_max=None):
        n_max = n_min if n_max is None else n_max
        if not n_min <= len(args) <= n_max:
            raise AsmError(f"`{mnem}` takes {n_min if n_min == n_max else f'{n_min}-{n_max}'} operand(s), got {len(args)}")

    if mnem == "jmp":
        need(1, 2)
        cond = args[0] if len(args) == 2 else ""
        if cond not in JMP_CONDS:
            raise AsmError(f"unknown jmp condition {cond!r}; valid: {', '.join(c for c in JMP_CONDS if c)}")
        target = args[-1]
        if target in labels:
            addr = labels[target]
        elif re.fullmatch(r"(0x[0-9a-f]+|0b[01]+|\d+)", target):
            addr = _range(_num(target, "jmp address"), 0, IMEM_SIZE - 1, "jmp address")
        else:
            raise AsmError(f"undefined label {target!r}")
        return encode(OP_JMP, (JMP_CONDS[cond] << 6) | addr)

    if mnem == "wait":
        timeout = bool(args) and args[-1] == "timeout"
        a = args[:-1] if timeout else args
        if len(a) == 3 and a[1] == "pin":
            mode, b = 0, 0
        elif len(a) == 3 and a[1] == "edge":
            mode, b = 1, 0
        elif len(a) == 5 and a[1] == "edge" and a[3] in WAIT_QUALS:
            mode, b = WAIT_QUALS[a[3]], _range(_num(a[4], "wait pin B"), 0, 7, "wait pin B")
        else:
            raise AsmError("wait syntax: wait <pol> pin <a> | wait <pol> edge <a> [if_high|if_low <b>], then optional `timeout`")
        pol = _range(_num(a[0], "wait polarity"), 0, 1, "wait polarity")
        pin_a = _range(_num(a[2], "wait pin A"), 0, 7, "wait pin A")
        return encode(OP_WAIT, (mode << 8) | (pol << 7) | (pin_a << 4) | (b << 1) | int(timeout))

    if mnem in ("in", "out"):
        need(2)
        table = IN_SRCS if mnem == "in" else OUT_DSTS
        if args[0] not in table:
            raise AsmError(f"unknown `{mnem}` {'source' if mnem == 'in' else 'destination'} {args[0]!r}; valid: {', '.join(table)}")
        count = _range(_num(args[1], "bit count"), 1, 16, "bit count")
        # AMBIGUITY 26 (resolved, spec section 4): count [3:0], 16 encoded as 0; [6:4] must be 0
        return encode(OP_IN if mnem == "in" else OP_OUT, (table[args[0]] << 7) | (count & 0xF))

    if mnem in ("push", "pull"):
        cond_word = "iffull" if mnem == "push" else "ifempty"
        cond, block = 0, 1
        seen = set()
        for a in args:
            if a in seen:
                raise AsmError(f"repeated `{a}`")
            seen.add(a)
            if a == cond_word:
                cond = 1
            elif a in ("block", "noblock"):
                if {"block", "noblock"} <= seen:
                    raise AsmError("`block` and `noblock` both given")
                block = int(a == "block")
            else:
                raise AsmError(f"unknown `{mnem}` option {a!r}; valid: {cond_word}, block, noblock")
        return encode(OP_PUSHPULL, (int(mnem == "pull") << 9) | (block << 8) | (cond << 7))

    if mnem == "mov":
        need(2)
        dst, src = args
        m = re.fullmatch(r"(~|!|::)?(.*)", src)
        op, src = m.group(1) or "", m.group(2)
        if dst == "status":
            # AMBIGUITY 3 (resolved, spec section 5 MOV): STATUS is source-only, as in PIO
            raise AsmError("status is a mov source only")
        if dst not in MOV_DSTS:
            raise AsmError(f"unknown mov destination {dst!r}; valid: {', '.join(MOV_DSTS)}")
        if src not in MOV_SRCS:
            raise AsmError(f"unknown mov source {src!r}; valid: {', '.join(MOV_SRCS)}")
        return encode(OP_MOV, (MOV_DSTS[dst] << 7) | (MOV_OPS[op] << 5) | MOV_SRCS[src])

    if mnem == "nop":
        need(0)
        return encode(OP_MOV, (MOV_DSTS["y"] << 7) | MOV_SRCS["y"])

    if mnem == "set":
        need(2)
        if args[0] not in SET_DSTS:
            raise AsmError(f"unknown set destination {args[0]!r}; valid: {', '.join(SET_DSTS)}")
        value = _range(_num(args[1], "set value"), 0, 127, "set value")
        return encode(OP_SET, (SET_DSTS[args[0]] << 7) | value)

    if mnem == "irq":
        need(2)
        if args[0] not in IRQ_OPS:
            raise AsmError(f"unknown irq operation {args[0]!r}; valid: {', '.join(IRQ_OPS)}")
        flag = _range(_num(args[1], "irq flag"), 0, 3, "irq flag")
        return encode(OP_IRQ, (IRQ_OPS[args[0]] << 8) | flag)

    if mnem == "crc":
        operand = 0
        for a in args:
            if a not in CRC_FLAGS:
                raise AsmError(f"unknown crc option {a!r}; valid: {', '.join(CRC_FLAGS)}")
            if operand & (1 << CRC_FLAGS[a]):
                raise AsmError(f"repeated `{a}`")
            operand |= 1 << CRC_FLAGS[a]
        return encode(OP_CRC, operand)

    if mnem == "delay":
        need(1)
        return encode(OP_DELAY, _range(_num(args[0], "delay ticks"), 0, 1023, "delay ticks"))

    raise AsmError(f"unknown instruction {mnem!r}")


def assemble(source):
    """Assemble .pasm source text into a Program. Raises AsmError with the line number on failure."""
    # Pass 1: labels, directives, instruction lines
    labels, instrs = {}, []
    wrap_target, wrap, side_set = None, None, None
    for lineno, raw in enumerate(source.splitlines(), 1):
        text = _strip_comment(raw)
        try:
            while True:
                m = re.match(r"^([A-Za-z_]\w*)\s*:\s*(.*)$", text)
                if not m:
                    break
                name = m.group(1).lower()
                if name in labels:
                    raise AsmError(f"label {name!r} defined twice")
                if name in JMP_CONDS or name in ("timeout",):
                    raise AsmError(f"{name!r} is reserved and cannot be a label")
                labels[name] = len(instrs)
                text = m.group(2).strip()
            if not text:
                continue
            if text.startswith("."):
                parts = text.split()
                d = parts[0].lower()
                if d == ".wrap_target" and len(parts) == 1:
                    if wrap_target is not None:
                        raise AsmError(".wrap_target given twice")
                    wrap_target = len(instrs)
                elif d == ".wrap" and len(parts) == 1:
                    if wrap is not None:
                        raise AsmError(".wrap given twice")
                    if not instrs:
                        raise AsmError(".wrap must follow at least one instruction")
                    wrap = len(instrs) - 1
                elif d == ".side_set" and len(parts) == 2:
                    if instrs:
                        raise AsmError(".side_set must come before the first instruction")
                    side_set = _range(_num(parts[1], ".side_set count"), 0, 2, ".side_set count")
                else:
                    raise AsmError(f"unknown or malformed directive {text!r}")
                continue
            instrs.append((lineno, raw, text))
        except AsmError as e:
            raise AsmError(str(e), lineno, raw) from None

    if not instrs:
        raise AsmError("program has no instructions")
    if len(instrs) > IMEM_SIZE:
        raise AsmError(f"program has {len(instrs)} instructions; instruction memory holds {IMEM_SIZE}")

    # Pass 2: encode
    words = []
    for lineno, raw, text in instrs:
        try:
            body, side, delay = _split_suffixes(text)
            parts = body.split(None, 1)
            mnem = parts[0].lower()
            args = _tokens(parts[1]) if len(parts) > 1 else []
            word = _encode_body(mnem, args, labels, lineno)

            d = 0
            if delay is not None:
                d = _range(_num(delay.strip(), "delay"), 0, 7, "delay field")
                if (word >> 16) == OP_DELAY and (word & 0x3FF) + d > MAX_DELAY_COUNTER:
                    raise AsmError(f"`delay` ticks plus [{d}] exceed the 10-bit delay counter ({MAX_DELAY_COUNTER})")
            s = None
            if side is not None:
                s = _num(side, "side-set value")
                limit = 3 if side_set is None else (1 << side_set) - 1
                if side_set == 0:
                    raise AsmError("`side` used but .side_set is 0")
                _range(s, 0, limit, "side-set value")
            words.append(word | (d << 13) | (0 if s is None else (1 << 12) | (s << 10)))
        except AsmError as e:
            raise AsmError(str(e), lineno, raw) from None

    # CRC taps only update on 1-bit shifts in v1 (spec section 6): warn about wider shifts
    warnings = []
    ops = [(w >> 16, w & 0x3FF) for w in words]
    taps_out = any(op == OP_CRC and operand & (1 << CRC_FLAGS["tap_out"]) for op, operand in ops)
    taps_in = any(op == OP_CRC and operand & (1 << CRC_FLAGS["tap_in"]) for op, operand in ops)
    for (op, operand), lineno in zip(ops, (ln for ln, _, _ in instrs)):
        count = (operand & 0xF) or 16
        if count > 1 and ((op == OP_OUT and taps_out) or (op == OP_IN and taps_in)):
            name = "out" if op == OP_OUT else "in"
            warnings.append(f"line {lineno}: `{name}` of {count} bits does not update the CRC (v1 taps only 1-bit shifts)")

    return Program(
        warnings=warnings,
        words=words,
        labels=labels,
        wrap_target=0 if wrap_target is None else wrap_target,
        wrap=len(words) - 1 if wrap is None else wrap,
        side_set=2 if side_set is None else side_set,
        lines=[ln for ln, _, _ in instrs],
    )


def assemble_file(path):
    with open(path) as f:
        return assemble(f.read())


def main(argv):
    if len(argv) != 2:
        print("usage: python -m model.asm <file.pasm>", file=sys.stderr)
        return 2
    try:
        prog = assemble_file(argv[1])
    except AsmError as e:
        print(f"{argv[1]}: {e}", file=sys.stderr)
        return 1
    for w in prog.warnings:
        print(f"{argv[1]}: warning: {w}", file=sys.stderr)
    for addr, word in enumerate(prog.words):
        marks = ("  <- wrap_target" if addr == prog.wrap_target else "") + ("  <- wrap" if addr == prog.wrap else "")
        print(f"{addr:2d}: {word:05x}{marks}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
