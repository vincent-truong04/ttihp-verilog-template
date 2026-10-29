from dataclasses import replace

import pytest

from model.asm import assemble
from model.sim import Emulator, SMConfig


def build(src, sm=0, enable=True, **cfg):
    """Assemble src, load it at address 0, configure `sm` (wrap from the program) and enable it."""
    prog = assemble(src)
    emu = Emulator()
    emu.load(prog.words)
    config = replace(SMConfig(), wrap_target=prog.wrap_target, wrap_end=prog.wrap, **cfg)
    emu.configure(sm, config)
    if enable:
        emu.enable(sm)
    return emu, emu.sms[sm]


@pytest.fixture
def make():
    return build
