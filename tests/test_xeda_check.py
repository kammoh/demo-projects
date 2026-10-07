"""The oracle of `xeda_check.py`: a deliberately broken design file fails each mode.

Run with openXC7's tools first on PATH and xeda (it needs PyYAML) on it too:

    source /opt/openxc7/export.sh
    python -m pytest tests/test_xeda_check.py

Each test works in a small repository of its own under `tmp_path` (a copy of the checker, the
Makefile include, a three-file demo and workflow fixtures, committed to a git repository because
the checker exports `HEAD`), so the real design files are never touched. What needs no tool
(`--ci-list`, the units) always runs; what runs yosys is skipped when the toolchain is not on
PATH. The expensive part, a bitstream build (`--twice`, `--from-makefile-netlist`: a chip
database for each side), runs only with `XEDA_CHECK_FULL=1`; everything that can fail cheaply
fails in `--synth-only` or before any chip database exists.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True  # loading the checker must not leave a `__pycache__` in the checkout

FORK = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("xeda_check", FORK / "xeda_check.py")
assert spec and spec.loader
xeda_check = importlib.util.module_from_spec(spec)
sys.modules["xeda_check"] = xeda_check
spec.loader.exec_module(xeda_check)

FULL = pytest.mark.skipif(
    not os.environ.get("XEDA_CHECK_FULL"), reason="builds bitstreams: set XEDA_CHECK_FULL=1"
)


def toolchain_or_skip() -> None:
    try:
        xeda_check.check_toolchain("xeda")
    except xeda_check.SetupError as error:
        pytest.skip(str(error))


# ---- a small repository ------------------------------------------------------------------------

MAKEFILE = """FAMILY  = artix7
PART    = xc7a35tcsg324-1
PROJECT = mini
CHIPDB  = ${ARTIX7_CHIPDB}

# `unused.v` is read and changes nothing in the netlist: a design file that drops it builds the
# same hardware, which only the comparison of its sources can see
ADDITIONAL_SOURCES = counter.v unused.v

include ../openXC7.mk
"""
MINI_V = """`default_nettype none
module mini (input wire clk, output wire led);
    counter c (.clk(clk), .msb(led));
endmodule
"""
COUNTER_V = """`default_nettype none
module counter (input wire clk, output wire msb);
    reg [24:0] r = 0;
    always @(posedge clk) r <= r + 1;
    assign msb = r[24];
endmodule
"""
UNUSED_V = """module unused (input wire a, output wire b);
    assign b = ~a;
endmodule
"""
MINI_XDC = """set_property LOC E3 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports {clk}]

set_property LOC H5 [get_ports led]
set_property IOSTANDARD LVCMOS33 [get_ports {led}]
"""
MINI_YAML = """name: mini
rtl:
  top: mini
  sources:
    - mini.v
    - counter.v
    - unused.v
    - mini.xdc
flows:
  yosys_fpga:
    fpga:
      part: xc7a35tcsg324-1
"""
SMOKE = """name: smoke
on: [push]
jobs:
  project:
    strategy:
      matrix:
        include:
          - family: artix7
            upper: ARTIX7
            project: mini
          - family: artix7
            upper: ARTIX7
            project: boards
    steps:
      - name: Build
        env:
          BOARD: second
        run: make -C "$PROJECT"
  determinism:
    strategy:
      matrix:
        include:
          - family: artix7
            upper: ARTIX7
            project: mini
    steps:
      - run: make -C "$PROJECT"
"""
HEAVY = """name: heavy
on: [workflow_dispatch]
jobs:
  project:
    strategy:
      matrix:
        include:
          - family: artix7
            upper: ARTIX7
            project: slow
    steps:
      - run: make -C "$PROJECT"
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_fork(root: Path, *, committed: bool = True) -> Path:
    """The checker and what it reads, as a repository: `mini` (three Verilog files and their
    constraints), `boards` (whose design file is named for the workflow's BOARD) and `slow`."""
    root.mkdir(parents=True)
    for name in ("xeda_check.py", "xedaproject.yaml", "openXC7.mk"):
        shutil.copy(FORK / name, root / name)
    write(root / ".github/workflows/smoke.yml", SMOKE)
    write(root / ".github/workflows/heavy.yml", HEAVY)
    mini = root / "mini"
    write(mini / "Makefile", MAKEFILE)
    write(mini / "mini.v", MINI_V)
    write(mini / "counter.v", COUNTER_V)
    write(mini / "unused.v", UNUSED_V)
    write(mini / "mini.xdc", MINI_XDC)
    write(mini / "mini.yaml", MINI_YAML)
    write(root / "boards/boards-second.yaml", "name: boards-second\n")
    write(root / "slow/slow.yaml", "name: slow\n")
    if committed:
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "fixture"], cwd=root, check=True)
    return root


def check(root: Path, *arguments: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "xeda_check.py", *arguments],
        cwd=root, capture_output=True, text=True, timeout=timeout,
    )


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


@pytest.fixture
def fork(tmp_path: Path) -> Path:
    return make_fork(tmp_path / "fork")


# ---- --ci-list: no tool, no build -------------------------------------------------------------


def test_ci_list_is_green_when_every_project_has_a_design_file(fork: Path) -> None:
    run = check(fork, "--ci-list")
    assert run.returncode == 0, run.stdout + run.stderr
    # the project matrix, the determinism matrix and heavy.yml are all read; the picosoc-like
    # project's design file is the one named by the job's BOARD
    out = "\n".join(" ".join(line.split()) for line in run.stdout.splitlines())
    assert "mini smoke, determinism design file mini/mini.yaml" in out
    assert "boards smoke (BOARD=second) design file boards/boards-second.yaml" in out
    assert "slow heavy design file slow/slow.yaml" in out
    assert "3 projects: 3 with a design file, 0 excluded, 0 missing" in out


def test_ci_list_fails_on_a_project_without_a_design_file(fork: Path) -> None:
    (fork / "mini/mini.yaml").unlink()
    run = check(fork, "--ci-list")
    assert run.returncode == 1
    assert "mini" in run.stdout and "missing" in run.stdout
    assert "1 missing or inconsistent" in run.stdout


def test_ci_list_fails_on_a_project_upstream_added(fork: Path) -> None:
    edit(fork / ".github/workflows/heavy.yml", "project: slow", "project: slower")
    run = check(fork, "--ci-list")
    assert run.returncode == 1
    assert "slower" in run.stdout and "missing" in run.stdout


def test_an_exclusion_is_a_state_and_needs_its_reason(fork: Path) -> None:
    (fork / "mini/mini.yaml").unlink()
    write(fork / "xeda-exclusions.yaml", "excluded:\n  mini: no board to compare against\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "excluded: no board to compare against" in run.stdout
    write(
        fork / "xeda-exclusions.yaml",
        "excluded:\n  mini:\n    reason: a reason\n    evidence: its log\n",
    )
    assert "excluded: a reason (its log)" in check(fork, "--ci-list").stdout
    write(fork / "xeda-exclusions.yaml", "excluded:\n  mini: ''\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 2 and "needs a reason" in run.stderr


def test_an_exclusion_that_is_stale_or_contradicts_a_design_file_fails(fork: Path) -> None:
    write(fork / "xeda-exclusions.yaml", "excluded:\n  not-in-ci: because\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 2 and "not-in-ci" in run.stderr
    write(fork / "xeda-exclusions.yaml", "excluded:\n  mini: because\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 1 and "CONFLICT" in run.stdout


def test_a_design_file_outside_ci_can_be_excluded(fork: Path) -> None:
    """A design file of a project upstream's CI does not build may be excluded, by its path:
    `--ci-list` shows it with its reason. One that does not exist, or the design file of a CI
    project (which is excluded by its name, without a design file), is an error."""
    write(fork / "extra/extra.yaml", "name: extra\n")
    write(fork / "xeda-exclusions.yaml",
          "excluded:\n  extra/extra.yaml:\n    reason: builds on neither route\n    evidence: its log\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "excluded design file (not built by --all): extra/extra.yaml: builds on neither route (its log)" in run.stdout
    assert "design files for projects upstream's CI does not build" not in run.stdout
    write(fork / "xeda-exclusions.yaml", "excluded:\n  extra/gone.yaml: because\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 2 and "extra/gone.yaml, which is not a design file" in run.stderr
    write(fork / "xeda-exclusions.yaml", "excluded:\n  mini/mini.yaml: because\n")
    run = check(fork, "--ci-list")
    assert run.returncode == 2 and "the design file of a project upstream's CI builds" in run.stderr


def test_the_real_workflows_are_listed_with_the_regression_cases() -> None:
    """Upstream's own list (this repository's `.github/workflows`): 20 smoke projects, the heavy
    one and the regression cases `regression/run.sh` runs by default."""
    rows = {r.name: r for r in xeda_check.ci_rows()}
    assert sum("smoke" in r.where for r in rows.values()) == 20
    assert rows["litex-ddr-hpcstore-k420t"].where == ["heavy"]
    assert [n for n, r in rows.items() if "determinism" in r.where] == [
        "blinky-digilent-arty", "blinky-qmtech", "blinky-digilent-zybo", "ddr3-test-arty-s7"
    ]
    assert len([n for n in rows if n.startswith("regression/")]) == 18
    assert rows["picosoc"].design == FORK / "picosoc/picosoc-kx2.yaml"


# ---- units: what --twice and --from-makefile-netlist compare -------------------------------


def test_the_bitstream_comparison_has_teeth() -> None:
    """`--twice` rests on this: a bit of configuration data differs, the date does not count."""
    original = (FORK / "blinky-digilent-arty/blinky.bit").read_bytes()
    fields, _, data = xeda_check.parse_bitstream(original)
    start = len(original) - len(data)

    def compared(data: bytes) -> bool:
        return xeda_check.compare_bitstreams_bytes(original, data)[0]

    assert compared(original)
    flipped = bytearray(original)
    flipped[start + 1000] ^= 0x01
    assert not compared(bytes(flipped))
    redated = bytearray(original)
    date = original.index(fields["c"])
    redated[date : date + len(fields["c"])] = b"x" * len(fields["c"])
    assert compared(bytes(redated))


def test_a_rebuilt_toolchain_gets_chip_databases_of_its_own(tmp_path: Path, monkeypatch) -> None:
    """The Makefile side's chip databases are kept by toolchain: a changed nextpnr, bbasm,
    generator or Project X-Ray file names another directory, so a database made by an older
    toolchain is never placed with by a newer one."""
    prefix = tmp_path / "openxc7"
    files = {
        "bin/nextpnr-himbaechel": "nextpnr 1",
        "bin/bbasm": "bbasm 1",
        "share/nextpnr/himbaechel/uarch/xilinx/gen/xilinx_gen.py": "gen 1",
        "prjxray-db/artix7/tilegrid.json": "grid 1",
        "prjxray-db/kintex7/tilegrid.json": "grid 1",
    }
    for name, text in files.items():
        write(prefix / name, text)
    env = {
        "NEXTPNR_XILINX_DIR": str(prefix),
        "PRJXRAY_DB_DIR": str(prefix / "prjxray-db"),
        "PATH": str(prefix / "bin"),
    }
    (prefix / "bin/nextpnr-himbaechel").chmod(0o755)

    def directory(family: str = "artix7") -> Path:
        monkeypatch.setattr(xeda_check, "_TOOLCHAIN_IDS", {})
        return xeda_check.chipdb_directory(tmp_path / "w", family, env)

    first = directory()
    assert first.parent == tmp_path / "w/chipdb" and first.name.startswith("artix7-")
    assert directory() == first
    assert directory("kintex7").name.startswith("kintex7-")
    seen = {first}
    for name in ("bin/nextpnr-himbaechel", "bin/bbasm",
                 "share/nextpnr/himbaechel/uarch/xilinx/gen/xilinx_gen.py",
                 "prjxray-db/artix7/tilegrid.json"):
        (prefix / name).write_text(files[name] + " rebuilt")
        changed = directory()
        assert changed not in seen, name
        seen.add(changed)
    # another family's data is not this family's chip database
    (prefix / "prjxray-db/kintex7/tilegrid.json").write_text("grid 2")
    assert directory() in seen


def test_a_generated_verilog_file_is_compared_without_its_comments(tmp_path: Path) -> None:
    """LiteX's date comment and its module tree comment change between generations; the code
    is what yosys reads, and a change there is a change."""
    def generated(name: str, date: str, tree: str, code: str) -> bytes:
        path = tmp_path / name
        path.write_text(f"// Date      : {date}\n/*\n{tree}\n*/\nmodule soc; {code}\nendmodule\n")
        return xeda_check.generated_content(path)

    first = generated("a.v", "2026-10-06 17:00", "BB:FDCE\nBB:PLLE2_ADV", "wire a;")
    assert generated("b.v", "2026-10-06 18:00", "BB:PLLE2_ADV\nBB:FDCE", "wire a;") == first
    assert generated("c.v", "2026-10-06 17:00", "BB:FDCE\nBB:PLLE2_ADV", "wire b;") != first


def test_the_netlist_design_has_the_makefiles_netlist_for_its_hdl(fork: Path, tmp_path: Path) -> None:
    import yaml

    edit(
        fork / "mini/mini.yaml",
        "flows:\n",
        "flows:\n  nextpnr:\n    timing_allow_fail: true\n",
    )
    netlist = tmp_path / "mini.json"
    netlist.write_text("{}")
    derived = yaml.safe_load(
        xeda_check.derive_netlist_design(fork / "mini/mini.yaml", netlist, tmp_path / "out").read_text()
    )
    assert derived["name"] == "mini-from-makefile-netlist"
    assert derived["rtl"]["sources"] == [
        {"file": str(netlist), "type": "JsonNetlist"},
        str((fork / "mini/mini.xdc").resolve()),
    ]
    part = {"part": "xc7a35tcsg324-1"}
    assert derived["flows"] == {
        "nextpnr": {"fpga": part, "timing_allow_fail": True},
        "fpga_pack": {"fpga": part},
    }


# ---- --regression: xeda's build judged as run.sh judges its own ---------------------------------


def test_a_regression_case_s_own_markers_give_its_verdict(tmp_path: Path) -> None:
    for marker, kind in ((None, "pass"), ("expect_fail", "expect-fail"), ("no_route", "placed")):
        case = tmp_path / kind
        case.mkdir()
        if marker:
            (case / marker).write_text("")
        assert xeda_check.regression_verdict_kind(case) == kind


def test_the_default_regression_cases_leave_out_only_an_excluded_one(tmp_path: Path, monkeypatch) -> None:
    """With no case named, every case of run.sh's default list has a design file or an exclusion;
    a case with neither fails, and a case named on purpose needs its design file."""
    root = tmp_path / "fork"
    write(root / "regression/run.sh", 'cases=("$@"); [ ${#cases[@]} -eq 0 ] && cases=(alpha beta gamma)\n')
    for case in ("alpha", "gamma"):
        write(root / f"regression/{case}/{case}.yaml", f"name: reg-{case}\n")
    monkeypatch.setattr(xeda_check, "HERE", root)
    monkeypatch.setattr(xeda_check, "REGRESSION", root / "regression")
    monkeypatch.setattr(xeda_check, "EXCLUSIONS", root / "xeda-exclusions.yaml")
    with pytest.raises(xeda_check.SetupError, match="regression/beta has no design file"):
        xeda_check.regression_designs([])
    write(root / "xeda-exclusions.yaml", "excluded:\n  regression/beta: placement-only\n")
    assert xeda_check.regression_designs([]) == [
        root / "regression/alpha/alpha.yaml", root / "regression/gamma/gamma.yaml"
    ]
    with pytest.raises(xeda_check.SetupError, match="regression/beta has no design file"):
        xeda_check.regression_designs(["beta"])


def regression_case(root: Path, check: str | None) -> Path:
    """A case directory as upstream writes one: `expect.txt` and an executable `check.sh`."""
    case = root / "case"
    write(case / "top.v", "module top; endmodule\n")
    write(case / "expect.txt", "BUFIO_Y[0-3]\\.IN_USE\n")
    if check is not None:
        write(case / "check.sh", check)
        (case / "check.sh").chmod(0o755)
    return case


def built_run(root: Path, *, ok: bool, state: str, fasm: str | None, log: str = "") -> dict:
    """What `build_xeda` returns for a `xeda run nextpnr`: its JSON document's nodes, the FASM."""
    run = root / "run"
    write(run / "nextpnr.log", log)
    write(run / "results.json", "{}")
    built: dict = {"ok": ok, "error": None if ok else "nextpnr failed",
                   "doc": {"nodes": [{"flow": "nextpnr", "run_path": str(run), "state": state}]}}
    if fasm is not None:
        write(run / "config.fasm", fasm)
        built["fasm"] = run / "config.fasm"
    return built


def test_the_regression_judge_has_teeth(tmp_path: Path) -> None:
    """Each criterion of `run.sh` fails a build that misses it: an empty FASM, a pattern of
    `expect.txt`, `check.sh`; an expected failure that succeeds, or fails before nextpnr."""
    grep_fasm = '#!/usr/bin/env bash\ngrep -q "IN_USE" "$FASM"\n'
    case = regression_case(tmp_path, grep_fasm)
    good = "BUFIO_Y1.IN_USE\n"

    def judge(kind: str, built: dict, case: Path = case) -> bool:
        return xeda_check.judge_regression(case, kind, built, tmp_path / "check", tmp_path / "db.bin")[0]

    assert judge("pass", built_run(tmp_path, ok=True, state="ran", fasm=good))
    assert not judge("pass", built_run(tmp_path, ok=True, state="ran", fasm=""))
    assert not judge("pass", built_run(tmp_path, ok=True, state="ran", fasm="BUFIO_Y7.IN_USE\n"))
    assert not judge("pass", built_run(tmp_path, ok=False, state="failed", fasm=None))
    failing = regression_case(tmp_path / "other", '#!/usr/bin/env bash\nexit 1\n')
    assert not judge("pass", built_run(tmp_path, ok=True, state="ran", fasm=good), failing)

    warned = regression_case(
        tmp_path / "fails", '#!/usr/bin/env bash\ngrep -q "Conflicting" "$(dirname "$0")/nextpnr.log"\n'
    )
    assert judge("expect-fail", built_run(tmp_path, ok=False, state="failed", fasm=None,
                                          log="Conflicting outputs"), warned)
    assert not judge("expect-fail", built_run(tmp_path, ok=False, state="failed", fasm=None,
                                              log="something else"), warned)
    assert not judge("expect-fail", built_run(tmp_path, ok=True, state="ran", fasm=good,
                                              log="Conflicting outputs"), warned)
    assert not judge("expect-fail", built_run(tmp_path, ok=False, state="not run", fasm=None,
                                              log="Conflicting outputs"), warned)


# ---- a demo with two design files: the Arty A7-35T and A7-100T -------------------------------


def test_a_directory_is_its_own_design_file_among_others(fork: Path, monkeypatch) -> None:
    """`xeda_check.py mini` is `mini/mini.yaml` though the directory holds another design file;
    a directory with several and none of its own name asks for one."""
    monkeypatch.setattr(xeda_check, "HERE", fork)
    write(fork / "mini/mini-big.yaml", MINI_YAML.replace("name: mini", "name: mini-big"))
    assert xeda_check.designs_for(["mini"], False) == [fork / "mini/mini.yaml"]
    write(fork / "boards/boards-first.yaml", "name: boards-first\n")
    with pytest.raises(xeda_check.SetupError, match="2 design files"):
        xeda_check.designs_for(["boards"], False)


def test_the_chip_database_is_made_for_the_part_the_make_arguments_name(tmp_path: Path) -> None:
    """A design file whose `MAKE_ARGS` set `PART=` needs the chip database of that part: the
    Makefile's rule for it exists only with the same arguments."""
    scratch, chipdb = tmp_path / "demo", tmp_path / "chipdb"
    chipdb.mkdir()
    write(scratch / "Makefile", "PART = parta\nCHIPDB = ${TEST_CHIPDB}\n\n${CHIPDB}/${PART}.bin:\n\techo db > $@\n")
    env = dict(os.environ, TEST_CHIPDB=str(chipdb))
    with pytest.raises(xeda_check.SetupError, match="No rule to make target"):
        xeda_check.chipdb_ready(scratch, chipdb, "partb", env, print)
    xeda_check.chipdb_ready(scratch, chipdb, "partb", env, print, ["PART=partb"])
    assert (chipdb / "partb.bin").read_text() == "db\n"


def with_part(data: bytes, part: bytes) -> bytes:
    """*data*, a bitstream, with the part (`b`) field of its header replaced by *part*."""
    i = 13
    while chr(data[i]) != "b":
        i += 3 + int.from_bytes(data[i + 1 : i + 3], "big")
    length = int.from_bytes(data[i + 1 : i + 3], "big")
    return data[: i + 1] + len(part).to_bytes(2, "big") + part + data[i + 3 + length :]


def test_the_golden_is_compared_only_with_a_build_of_its_part(tmp_path: Path) -> None:
    """The committed `blinky.bit` of the Arty demo is the A7-35T's: an A7-100T build of the demo
    has no golden, rather than one it differs from."""
    demo = FORK / "blinky-digilent-arty"
    golden = (demo / "blinky.bit").read_bytes()
    other = tmp_path / "other.bit"
    other.write_bytes(with_part(golden, b"7a100tcsg324\0"))
    assert xeda_check.golden_verdict(demo, "blinky", other).startswith("no committed golden for this part")
    same = tmp_path / "same.bit"
    same.write_bytes(golden)
    assert "for this part" not in xeda_check.golden_verdict(demo, "blinky", same)


def test_a_dotted_part_reaches_the_netlist_design(tmp_path: Path) -> None:
    """`fpga.part: ...` in a design file's `flows.yosys_fpga` is the part, as xeda reads it."""
    import yaml

    design = tmp_path / "demo/demo-big.yaml"
    write(design, "name: demo-big\nrtl:\n  top: top\n  sources: [top.v, top.xdc]\n"
                  "flows:\n  yosys_fpga:\n    fpga.part: xc7a100tcsg324-1\n")
    derived = yaml.safe_load(xeda_check.derive_netlist_design(design, tmp_path / "n.json", tmp_path / "out").read_text())
    part = {"part": "xc7a100tcsg324-1"}
    assert derived["flows"] == {"nextpnr": {"fpga": part}, "fpga_pack": {"fpga": part}}


# ---- --synth-only: yosys on both sides --------------------------------------------------------


def test_synth_only_passes_a_design_that_is_the_makefiles(fork: Path) -> None:
    toolchain_or_skip()
    run = check(fork, "--synth-only", "mini", "--work", str(fork / "w"))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "identical" in run.stdout and "1 of 1 passed" in run.stdout


def test_all_reports_an_excluded_design_file_instead_of_building_it(fork: Path) -> None:
    """`--all` builds every design file but the excluded ones, which it reports with their
    reasons; without the exclusion, the broken design file fails the sweep."""
    toolchain_or_skip()
    for name in ("boards", "slow"):  # stand-ins that no tool can build
        shutil.rmtree(fork / name)
    write(fork / "mini/mini-broken.yaml", MINI_YAML.replace("name: mini", "name: mini-broken")
          .replace("top: mini", "top: counter"))
    write(fork / "xeda-exclusions.yaml",
          "excluded:\n  mini/mini-broken.yaml: its top is not the Makefile's\n")
    run = check(fork, "--synth-only", "--all", "--work", str(fork / "w"))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "EXCLUDED  mini/mini-broken.yaml: its top is not the Makefile's" in run.stdout
    assert "1 of 1 passed" in run.stdout
    assert "1 excluded, not built (xeda-exclusions.yaml): mini/mini-broken.yaml" in run.stdout
    (fork / "xeda-exclusions.yaml").unlink()
    run = check(fork, "--synth-only", "--all", "--work", str(fork / "w"))
    assert run.returncode == 1 and "1 of 2 passed" in run.stdout, run.stdout + run.stderr


def test_synth_only_fails_a_wrong_part(fork: Path) -> None:
    toolchain_or_skip()
    edit(fork / "mini/mini.yaml", "xc7a35tcsg324-1", "xc7a35tcsg324-3")
    run = check(fork, "--synth-only", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "part: the Makefile builds xc7a35tcsg324-1, the design file xc7a35tcsg324-3" in run.stdout


def test_synth_only_fails_a_dropped_source_that_the_netlist_shows(fork: Path) -> None:
    toolchain_or_skip()
    edit(fork / "mini/mini.yaml", "    - counter.v\n", "")
    run = check(fork, "--synth-only", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "identical" not in run.stdout
    assert "source counter.v: the Makefile reads it" in run.stdout or "FAILED" in run.stdout


def test_synth_only_fails_a_dropped_source_that_changes_nothing_in_the_netlist(fork: Path) -> None:
    """`unused.v` is never instantiated, so the netlists are identical: the comparison of the
    design file with the Makefile is what catches it."""
    toolchain_or_skip()
    edit(fork / "mini/mini.yaml", "    - unused.v\n", "")
    run = check(fork, "--synth-only", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "cells: identical" in run.stdout
    assert "source unused.v: the Makefile reads it, the design file does not" in run.stdout


def test_synth_only_fails_dropped_constraints_and_a_wrong_top(fork: Path) -> None:
    toolchain_or_skip()
    edit(fork / "mini/mini.yaml", "    - mini.xdc\n", "")
    run = check(fork, "--synth-only", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "constraints mini.xdc: the Makefile builds with it, the design file lacks it" in run.stdout


# ---- --twice and --from-makefile-netlist: failures that need no chip database -------------------


def test_twice_refuses_a_design_that_is_not_the_makefiles_before_building_it(fork: Path) -> None:
    """A wrong part would cost a chip database; a dropped source that changes nothing would pass
    a determinism check of the wrong design. Both are refused first."""
    toolchain_or_skip()
    edit(fork / "mini/mini.yaml", "xc7a35tcsg324-1", "xc7a35tcsg324-3")
    run = check(fork, "--twice", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "design file: part:" in run.stdout and "DESIGN FILE" in run.stdout
    assert not (fork / "w/xeda_run/.cache/xilinx-chipdb").exists()  # no chip database was made
    write(fork / "mini/mini.yaml", MINI_YAML)
    edit(fork / "mini/mini.yaml", "    - unused.v\n", "")
    run = check(fork, "--twice", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1 and "source unused.v" in run.stdout


def test_twice_fails_a_design_xeda_cannot_build(fork: Path) -> None:
    toolchain_or_skip()
    edit(fork / "mini/mini.yaml", "    - counter.v\n", "")  # `counter` is then undefined
    run = check(fork, "--twice", "mini", "--work", str(fork / "w"))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "BUILD FAILED" in run.stdout


# ---- the bitstream modes, on one small Artix-7 design (set XEDA_CHECK_FULL=1) -------------------


@pytest.fixture(scope="module")
def full_fork(tmp_path_factory) -> Path:
    toolchain_or_skip()
    return make_fork(tmp_path_factory.mktemp("full") / "fork")


def full(fork: Path, *arguments: str) -> subprocess.CompletedProcess:
    # one work directory for all: the chip databases are generated once
    return check(fork, *arguments, "mini", "--work", str(fork.parent / "work"))


@FULL
def test_twice_passes_a_deterministic_design(full_fork: Path) -> None:
    run = full(full_fork, "--twice")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "deterministic" in run.stdout and "identical but for the header's date and time" in run.stdout


@FULL
def test_from_makefile_netlist_passes_a_design_that_is_the_makefiles(full_fork: Path) -> None:
    run = full(full_fork, "--from-makefile-netlist")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "identical" in run.stdout


@FULL
def test_from_makefile_netlist_fails_a_wrong_part_and_dropped_constraints(full_fork: Path) -> None:
    design = full_fork / "mini/mini.yaml"
    try:
        edit(design, "xc7a35tcsg324-1", "xc7a35tcsg324-3")  # the same die, so the same chip database
        run = full(full_fork, "--from-makefile-netlist")
        assert run.returncode == 1, run.stdout + run.stderr
        assert "header differs" in run.stdout
        write(design, MINI_YAML)
        edit(design, "    - mini.xdc\n", "")
        run = full(full_fork, "--from-makefile-netlist")
        assert run.returncode == 1, run.stdout + run.stderr
        assert "identical" not in run.stdout
    finally:
        write(design, MINI_YAML)
