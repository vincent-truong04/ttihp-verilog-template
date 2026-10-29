# CLAUDE.md

## Project

Vincent Truong's solo entry for Jane Street's protocol emulator ASIC competition, built on the Tiny Tapeout
template for the IHP SG13CMOS5L process.

- Top module: `tt_um_vincent_truong04_protoemu` (`src/project.v`)
- Process: IHP `ihp-sg13cmos5l`, branch `cmos5l`
- Size: 6x4 tiles (`info.yaml`)
- Clock: 50 MHz
- Deadline: **January 18, 2027**

The current design is a hello-world 8N1 UART TX (`src/uart_tx.v`). It's a placeholder until the programmable
protocol core replaces it.

## Running tests

```sh
cd test && PATH=../.venv/bin:$PATH make
```

This runs the cocotb tests in `test/test.py` on Icarus Verilog. `make` exits 0 even when tests fail, so check
the summary table or run `python -m cocotb_tools.check_results results.xml` (CI does the same).

When adding a source file, list it in both `info.yaml` (`source_files`) and `test/Makefile` (`PROJECT_SOURCES`).

## GitHub

- `origin` is the fork `vincent-truong04/ttihp-verilog-template`. `upstream` is `TinyTapeout/ttihp-verilog-template`.
- `gh` commands must target the fork. It's set as the default with `gh repo set-default`; pass
  `-R vincent-truong04/ttihp-verilog-template` if there's any doubt. Upstream's runs and commits are not this
  project's.
- Pull template fixes with `git fetch upstream && git merge upstream/cmos5l`.
- CI runs the `test`, `docs` and `gds` workflows on every push. The `gds` workflow includes `gl_test`, `precheck` and `viewer`. Cell counts and
  utilization are in the `tt_submission` artifact under `stats/metrics.csv` and `stats/synthesis-stats.txt`.

## Working rules

- Vincent writes the core RTL. Don't modify Verilog in `src/` without asking first. If a test fails, report what
  failed and why instead of fixing the RTL.
- Show the full diff and get approval before committing or pushing.
- Keep LF line endings (`.gitattributes` enforces `eol=lf`). Files copied from Windows may have CRLF.
