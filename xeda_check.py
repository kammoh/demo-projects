#!/usr/bin/env python3
"""Build a demo with the upstream Makefile and with xeda, and say whether the FASM matches.

    source /opt/openxc7/export.sh
    python xeda_check.py blinky-digilent-arty            # a demo with one design file
    python xeda_check.py picosoc/picosoc-kx2.yaml        # or a design file
    python xeda_check.py --all                           # every demo that has a design file
    python xeda_check.py --regenerate hdmi-stlv7325      # run a demo's generator (LiteX) again

For each design this runs two builds and compares what they write:

  upstream  `make` in a scratch export of the committed tree, never in this checkout (the tracked
            `blinky.json` and golden `.bit` would otherwise be taken for up-to-date targets).
  xeda      `xeda run fpga_pack` in two modes: "repository" (from this directory, so that
            `xedaproject.yaml` applies: the Makefile's own synthesis recipe) and "defaults"
            (no project file: xeda's own settings).

Verdicts. The oracle is repository mode: its FASM features (every line but comments, in order)
must equal the Makefile's, and its bitstream must equal it but for the header's date and time.
Defaults mode must build; a difference there is reported, not failed. The committed golden `.bit`
(if any) is compared the way upstream CI does, through `.github/scripts/normbit.py`, for
information. Exit status 0 when every checked design passes, 1 when one does not, 2 on a setup
error (a tool that is not openXC7's, xeda not found). Nothing here programs a device.

    python xeda_check.py --qor --all --out qor           # quality of results, not identity

`--qor` asks a different question: not "is xeda's output the Makefile's" but "is xeda's no worse".
For each design it builds the Makefile's flow (the baseline) and xeda's in the chosen modes
(`--modes defaults,repository`, default `defaults`: what a user gets with no project file), and
measures the same things from both the same way: mapped cells from the yosys netlists
(`yosys` stage), and, from nextpnr (`placed` stage), the distinct physical LUT locations of its
placement dump, flip-flops, carries, DSPs, block RAMs, and the maximum frequency of every clock.
The two stages are never compared with each other. Verdicts are better / equal / worse /
not comparable, area and timing separately, and written to `<out>.json` and `<out>.csv` with a
summary on stdout. Timing noise is not guessed: `--seeds N` runs nextpnr on each netlist with
seeds 1..N and compares the distributions. Functional correctness of a different mapping is NOT
established here; no hardware is involved.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

# loading `normbit.py` must not leave a `__pycache__` in the checkout
sys.dont_write_bytecode = True

# `make` variables a demo needs on its command line, by design file (the Makefile of `picosoc`
# selects its part by BOARD)
MAKE_ARGS: dict[str, list[str]] = {
    f"picosoc/picosoc-{board}.yaml": [f"BOARD={board}"]
    for board in ("qmtech", "genesys2", "kx2", "hpc_420t")
}

# directories next to a demo that its Makefile may name (`../vexriscv/VexRiscv.v`)
LIBRARY_DIRECTORIES = ("vexriscv", "vexriscv_smp", "serv")

TOOLS = ("yosys", "nextpnr-himbaechel", "nextpnr-xilinx", "fpga-as")

# Inputs a demo's committed files lack because they are generated, not tracked: the netlist of
# `hdmi-stlv7325` is made by LiteX (`hdmi_demo.py`), which is what both routes build. Its design
# file declares the command (`rtl.generator`: `args`, `generated_sources`) and this table names
# the design file, so that the checker runs the declared command and cannot drift from it. The
# command runs in the demo's directory (output into its git-ignored `build/`, where xeda writes
# too) when a generated file is missing or with `--regenerate`, and the files go into the scratch
# export the Makefile builds in, at the same relative paths.
#
# LiteX stamps the build date into the ROM image, which is design content, so two generations
# differ unless it is pinned: `SOURCE_DATE_EPOCH` is, for every process the checker starts (the
# Makefile's, xeda's and its generator, which inherits it). Without it the routes would build two
# different ROMs and the FASM comparison would be void. (The Verilog's own date comments are the
# wall clock regardless; `generated_content` leaves them out.)
GENERATED = {"hdmi-stlv7325": "hdmi-stlv7325.yaml"}
GENERATOR_EPOCH = "1767225600"
REGENERATE = False  # `--regenerate`: make the generated inputs again
LITEX_PYTHON: str | None = None  # `--litex-python`


class SetupError(Exception):
    pass


# ---- the toolchain ------------------------------------------------------------------------


def check_toolchain(xeda: str) -> dict[str, str]:
    """The tools that will run, which must be openXC7's: the `nextpnr-xilinx` shim and
    `fpga-as` exist only there."""
    found = {}
    for tool in (*TOOLS, xeda):
        path = shutil.which(tool)
        if path is None:
            raise SetupError(f"`{tool}` is not on PATH (source /opt/openxc7/export.sh first)")
        found[tool] = path
    prefix = Path(found["nextpnr-himbaechel"]).resolve().parent.parent
    for tool in ("yosys", "fpga-as", "nextpnr-xilinx"):
        if Path(found[tool]).resolve().parent.parent != prefix:
            raise SetupError(f"`{tool}` is {found[tool]}, not from {prefix}: openXC7's `bin` first")
    for name in ("NEXTPNR_XILINX_DIR", "PRJXRAY_DB_DIR"):
        if not os.environ.get(name):
            raise SetupError(f"{name} is not set (source /opt/openxc7/export.sh)")
    return found


# ---- the upstream build -------------------------------------------------------------------


def litex_python(given: str | None) -> str:
    """The interpreter that imports LiteX: *given*, else `$LITEX_PYTHON`, else this one."""
    candidate = given or os.environ.get("LITEX_PYTHON") or sys.executable
    probe = subprocess.run(
        [candidate, "-c", "import litex.soc, litex_boards"], cwd="/", capture_output=True
    )
    if probe.returncode:
        raise SetupError(f"{candidate} does not import litex (give --litex-python or LITEX_PYTHON)")
    return candidate


def generator_spec(demo: Path) -> dict | None:
    """What `rtl.generator` of the demo's design file declares: the program's arguments (run in
    the demo's directory) and the files it generates, relative to it. None for a demo that has no
    generated input."""
    name = GENERATED.get(demo.name)
    if name is None:
        return None
    import yaml  # not needed by a demo without generated inputs

    generator = ((yaml.safe_load((demo / name).read_text()) or {}).get("rtl") or {}).get("generator")
    if not isinstance(generator, dict) or "executable" not in generator:
        raise SetupError(f"{demo / name} has no `rtl.generator` with an `executable` and `args`")
    return {
        "executable": generator["executable"],
        "args": [str(a) for a in generator.get("args", [])],
        "files": [str(f) for f in generator.get("generated_sources", [])],
    }


def litex_shim(spec: dict, python: str | None, work: Path) -> dict[str, str]:
    """The environment for a process that runs the generator by the name the design gives it
    (`python3`): `PATH` leads with a directory whose `python3` is the interpreter that imports LiteX.
    openXC7's `export.sh` puts its own venv's `python3` first, which has no LiteX. A wrapper, not a
    link: a venv's `python` finds its packages beside the path it was started by."""
    executable = Path(spec["executable"])
    if executable.parent != Path("."):
        return {}
    shim = work / "litex-bin"
    shim.mkdir(parents=True, exist_ok=True)
    wrapper = shim / executable.name
    wrapper.write_text(f'#!/bin/sh\nexec "{litex_python(python)}" "$@"\n')
    wrapper.chmod(0o755)
    return {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}


def generate_inputs(demo: Path, work: Path, python: str | None, regenerate: bool, log) -> list[str]:
    """Make the demo's generated inputs that are missing (or all, with *regenerate*) by running
    its design file's generator in the demo's directory; returns their paths relative to it."""
    spec = generator_spec(demo)
    if spec is None:
        return []
    if regenerate or not all((demo / f).exists() for f in spec["files"]):
        command = [spec["executable"], *spec["args"]]
        log(f"generating {', '.join(spec['files'])}: {' '.join(command)}")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **litex_shim(spec, python, work))
        run = subprocess.run(command, cwd=demo, env=env, capture_output=True, text=True)
        (work / "litex.log").write_text(run.stdout + run.stderr)
        if run.returncode:
            raise SetupError(f"the generator failed ({run.returncode}); see {work / 'litex.log'}")
        missing = [f for f in spec["files"] if not (demo / f).exists()]
        if missing:
            raise SetupError(f"the generator did not write {', '.join(missing)}")
    return spec["files"]


def generated_content(path: Path) -> bytes:
    """What of a generated file is the design: all of it, but for a Verilog file's whole-line `//`
    comments. LiteX writes the wall-clock time into two of them (`// Date` and `Auto-Generated by
    LiteX on`), whatever `SOURCE_DATE_EPOCH` is; the ROM image holds the pinned build date, and
    that is design content."""
    data = path.read_bytes()
    if path.suffix != ".v":
        return data
    return b"\n".join(l for l in data.split(b"\n") if not l.lstrip().startswith(b"//"))


def generated_differ(demo: Path, scratch: Path) -> list[str]:
    """The generated inputs that are not what the Makefile's build was given: xeda generates them
    again on every run (`--clean`), so an input made under another `SOURCE_DATE_EPOCH` (or by hand
    since) leaves the two routes with different netlists, and a comparison of them means nothing."""
    spec = generator_spec(demo)
    return [
        f for f in (spec["files"] if spec else [])
        if generated_content(demo / f) != generated_content(scratch / f)
    ]


def export_tree(demo: Path, work: Path) -> Path:
    """`git archive HEAD` of the Makefile, the demo and the library directories it may name, into
    a fresh directory under *work*."""
    target = work / "upstream"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    names = ["openXC7.mk", demo.name, *LIBRARY_DIRECTORIES]
    paths = [n for n in names if (HERE / n).exists()]
    archive = subprocess.run(
        ["git", "archive", "HEAD", *paths], cwd=HERE, check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    # inputs that are generated, not tracked (made once, in the checkout): not in the archive
    for name in generate_inputs(demo, work, LITEX_PYTHON, REGENERATE, print):
        (target / demo.name / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(demo / name, target / demo.name / name)
    return target / demo.name


PROBE_MK = """.PHONY: __xeda_check_probe
__xeda_check_probe:
\t@echo "PROJECT=$(PROJECT)"
\t@echo "PART=$(PART)"
\t@echo "FAMILY=$(FAMILY)"
\t@echo "DBPART=$(DBPART)"
\t@echo "TOP_MODULE=$(TOP_MODULE)"
\t@echo "XDC=$(XDC)"
\t@echo "PNR_ARGS=$(PNR_ARGS)"
\t@echo "TOP_VERILOG=$(TOP_VERILOG)"
\t@echo "ADDITIONAL_SOURCES=$(ADDITIONAL_SOURCES)"
\t@echo "SYNTH_OPTS=$(SYNTH_OPTS)"
"""


def make_variables(directory: Path, env: dict[str, str], args: list[str]) -> dict[str, str]:
    """`PROJECT`, `PART`, `FAMILY`, `DBPART`, `TOP_MODULE`, `XDC` and `PNR_ARGS` as make evaluates
    them (never by reading the Makefile): a second makefile that only prints them."""
    probe = directory.parent / "xeda-check-probe.mk"
    probe.write_text(PROBE_MK)
    run = subprocess.run(
        ["make", "-s", "-C", str(directory), "-f", "Makefile", "-f", str(probe),
         "__xeda_check_probe", *args],
        env=env,
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in run.stdout.splitlines() if "=" in line)
    missing = [n for n in ("PROJECT", "PART", "FAMILY", "DBPART") if not values.get(n)]
    if missing:
        raise SetupError(f"make reports no {', '.join(missing)} in {directory}: {run.stderr}")
    return values


def chipdb_ready(scratch: Path, chipdb: Path, dbpart: str, env: dict[str, str], log) -> None:
    """Have the Makefile's own rule generate the part's chip database, once at a time: two checks
    of one part must not both write `<part>.bin` while the other reads it."""
    import fcntl

    target = chipdb / f"{dbpart}.bin"
    with open(chipdb / f"{dbpart}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.is_file():
            log(f"upstream: generating {target}")
            run = subprocess.run(
                ["make", "-C", str(scratch), str(target)], env=env, capture_output=True, text=True
            )
            if run.returncode:
                raise SetupError(f"chip database {target}: {run.stderr[-500:]}")


def build_upstream(
    demo: Path, work: Path, shared: Path, args: list[str], log, placement: bool = False
) -> dict:
    """The Makefile's build. With *placement*, nextpnr also writes its placement dump
    (`-o placement=`, which xeda always passes too): the physical LUTs are counted from it."""
    scratch = export_tree(demo, work)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    variables = make_variables(scratch, env, args)
    project, family = variables["PROJECT"], variables["FAMILY"]
    # every output of the committed tree goes: make would take a tracked one for up to date
    for suffix in ("json", "fasm", "bit", "frames"):
        (scratch / f"{project}.{suffix}").unlink(missing_ok=True)
    # one chip database cache for all checks, which the Makefile fills the way it always does
    chipdb = shared / "chipdb" / family
    chipdb.mkdir(parents=True, exist_ok=True)
    env[f"{family.upper()}_CHIPDB"] = str(chipdb)
    if placement:
        chipdb_ready(scratch, chipdb, variables["DBPART"], env, log)
    cmd = ["make", "-C", str(scratch), *args]
    dump = scratch / "placement.json"
    if placement:
        pnr = f"{variables.get('PNR_ARGS', '')} -o placement={dump}".strip()
        cmd.append(f"PNR_ARGS={pnr}")
    log(f"upstream: {' '.join(cmd)}  (chipdb {chipdb})")
    run = subprocess.run(cmd, env=env, capture_output=True, text=True)
    (work / "upstream.log").write_text(run.stdout + run.stderr)
    fasm, bit = scratch / f"{project}.fasm", scratch / f"{project}.bit"
    ok = run.returncode == 0 and fasm.is_file() and bit.is_file()
    return {
        "ok": ok,
        "fasm": fasm,
        "bit": bit,
        "netlist": scratch / f"{project}.json",
        "placement": dump if placement else None,
        "chipdb": chipdb / f"{variables['DBPART']}.bin",
        "xdc": scratch / variables.get("XDC", f"{project}.xdc"),
        "pnr_args": variables.get("PNR_ARGS", ""),
        "scratch": scratch,
        "env": env,
        "variables": variables,
        "log": work / "upstream.log",
        "error": None if ok else f"make exited {run.returncode}; see {work / 'upstream.log'}",
    }


# ---- the xeda build -----------------------------------------------------------------------


def build_xeda(
    design: Path, mode: str, work: Path, shared: Path, xeda: str, log, extra: tuple[str, ...] = ()
) -> dict:
    """`xeda run fpga_pack --json`, from here ("repository": the project file is found) or
    from an empty directory ("defaults"). One run root for every check, whose chip databases they
    share; the run directories of the two modes are told apart by the settings hash."""
    cwd = HERE if mode == "repository" else work / "no-project-file"
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [
        xeda,
        "run",
        "fpga_pack",
        str(design),
        "--json",
        "--clean",
        "--hashed-run-dirs",
        "--run-root",
        str(shared / "xeda_run"),
        *extra,
    ]
    log(f"xeda ({mode}): {' '.join(cmd)}  (from {cwd})")
    spec = generator_spec(design.parent)
    env = dict(os.environ, **(litex_shim(spec, LITEX_PYTHON, work) if spec else {}))
    run = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    (work / f"xeda-{mode}.log").write_text(run.stderr)
    try:
        doc = json.loads(run.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"no JSON on stdout (exit {run.returncode}); see "
            f"{work / f'xeda-{mode}.log'}",
        }
    if not doc.get("success"):
        error = doc.get("error") or {}
        return {
            "ok": False,
            "error": (
                f"{error.get('type')}: {error.get('message')}"
                if error
                else "the run failed; see its results.json"
            ),
            "doc": doc,
        }
    nextpnr = next(n for n in doc["nodes"] if n["flow"] == "nextpnr")
    routed = json.loads((Path(nextpnr["run_path"]) / "results.json").read_text())
    return {
        "ok": True,
        "doc": doc,
        "bit": Path(doc["results"]["outputs"]["bitstream"]["path"]),
        "fasm": Path(routed["outputs"]["config"]["path"]),
        "nodes": {n["flow"]: Path(n["run_path"]) for n in doc["nodes"]},
        "device": routed.get("device"),
        "fabric": routed.get("fabric"),
    }


# ---- the comparison -----------------------------------------------------------------------


def fasm_features(path: Path) -> list[str]:
    """Every FASM line that is not a comment or blank, in order."""
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def fasm_comments(path: Path) -> list[str]:
    """The comment lines, with yosys's numbering of generated names (`$abc$2027$...`) taken out:
    the same names carry other numbers when the cell library is read before the design."""
    lines = (line.strip() for line in path.read_text().splitlines())
    return [re.sub(r"\$\d+", "$N", line) for line in lines if line.startswith("#")]


def compare_fasm(upstream: Path, ours: Path) -> tuple[bool, str]:
    a, b = fasm_features(upstream), fasm_features(ours)
    if a == b:
        # what `tests/test_openxc7_real.py` compares too; the comments decide nothing here
        same = fasm_comments(upstream) == fasm_comments(ours)
        return (
            True,
            f"identical ({len(a)} features, same order; comments {'' if same else 'not '}equal)",
        )
    if sorted(a) == sorted(b):
        return False, f"the same {len(a)} features in a different order"
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    detail = "".join(f"\n      < {line}" for line in only_a[:5])
    detail += "".join(f"\n      > {line}" for line in only_b[:5])
    return False, (
        f"{len(a)} upstream features vs {len(b)}: {len(only_a)} only upstream, "
        f"{len(only_b)} only xeda{detail}"
    )


def parse_bitstream(data: bytes) -> tuple[dict[str, bytes], int, bytes]:
    """The `.bit` header's fields by tag (`a` source, `b` part, `c` date, `d` time), the length
    the `e` field states, and the configuration data after it: 13 bytes of preamble, fields with
    a 2-byte length, then `e` with a 4-byte one. Field lengths vary with the names in them, so
    nothing is found by offset. fpga-as 1.0 states 0 for `e`, which it cannot know when it
    writes to a stream, so the data is whatever follows."""
    fields: dict[str, bytes] = {}
    i = 13
    while i < len(data) and 0x61 <= data[i] <= 0x7A:
        tag = chr(data[i])
        if tag == "e":
            return fields, int.from_bytes(data[i + 1 : i + 5], "big"), data[i + 5 :]
        length = int.from_bytes(data[i + 1 : i + 3], "big")
        fields[tag] = data[i + 3 : i + 3 + length]
        i += 3 + length
    raise ValueError("no configuration data (`e` field) in the bitstream")


def compare_bitstreams(upstream: Path, ours: Path) -> tuple[bool, str]:
    """Same part and source tag, same configuration data; the date and time are the build's."""
    try:
        (ha, la, pa), (hb, lb, pb) = (parse_bitstream(p.read_bytes()) for p in (upstream, ours))
    except ValueError as error:
        return False, f"unreadable bitstream: {error}"
    differing = [t for t in sorted((ha.keys() | hb.keys()) - {"c", "d"}) if ha.get(t) != hb.get(t)]
    if differing:
        names = ", ".join(f"{t}: {ha.get(t)!r} vs {hb.get(t)!r}" for t in differing)
        return False, f"header differs ({names})"
    note = f"; the `e` length field is {la} vs {lb}" if la != lb else ""
    if pa != pb:
        words = sum(pa[i : i + 4] != pb[i : i + 4] for i in range(0, min(len(pa), len(pb)), 4))
        return False, (
            f"configuration data differs: {len(pa)} vs {len(pb)} bytes, " f"{words} words{note}"
        )
    return True, f"identical but for the header's date and time ({len(pa)} bytes of data){note}"


def golden_verdict(demo: Path, project: str, bit: Path) -> str:
    """Upstream CI's own comparison with the committed `.bit`, through its `normbit.py`, and
    what is different when it fails."""
    golden = HERE / demo.name / f"{project}.bit"
    if not golden.is_file():
        return "no committed golden"
    spec = importlib.util.spec_from_file_location("normbit", HERE / ".github/scripts/normbit.py")
    assert spec and spec.loader
    normbit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(normbit)
    if normbit.normalize(str(golden)) == normbit.normalize(str(bit)):
        return "matches the golden (normbit.py)"
    return "differs from the golden (normbit.py): " + compare_bitstreams(golden, bit)[1]


# ---- one design ---------------------------------------------------------------------------


@dataclass
class Verdict:
    design: str
    passed: bool = False
    lines: list[str] = field(default_factory=list)


def check_design(design: Path, work: Path, xeda: str, log) -> Verdict:
    demo = design.parent
    verdict = Verdict(str(design.relative_to(HERE)))
    args = MAKE_ARGS.get(verdict.design, [])
    shared = work
    work = work / design.stem
    work.mkdir(parents=True, exist_ok=True)

    upstream = build_upstream(demo, work, shared, args, log)
    if not upstream["ok"]:
        verdict.lines.append(f"upstream: BUILD FAILED: {upstream['error']}")
        return verdict
    project = upstream["variables"]["PROJECT"]
    verdict.lines.append(f"upstream: built {project}.bit for {upstream['variables']['PART']}")
    verdict.lines.append("          golden: " + golden_verdict(demo, project, upstream["bit"]))

    passed = True
    for mode in ("repository", "defaults"):
        ours = build_xeda(design, mode, work, shared, xeda, log)
        label = f"xeda {mode}:"
        if not ours["ok"]:
            verdict.lines.append(f"{label:<18}BUILD FAILED: {ours['error']}")
            passed = False
            continue
        same_fasm, fasm_text = compare_fasm(upstream["fasm"], ours["fasm"])
        same_bit, bit_text = compare_bitstreams(upstream["bit"], ours["bit"])
        required = mode == "repository"
        flag = "PASS" if same_fasm and same_bit else ("FAIL" if required else "differs")
        verdict.lines.append(
            f"{label:<18}{flag}  (device {ours['device']}, fabric "
            f"{ours['fabric']}){'' if required else '  [informational]'}"
        )
        verdict.lines.append(f"          fasm: {fasm_text}")
        verdict.lines.append(f"          bit:  {bit_text}")
        verdict.lines.append("          golden: " + golden_verdict(demo, project, ours["bit"]))
        if required and not (same_fasm and same_bit):
            passed = False
    differ = generated_differ(demo, upstream["scratch"])
    if differ:
        verdict.lines.append(
            f"generated inputs: FAIL: {', '.join(differ)} is not what the Makefile built from; "
            "the two routes built different netlists (run with --regenerate)"
        )
        passed = False
    verdict.passed = passed
    return verdict


# ---- quality of results (--qor) -----------------------------------------------------------
#
# Everything below measures an OUTPUT of a build, never how long it took, from files both
# builds leave in the same form: the yosys netlist (`write_json`), nextpnr's log and placement
# dump, the FASM and the bitstream. A baseline number and xeda's are always read by the same
# function, so a difference is the flows', not the parsers'.

# Mapped primitive footprints in 6-input LUTs, as xeda's `yosys_fpga` counts them (its
# `XILINX_LUT_FOOTPRINT`); copied rather than imported so that this script needs no xeda import.
LUT_FOOTPRINT = {
    **{f"LUT{width}": ("logic", 1) for width in range(1, 7)},
    "LUT6_2": ("logic", 2),
    "CFGLUT5": ("logic", 1),
    "INV": ("logic", 1),
    "RAM32M": ("ram", 4),
    "RAM64M": ("ram", 4),
    "RAM32M16": ("ram", 8),
    "RAM64M8": ("ram", 8),
    "RAM32X16DR8": ("ram", 8),
    "RAM64X8SW": ("ram", 8),
}
LUT_MEMORY = re.compile(r"(RAM|ROM)(\d+)X(\d+)([SD])?(?:_1)?")
SRL = re.compile(r"SRLC?(?:16|32)E?(?:_1)?")
FLIP_FLOP = re.compile(r"(FD(RE|SE|CE|PE)|LD(CE|PE))(_1)?")


def lut_footprint(cell: str) -> tuple[str, int] | None:
    known = LUT_FOOTPRINT.get(cell)
    if known is not None:
        return known
    if SRL.fullmatch(cell):
        return ("srl", 1)
    memory = LUT_MEMORY.fullmatch(cell)
    if memory is None:
        return None
    kind, depth, width, ports = memory[1], int(memory[2]), int(memory[3]), memory[4]
    luts = (width + 1) // 2 if depth <= 32 else width * (depth // 64)
    return ("ram" if kind == "RAM" else "logic", luts * (2 if ports == "D" else 1))


def netlist_cells(path: Path) -> dict[str, int]:
    """Cells by type of the design a yosys JSON netlist describes, hierarchy expanded (a
    submodule counts once per instance), so a flattened and an unflattened netlist compare."""
    doc = json.loads(path.read_text())
    modules = doc["modules"]
    tops = [n for n, m in modules.items() if "top" in m.get("attributes", {})]
    if len(tops) != 1:
        raise ValueError(f"{path}: {len(tops)} modules marked top")
    memo: dict[str, dict[str, int]] = {}

    def count(name: str) -> dict[str, int]:
        if name in memo:
            return memo[name]
        total: dict[str, int] = {}
        for cell in modules[name]["cells"].values():
            kind = cell["type"]
            # a primitive's definition is in the netlist too, as a (white)box
            attributes = modules[kind].get("attributes", {}) if kind in modules else None
            user = attributes is not None and not {"blackbox", "whitebox"} & set(attributes)
            inner = count(kind) if user else {kind.lstrip("\\"): 1}
            for k, v in inner.items():
                total[k] = total.get(k, 0) + v
        memo[name] = total
        return total

    return count(tops[0])


def yosys_stage(cells: dict[str, int]) -> dict:
    """Mapped LUT footprint, flip-flops and the cells that are neither, from cell counts."""
    luts = {"logic": 0, "ram": 0, "srl": 0}
    for cell, n in cells.items():
        got = lut_footprint(cell)
        if got:
            luts[got[0]] += got[1] * n
    return {
        "lut": sum(luts.values()),
        "lut_logic": luts["logic"],
        "lut_ram": luts["ram"],
        "lut_srl": luts["srl"],
        "ff": sum(n for c, n in cells.items() if FLIP_FLOP.fullmatch(c)),
        "carry4": cells.get("CARRY4", 0),
        "muxf": sum(n for c, n in cells.items() if re.fullmatch(r"MUXF[789]", c)),
        "dsp": cells.get("DSP48E1", 0),
        "bram18": cells.get("RAMB18E1", 0),
        "bram36": cells.get("RAMB36E1", 0),
        "cells": dict(sorted(cells.items())),
    }


MAX_FREQUENCY = re.compile(
    r"^(?:Info|Warning|ERROR): Max frequency for clock\s+'(.+)': ([\d.]+) MHz \((PASS|FAIL) at ([\d.]+) MHz\)"
)
UTILISATION = re.compile(r"^Info: \s+(\w+):\s+(\d+)/\s*(\d+)\s+\d+%")


def nextpnr_log(text: str) -> dict:
    """What nextpnr's log says: the last block of `Max frequency` lines (after routing), the
    device utilization, and its errors."""
    blocks: list[list[tuple[str, float, bool, float]]] = []
    previous = -2
    used: dict[str, int] = {}
    errors: list[str] = []
    in_util = False
    for number, line in enumerate(text.splitlines()):
        match = MAX_FREQUENCY.match(line)
        if match:
            if number != previous + 1:
                blocks.append([])
            previous = number
            blocks[-1].append(
                (match[1], float(match[2]), match[3] == "PASS", float(match[4]))
            )
            continue
        if line.startswith("Info: Device utilisation:"):
            in_util, used = True, {}
            continue
        if in_util:
            util = UTILISATION.match(line)
            if util:
                used[util[1]] = int(util[2])
                continue
            in_util = False
        # a clock that misses its target is reported as an ERROR unless `--timing-allow-fail`
        if (line.startswith("ERROR:") or line.startswith("FATAL")) and "Max frequency" not in line:
            errors.append(line.strip())
    clocks = [
        {"clock": c, "fmax": f, "met": ok, "target": t} for c, f, ok, t in (blocks[-1] if blocks else [])
    ]
    return {
        "clocks": clocks,
        "used": used,
        "errors": errors,
        "finished": "Program finished normally." in text,
    }


def physical_luts(path: Path | None) -> int | None:
    """Distinct `(tile, site, A-D)` LUT locations in nextpnr-himbaechel's placement dump: a 5LUT
    and a 6LUT position of one physical LUT count once (xeda's `lut` of its nextpnr flow)."""
    if path is None or not path.is_file():
        return None
    locations = set()
    for where in json.loads(path.read_text()).values():
        if isinstance(where, dict) and where.get("type") == "SLICE_LUTX":
            locations.add((where["tile"], where["site"], where["bel"][0]))
    return len(locations)


def clock_key(name: str) -> str:
    """A clock's name with yosys's numbering of generated names taken out, so that
    `$abc$2027$aiger$o71` of one netlist is that of another."""
    return re.sub(r"\$\d+", "$N", name)


def placed_stage(
    log_text: str, placement: Path | None, fasm: Path | None = None, bit: Path | None = None
) -> dict:
    parsed = nextpnr_log(log_text)
    used = parsed["used"]
    try:
        data_bytes = len(parse_bitstream(bit.read_bytes())[2]) if bit else None
    except (OSError, ValueError):
        data_bytes = None
    return {
        "lut": physical_luts(placement),
        "lut_positions": used.get("SLICE_LUTX"),
        "ff": used.get("SLICE_FFX"),
        "carry4": used.get("CARRY4"),
        "dsp": used.get("DSP48E1"),
        "bram18": used.get("RAMB18E1"),
        "bram36": used.get("RAMB36E1"),
        "utilization": used,
        "clocks": parsed["clocks"],
        "errors": parsed["errors"],
        "finished": parsed["finished"],
        "fasm_features": len(fasm_features(fasm)) if fasm else None,
        "bitstream_data_bytes": data_bytes,
    }


def measure(netlist: Path, log: Path, placement: Path | None, fasm: Path, bit: Path) -> dict:
    return {
        "yosys": yosys_stage(netlist_cells(netlist)),
        "placed": placed_stage(log.read_text(errors="replace"), placement, fasm, bit),
    }


def pnr_run(upstream: dict, netlist: Path, out: Path, seed: int | None = None) -> dict:
    """nextpnr on *netlist* as the Makefile runs it (its chip database, constraints and
    arguments; the placement dump xeda also writes), under *out*; the placed-stage measurements."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        "nextpnr-xilinx", "--chipdb", str(upstream["chipdb"]), "--xdc", str(upstream["xdc"]),
        "--json", str(netlist), *upstream["pnr_args"].split(),
        "-o", f"placement={out / 'placement.json'}",
    ]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    run = subprocess.run(cmd, env=upstream["env"], capture_output=True, text=True, cwd=out)
    text = run.stdout + run.stderr
    (out / "nextpnr.log").write_text(text)
    return {**placed_stage(text, out / "placement.json"), "returncode": run.returncode}


def seed_sweep(netlist: Path, upstream: dict, seeds: int, out: Path, log) -> list[dict]:
    """nextpnr on *netlist* with seeds 1..*seeds*: the placed stage of each run."""
    results = []
    for seed in range(1, seeds + 1):
        results.append({"seed": seed, **pnr_run(upstream, netlist, out / f"seed{seed}", seed)})
        log(f"  seed {seed} of {out.parent.name}/{netlist.name}: exit {results[-1]['returncode']}")
    return results


# Perturbations that cannot change a design's quality, only the numbering yosys gives generated
# names (`autoidx`), which abc9's mapping is sensitive to: each reads the primitive library
# `synth_xilinx` itself reads (same content) once more, or reads an unused module, or reads the
# additional sources in the other order.
NOISE_VARIANTS = ("reads1", "reads2", "reads3", "unused-module", "unused-lib", "reversed-order")


def noise_variants(base: dict, out: Path, log) -> dict[str, dict]:
    """The Makefile's yosys recipe under each perturbation, then nextpnr, each measured as the
    baseline is. Variants that would be the baseline itself are left out."""
    variables, scratch = base["variables"], base["scratch"]
    top = variables.get("TOP_MODULE") or variables["PROJECT"]
    files = [variables["TOP_VERILOG"], *variables.get("ADDITIONAL_SOURCES", "").split()]
    results = {}
    for name in NOISE_VARIANTS:
        order = files
        if name == "reversed-order":
            if len(files) < 3:
                continue
            order = [files[0], *reversed(files[1:])]
        prefix = ""
        if name.startswith("reads"):
            prefix = "read_verilog -lib -specify +/xilinx/cells_sim.v; " * int(name[5:])
        directory = out / name
        directory.mkdir(parents=True, exist_ok=True)
        if name == "unused-module":
            (directory / "unused.v").write_text("module xeda_check_unused(input a, output b); assign b = a; endmodule\n")
            prefix = f"read_verilog -defer {directory / 'unused.v'}; "
        elif name == "unused-lib":
            (directory / "unused.v").write_text("(* blackbox *) module xeda_check_unused(input a, output b); endmodule\n")
            prefix = f"read_verilog -lib {directory / 'unused.v'}; "
        netlist = directory / "netlist.json"
        script = (f"{prefix}synth_xilinx -flatten -abc9 {variables.get('SYNTH_OPTS', '')} -arch xc7 "
                  f"-top {top}; write_json {netlist}")
        run = subprocess.run(["yosys", "-q", "-l", str(directory / "yosys.log"), "-p", script, *order],
                             env=base["env"], cwd=scratch, capture_output=True, text=True)
        if run.returncode or not netlist.is_file():
            results[name] = {"error": f"yosys exited {run.returncode}: {run.stderr[-300:]}"}
            continue
        placed = pnr_run(base, netlist, directory / "pnr")
        results[name] = {"yosys": yosys_stage(netlist_cells(netlist)), "placed": placed}
        log(f"  noise {name} of {out.parent.name}: lut {placed['lut']} ff {placed['ff']}")
    return results


def noise_band(measures: list[dict]) -> dict:
    """Lowest and highest of each area resource, per stage, over the baseline and its
    perturbations, and of each clock's frequency."""
    band: dict = {}
    for stage, keys in (("yosys", YOSYS_KEYS), ("placed", AREA_KEYS)):
        band[stage] = {}
        for k in keys:
            values = [m[stage][k] for m in measures if m.get(stage) and m[stage].get(k) is not None]
            if values:
                band[stage][k] = (min(values), max(values))
    clocks: dict[str, list[float]] = {}
    for m in measures:
        for c in m.get("placed", {}).get("clocks", []):
            clocks.setdefault(clock_key(c["clock"]), []).append(c["fmax"])
    band["clocks"] = {k: (min(v), max(v)) for k, v in clocks.items()}
    return band


def qor_xeda_mode(design: Path, mode: str, work: Path, shared: Path, xeda: str, log) -> dict:
    built = build_xeda(design, mode, work, shared, xeda, log)
    missed = False
    if not built["ok"] and "timing constraints are not met" in built["error"]:
        # xeda's nextpnr failed the design's constraints, which the baseline met (or its Makefile
        # allows the failure): measure the run anyway, and count the failure against xeda
        missed = True
        built = build_xeda(
            design, mode, work, shared, xeda, log, ("-s", "flows.nextpnr.timing_allow_fail=true")
        )
    if not built["ok"]:
        return {"ok": False, "error": built["error"]}
    nodes = built["nodes"]
    synth, pnr = nodes["yosys_fpga"], nodes["nextpnr"]
    reported = json.loads((pnr / "results.json").read_text())
    ours = measure(
        synth / "netlist.json",
        pnr / "nextpnr.log",
        pnr / "placement.json",
        built["fasm"],
        built["bit"],
    )
    ours.update(
        ok=True,
        missed_constraints=missed,
        netlist=synth / "netlist.json",
        reported={k: reported.get(k) for k in ("lut", "ff", "Fmax", "timing_met")},
    )
    return ours


# ---- the verdicts --------------------------------------------------------------------------

# What an area verdict rests on: nextpnr's own utilization report (`SLICE_LUTX` is the LUT
# positions it occupies, 5LUT and 6LUT counted separately), never an interpretation of yosys's
# cells, which depends on the installed yosys. The yosys stage is context, judged by nothing.
AREA_KEYS = ("lut_positions", "ff", "carry4", "dsp", "bram18", "bram36")
# relative width the noise band of LUT positions is widened by (see `compare_area`)
AREA_FLOOR = 0.01
YOSYS_KEYS = ("lut", "ff", "carry4", "dsp", "bram18", "bram36")


def compare_area(
    base: dict, ours: dict, band: dict | None = None, keys: tuple[str, ...] = AREA_KEYS
) -> tuple[str, dict, str]:
    """Verdict on one stage's area. Every resource of *AREA_KEYS* both stages report is a
    difference (ours - baseline); lower is better. Given the *band* each resource moves in under
    quality-neutral perturbations of the baseline (`--noise`: its lowest and highest value), a
    resource is higher only above the band and lower only below it. `better`: none higher, one
    lower; `equal`: none either (differences inside the band are equal, within the noise);
    `worse`: none lower, one higher; `mixed`: one of each, which is reported as `worse` only
    if the LUTs or the flip-flops are what rose."""
    delta = {
        k: ours[k] - base[k] for k in keys if base.get(k) is not None and ours.get(k) is not None
    }

    def edges(k: str) -> tuple[float, float]:
        low, high = band[k] if band and k in band else (base[k], base[k])
        # the perturbations are a handful of samples of a heavy-tailed effect (LUT positions moved
        # up to 3.6% in them, usually under 0.5%): the band is widened by a floor. FF, DSP and
        # BRAM counts did not move at all, and keep none.
        floor = {"lut_positions": AREA_FLOOR * base[k], "carry4": max(2.0, AREA_FLOOR * base[k])}
        return low - floor.get(k, 0.0), high + floor.get(k, 0.0)

    up = [k for k in delta if ours[k] > edges(k)[1]]
    down = [k for k in delta if ours[k] < edges(k)[0]]
    if not up and not down:
        inside = [k for k, v in delta.items() if v]
        return "equal", delta, (f"differs inside the noise band: {', '.join(inside)}" if inside else "")
    if not up:
        return "better", delta, ""
    if not down:
        return "worse", delta, ""
    note = "mixed: " + ", ".join(f"{k} {delta[k]:+d}" for k in (*up, *down))
    return ("worse" if {"lut_positions", "lut", "ff"} & set(up) else "better"), delta, note


def clock_pairs(base: list[dict], ours: list[dict]) -> tuple[list[tuple[dict, dict]], list[str]]:
    """Clocks of the two builds that are the same clock: by name (yosys's numbering taken out),
    and when a name occurs more than once in both, by rank of frequency. A clock's name is the
    name yosys kept for its net, which differs between recipes (`sys_clk` or
    `main_crg_clkout_buf0`), so the clocks left over are paired by their target frequency (the
    constraint is the design's, the same for both) and, among equal targets, by rank of
    frequency, when each side has the same number of them at that target; the rest are returned
    unmatched."""
    groups: dict[str, tuple[list[dict], list[dict]]] = {}
    for side, clocks in enumerate((base, ours)):
        for clock in clocks:
            groups.setdefault(clock_key(clock["clock"]), ([], []))[side].append(clock)
    pairs: list[tuple[dict, dict]] = []
    left: tuple[list[dict], list[dict]] = ([], [])
    for key, (a, b) in sorted(groups.items()):
        if a and len(a) == len(b):
            pairs += list(zip(sorted(a, key=lambda c: c["fmax"]), sorted(b, key=lambda c: c["fmax"])))
        else:
            left[0].extend(a)
            left[1].extend(b)
    unmatched = []
    targets = {c["target"] for side in left for c in side}
    for target in sorted(targets):
        a = sorted((c for c in left[0] if c["target"] == target), key=lambda c: c["fmax"])
        b = sorted((c for c in left[1] if c["target"] == target), key=lambda c: c["fmax"])
        if len(a) == len(b):
            pairs += list(zip(a, b))
        else:
            unmatched += [f"{c['clock']}@{target:g}MHz" for c in (*a, *b)]
    return pairs, unmatched


#: nextpnr's target for a clock the constraints do not name
DEFAULT_TARGET_MHZ = 12.0


def compare_timing(base: dict, ours: dict, tolerance: float) -> tuple[str, list, str]:
    """Verdict on timing from the clocks after routing. A clock is `worse` when the baseline met
    its target and xeda does not, or when xeda's maximum frequency is below the baseline's by more
    than the noise, `better` when it is above by as much, else `equal`. The noise is the larger
    of *tolerance* (a fraction of the frequency) and, outside the range the baseline's clock took
    (`low`..`high`: over `--seeds` seeds, and over the perturbed netlists of `--noise`), nothing:
    xeda's clock is `worse` only when it is below that whole range. The design is the worst of its clocks, `better` only
    if none is worse. Only clocks that count are judged: one nextpnr was given no target for
    (its achieved frequency is then the figure of merit), or one that is near its constraint.
    A clock xeda's run misses where the baseline's did not is `worse` always."""
    pairs, unmatched = clock_pairs(base["clocks"], ours["clocks"])
    if not base["clocks"] or not ours["clocks"]:
        return "not comparable", [], "nextpnr reported no clock frequency"
    rows, kinds = [], []
    for a, b in pairs:
        noise = max(a.get("noise", 0.0), b.get("noise", 0.0), tolerance * a["fmax"])
        low, high = a.get("low", a["fmax"]), a.get("high", a["fmax"])
        if a["met"] and not b["met"]:
            kind = "worse"
        elif b["fmax"] < low - noise:
            kind = "worse"
        elif b["fmax"] > high + noise:
            kind = "better"
        else:
            kind = "equal"
        # a constrained clock that both builds beat three times over is headroom, not timing
        counted = a["target"] == DEFAULT_TARGET_MHZ or min(a["fmax"], b["fmax"]) < 3 * a["target"]
        if counted:
            kinds.append(kind)
        rows.append({
            "clock": a["clock"], "target": a["target"], "base_fmax": a["fmax"], "base_met": a["met"],
            "our_fmax": b["fmax"], "our_met": b["met"], "our_target": b["target"],
            "base_low": low, "base_high": high, "noise": round(noise, 2), "verdict": kind, "counted": counted,
        })
    if not rows:
        return "not comparable", rows, "no clock of the two builds has the same name"
    note = f"unmatched clocks: {', '.join(unmatched)}" if unmatched else ""
    if not kinds:
        return "equal", rows, (note + "; every clock has 3x headroom").lstrip("; ")
    if "worse" in kinds:
        return "worse", rows, note
    return ("better" if "better" in kinds else "equal"), rows, note


def sweep_clocks(sweep: list[dict]) -> list[dict]:
    """The clocks of a seed sweep as one list: each clock's median frequency, whether most seeds
    met it, and the lowest and highest frequency over the seeds (`low`/`high`, which a verdict
    reads as the noise)."""
    import statistics

    runs = []
    for run in sweep:
        seen: dict[str, list[dict]] = {}
        for clock in sorted(run["clocks"], key=lambda c: c["fmax"]):
            seen.setdefault(clock_key(clock["clock"]), []).append(clock)
        runs.append({
            (k if len(v) == 1 else f"{k}#{i}"): c for k, v in seen.items() for i, c in enumerate(v)
        })
    keys = set.intersection(*(set(r) for r in runs)) if runs else set()
    out = []
    for key in sorted(keys):
        freqs = [r[key]["fmax"] for r in runs]
        out.append({
            "clock": key,
            "fmax": statistics.median(freqs),
            "met": sum(r[key]["met"] for r in runs) * 2 > len(runs),
            "target": runs[0][key]["target"],
            "low": min(freqs),
            "high": max(freqs),
        })
    return out


def verdict_of(area: str, timing: str) -> str:
    """The worse of the two; a design with no timing information is judged on its area."""
    if timing == "not comparable" and area != "not comparable":
        timing = "equal"
    if "worse" in (area, timing):
        return "worse"
    if "not comparable" in (area, timing):
        return "not comparable"
    if "better" in (area, timing):
        return "better"
    return "equal"


def compare_qor(
    base: dict, ours: dict, tolerance: float, band: dict | None = None, base_args: str = ""
) -> dict:
    out: dict = {}
    # the two stages are compared each with its own kind, never with each other
    band = band or {}
    area_y, delta_y, note_y = compare_area(base["yosys"], ours["yosys"], band.get("yosys"), YOSYS_KEYS)
    area_p, delta_p, note_p = compare_area(base["placed"], ours["placed"], band.get("placed"))
    timing, clocks, note_t = compare_timing(base["placed"], ours["placed"], tolerance)
    out.update(
        area_yosys=area_y, delta_yosys=delta_y, area_placed=area_p, delta_placed=delta_p,
        timing=timing, clocks=clocks,
    )
    notes = [n for n in (note_p and f"nextpnr {note_p}", note_t) if n]
    if ours.get("missed_constraints") and "--timing-allow-fail" not in base_args:
        # the baseline's Makefile does not allow it: its run failed the build, xeda's would
        timing = out["timing"] = "worse"
        notes.append("xeda's nextpnr FAILED the timing constraints (the build fails)")
    if ours["placed"]["errors"] or not ours["placed"]["finished"]:
        notes.append("nextpnr errors in xeda's log")
    if base["placed"]["errors"]:
        notes.append("nextpnr errors in the baseline's log")
    if ours["placed"]["bitstream_data_bytes"] != base["placed"]["bitstream_data_bytes"]:
        notes.append("bitstream sizes differ")
    # the placed stage is the one that is built; the yosys stage explains it
    if area_y != area_p:
        notes.append(f"(context) yosys-stage area is {area_y}")
    out["verdict"] = verdict_of(area_p, timing)
    out["notes"] = "; ".join(notes)
    return out


# ---- one design, quality of results ----------------------------------------------------------


def qor_design(
    design: Path, work: Path, xeda: str, modes: list[str], seeds: int, tolerance: float, noise: bool, log
) -> dict:
    """The baseline's measurements, and xeda's in each mode with the verdicts against it."""
    demo = design.parent
    name = str(design.relative_to(HERE))
    args = MAKE_ARGS.get(name, [])
    shared = work
    work = work / design.stem
    work.mkdir(parents=True, exist_ok=True)
    row: dict = {"design": name, "modes": {}}
    try:
        base = build_upstream(demo, work, shared, args, log, placement=True)
    except SetupError as error:
        row["error"] = f"baseline: {error}"
        return row
    row["part"] = base["variables"]["PART"]
    if not base["ok"]:
        row["error"] = f"baseline: BUILD FAILED: {base['error']}"
        return row
    baseline = measure(base["netlist"], base["log"], base["placement"], base["fasm"], base["bit"])
    baseline["pnr_args"] = base["pnr_args"]
    sweeps = {}
    if seeds:
        sweeps["baseline"] = seed_sweep(base["netlist"], base, seeds, work, log)
        baseline["placed"]["single_seed_clocks"] = baseline["placed"]["clocks"]
        baseline["placed"]["clocks"] = sweep_clocks(sweeps["baseline"])
    band = None
    if noise:
        variants = noise_variants(base, work / "noise", log)
        row["noise"] = variants
        measures = [baseline] + [v for v in variants.values() if "error" not in v]
        for sweep in (sweeps.get("baseline", []),):  # seeds are noise in frequency too
            measures += [{"placed": r} for r in sweep]
        band = noise_band(measures)
        row["noise_band"] = band
        for clock in baseline["placed"]["clocks"]:
            low, high = band["clocks"].get(clock_key(clock["clock"]), (None, None))
            if low is not None:
                clock["low"], clock["high"] = low, high
    row["baseline"] = baseline
    for mode in modes:
        ours = qor_xeda_mode(design, mode, work, shared, xeda, log)
        if not ours["ok"]:
            row["modes"][mode] = {
                "ok": False, "error": ours["error"], "verdict": "worse",
                "notes": "BUILD FAILED where the baseline's succeeded",
            }
            continue
        if seeds:
            sweeps[mode] = seed_sweep(ours["netlist"], base, seeds, work, log)
            ours["placed"]["single_seed_clocks"] = ours["placed"]["clocks"]
            ours["placed"]["clocks"] = sweep_clocks(sweeps[mode])
        ours.pop("netlist")
        row["modes"][mode] = {**ours, **compare_qor(baseline, ours, tolerance, band, base['pnr_args'])}
        differ = generated_differ(demo, base["scratch"])
        if differ:
            # xeda generated its inputs again (`--clean`) and they are not the baseline's
            row["modes"][mode]["verdict"] = "not comparable"
            row["modes"][mode]["notes"] = (
                f"{', '.join(differ)} is not what the baseline built from: different netlists "
                f"(run with --regenerate); " + row["modes"][mode]["notes"]
            )
    return row


def csv_rows(rows: list[dict]) -> list[dict]:
    """One flat line per design and mode: both stages' resources side by side."""
    out = []
    for row in rows:
        base = row.get("baseline")
        for mode, ours in row["modes"].items():
            line = {"design": row["design"], "part": row.get("part"), "mode": mode,
                    "verdict": ours.get("verdict"), "notes": ours.get("notes", "")}
            if base and ours.get("ok"):
                line.update(area_placed=ours["area_placed"], area_yosys=ours["area_yosys"],
                            timing=ours["timing"])
                for stage, keys in (("yosys", YOSYS_KEYS), ("placed", (*AREA_KEYS, "lut"))):
                    for k in keys:
                        b, o = base[stage].get(k), ours[stage].get(k)
                        line[f"{stage}_{k}_base"], line[f"{stage}_{k}_xeda"] = b, o
                        line[f"{stage}_{k}_delta"] = None if b is None or o is None else o - b
                slow = min(ours["clocks"], key=lambda c: c["base_fmax"], default=None)
                if slow:
                    line.update(slowest_clock=slow["clock"], slowest_base_fmax=slow["base_fmax"],
                                slowest_xeda_fmax=slow["our_fmax"])
                line.update(base_fasm=base["placed"]["fasm_features"],
                            xeda_fasm=ours["placed"]["fasm_features"])
            elif not ours.get("ok", True):
                line["notes"] = ours.get("error", "")
            out.append(line)
        if not row["modes"]:
            out.append({"design": row["design"], "part": row.get("part"), "verdict": "not comparable",
                        "notes": row.get("error", "")})
    return out


def print_qor_summary(rows: list[dict]) -> None:
    print(f"\n{'design':<36}{'mode':<12}{'SLICE_LUTX b>x':>16}{'SLICE_FFX b>x':>16}"
          f"{'slowest clock base>xeda MHz':>34}  verdict")
    for row in rows:
        base = row.get("baseline")
        for mode, ours in row["modes"].items():
            if not (base and ours.get("ok")):
                print(f"{Path(row['design']).stem:<36}{mode:<12}  {ours.get('verdict')}: {ours.get('error')}")
                continue
            b, o = base["placed"], ours["placed"]
            slow = min(ours["clocks"], key=lambda c: c["base_fmax"], default=None)
            clock = f"{slow['base_fmax']:.1f} > {slow['our_fmax']:.1f}" if slow else "-"
            print(f"{Path(row['design']).stem:<36}{mode:<12}{b['lut_positions']!s:>8} >{o['lut_positions']!s:>6}{b['ff']!s:>8} >{o['ff']!s:>6}"
                  f"{clock:>34}  {ours['verdict']} (area {ours['area_placed']}, timing {ours['timing']})")
        if not row["modes"]:
            print(f"{row['design']:<36}{'':<12}  not comparable: {row.get('error')}")


def rejudge(rows: list[dict], tolerance: float) -> list[dict]:
    """The verdicts of rows `--qor` measured, decided again from their measurements (after a
    change of the rules): nothing is built."""
    for row in rows:
        if "baseline" not in row:
            continue
        base = row["baseline"]
        measures = [base] + [v for v in row.get("noise", {}).values() if "error" not in v]
        band = noise_band(measures) if row.get("noise") else None
        for clock in base["placed"]["clocks"]:
            clock.pop("low", None), clock.pop("high", None)
            low, high = (band or {}).get("clocks", {}).get(clock_key(clock["clock"]), (None, None))
            if low is not None:
                clock["low"], clock["high"] = low, high
        row["noise_band"] = band
        for mode, ours in row["modes"].items():
            if ours.get("ok"):
                ours.update(compare_qor(base, ours, tolerance, band, base.get('pnr_args', '')))
    return rows



def run_qor(options, designs: list[Path]) -> int:
    from concurrent.futures import ThreadPoolExecutor

    work = options.work.resolve()
    modes = [m.strip() for m in options.modes.split(",") if m.strip()]
    unknown = set(modes) - {"defaults", "repository"}
    if unknown:
        raise SetupError(f"unknown mode {', '.join(sorted(unknown))}: defaults, repository")

    def log(message: str) -> None:
        print(f"  .. {message}", file=sys.stderr, flush=True)

    def one(design: Path) -> dict:
        try:
            return qor_design(design, work, options.xeda, modes, options.seeds, options.tolerance, options.noise, log)
        except Exception as error:  # one design's trouble must not lose the others' rows
            return {"design": str(design.relative_to(HERE)), "modes": {},
                    "error": f"{type(error).__name__}: {error}"}

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        rows = list(pool.map(one, designs))
    out = Path(options.out)
    out.with_suffix(".json").write_text(json.dumps(rows, indent=1, default=str))
    flat = csv_rows(rows)
    columns = list(dict.fromkeys(k for line in flat for k in line))
    import csv

    with open(out.with_suffix(".csv"), "w", newline="") as handle:
        writer = csv.DictWriter(handle, columns)
        writer.writeheader()
        writer.writerows(flat)
    print_qor_summary(rows)
    worse = [(r["design"], m) for r in rows for m, o in r["modes"].items() if o.get("verdict") == "worse"]
    print(f"\n{len(worse)} worse than the baseline: " + ", ".join(f"{d} ({m})" for d, m in worse))
    return 1 if worse else 0



def designs_for(names: list[str], everything: bool) -> list[Path]:
    if everything:
        return sorted(
            p
            for p in HERE.glob("*/*.yaml")
            if p.stem == p.parent.name or p.stem.startswith(p.parent.name + "-")
        )
    found = []
    for name in names:
        path = Path(name)
        if path.suffix in (".yaml", ".yml"):
            found.append(path.resolve())
            continue
        candidates = sorted((HERE / name).glob("*.yaml"))
        if len(candidates) != 1:
            raise SetupError(f"{name}: {len(candidates)} design files; name one of them")
        found.append(candidates[0])
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("designs", nargs="*", help="a demo directory or a design file")
    parser.add_argument("--all", action="store_true", help="every demo with a design file")
    parser.add_argument(
        "--work",
        type=Path,
        default=HERE / ".xeda-check",
        help="scratch directory (git-ignored); keeps the chip databases",
    )
    parser.add_argument("--xeda", default="xeda", help="the xeda executable")
    parser.add_argument("--regenerate", action="store_true",
                        help="run the generator of a demo with generated inputs, though they exist")
    parser.add_argument("--litex-python", metavar="PYTHON",
                        help="the interpreter that imports LiteX (default: $LITEX_PYTHON, else this one)")
    parser.add_argument("--qor", action="store_true", help="compare area and timing, not identity")
    parser.add_argument("--modes", default="defaults", help="--qor: xeda's modes, comma-separated")
    parser.add_argument("--seeds", type=int, default=0, help="--qor: nextpnr seeds per netlist")
    parser.add_argument("--tolerance", type=float, default=0.05,
                        help="--qor: Fmax difference, as a fraction, that counts as noise")
    parser.add_argument("--noise", action="store_true",
                        help="--qor: first measure the baseline's own spread under perturbations "
                        "that cannot change quality; differences inside it are `equal`")
    parser.add_argument("--rejudge", metavar="JSON",
                        help="decide the verdicts of an earlier --qor run's <out>.json again")
    parser.add_argument("--jobs", type=int, default=1, help="--qor: designs built at once")
    parser.add_argument("--out", default="qor", help="--qor: write <out>.json and <out>.csv")
    options = parser.parse_args()
    global REGENERATE, LITEX_PYTHON
    REGENERATE, LITEX_PYTHON = options.regenerate, options.litex_python
    # every process below inherits it: see GENERATED
    os.environ.setdefault("SOURCE_DATE_EPOCH", GENERATOR_EPOCH)
    if options.rejudge:
        rows = rejudge(json.loads(Path(options.rejudge).read_text()), options.tolerance)
        Path(options.out).with_suffix(".json").write_text(json.dumps(rows, indent=1, default=str))
        print_qor_summary(rows)
        return 0
    if not options.designs and not options.all:
        parser.error("name a demo, or give --all")
    try:
        tools = check_toolchain(options.xeda)
        designs = designs_for(options.designs, options.all)
    except SetupError as error:
        print(f"setup: {error}", file=sys.stderr)
        return 2
    print("tools: " + ", ".join(f"{name}={path}" for name, path in tools.items()))
    options.work.mkdir(parents=True, exist_ok=True)
    if options.qor:
        return run_qor(options, designs)

    def log(message: str) -> None:
        print(f"  .. {message}", file=sys.stderr, flush=True)

    verdicts = []
    for design in designs:
        verdict = check_design(design, options.work.resolve(), options.xeda, log)
        verdicts.append(verdict)
        print(f"\n{'PASS' if verdict.passed else 'FAIL'}  {verdict.design}")
        print("\n".join(f"  {line}" for line in verdict.lines))
    print(f"\n{sum(v.passed for v in verdicts)} of {len(verdicts)} passed")
    return 0 if all(v.passed for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
