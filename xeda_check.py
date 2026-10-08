#!/usr/bin/env python3
"""Build a demo with the upstream Makefile and with xeda, and say whether the FASM matches.

    source /opt/openxc7/export.sh
    python xeda_check.py blinky-digilent-arty            # a demo with one design file
    python xeda_check.py picosoc/picosoc-kx2.yaml        # or a design file
    python xeda_check.py --all                           # every design file, but those excluded
    python xeda_check.py --regenerate hdmi-stlv7325      # run a demo's generator (LiteX) again
    python xeda_check.py --ci-list                       # which of upstream's CI projects lack a design
    python xeda_check.py --synth-only --all --jobs 3     # yosys on both sides, minutes, no chip database
    python xeda_check.py --twice blinky-digilent-arty    # two clean xeda builds, bitstreams equal
    python xeda_check.py --from-makefile-netlist picosoc/picosoc-kx2.yaml   # xeda's P&R on the Makefile's netlist
    python xeda_check.py --regression                    # run.sh's default regression cases, both ways

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

The four modes above the QoR one each ask one narrower question; the design file is the same.

  --ci-list              builds nothing. Reads `.github/workflows/smoke.yml` and `heavy.yml` (the
                         project matrix, the determinism matrix, the regression job, whose cases are
                         the default list of `regression/run.sh`) and prints every project with its
                         state: `design file`, `excluded: <reason>` (`xeda-exclusions.yaml`) or
                         `missing`. A job's `BOARD` picks picosoc's design file (`picosoc-kx2.yaml`).
                         Exit 1 on a `missing` project, on a project that is both excluded and has a
                         design file, 2 on an exclusion naming a project upstream does not build.
                         `xeda-exclusions.yaml` may also name a design file (`<dir>/<file>.yaml`) of
                         a project upstream's CI does not build: `--all` reports it as excluded,
                         with its reason, instead of building it (naming it still builds it).
  --synth-only           yosys on both sides and no chip database: `make <project>.json` in the
                         scratch export, and `xeda run yosys_fpga` (`--mode`: repository, the default,
                         or defaults); the cell-type counts of the two netlists must be equal, and the
                         design file must agree with the Makefile on part, top module, every HDL
                         source (in the Makefile's order) and the constraints file, as xeda resolved
                         them. Minutes for every design file (`--all --jobs 3`).
  --twice                two clean xeda builds of the design, FASM features and bitstream (but for the
                         header's date and time) equal: upstream's determinism job, on xeda. With no
                         design, the projects of upstream's determinism matrix. A design that
                         disagrees with the Makefile (as in --synth-only) is refused before a build:
                         determinism of the wrong design says nothing.
  --from-makefile-netlist  xeda's `nextpnr` and `fpga_pack` on the Makefile's own netlist (a derived
                         design file: a typed `JsonNetlist` source, the design's constraints and
                         `fpga` given to both flows), compared with the Makefile's FASM and bitstream.
                         It leaves synthesis out: a design that fails the normal check and passes
                         this one differs from the Makefile in synthesis alone.
  --regression           the regression cases (`regression/<case>/<case>.yaml`; with no case named,
                         the default list of `regression/run.sh`): `run.sh <case>` in a scratch
                         export gives upstream's verdict, and xeda's build of the design file
                         (`xeda run nextpnr`, both modes) is judged by the case's own files the
                         same way (a non-empty FASM, `expect.txt`, `check.sh`; `expect_fail`: nextpnr
                         must fail). Repository mode must give run.sh's verdict. A placement-only
                         case (`no_route`) has no FASM and is not judged.

Per-design arguments for xeda (`timing_allow_fail`, `extra_args`, `synth_flags`) are the design
file's own; the checker adds none. The one thing it passes to `make` is `MAKE_ARGS`: picosoc's
`BOARD=`, which its Makefile selects the part by and for which xeda has no counterpart, and the
`PART=` of a design file that builds a demo for another part than its Makefile names (the Arty
A7-100T, and the speed grade -2 of the STLV7325 v2).
`tests/test_xeda_check.py` is the oracle of all of this: a broken design file fails each mode.

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
import copy
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent

# loading `normbit.py` must not leave a `__pycache__` in the checkout
sys.dont_write_bytecode = True

# `make` variables a demo needs on its command line, by design file: the Makefile of `picosoc`
# selects its part by BOARD, and a design file that names another part than its Makefile (the Arty
# A7-100T, and the speed grade -2 of the STLV7325 v2) builds the demo with that PART
MAKE_ARGS: dict[str, list[str]] = {
    **{
        f"picosoc/picosoc-{board}.yaml": [f"BOARD={board}"]
        for board in ("qmtech", "genesys2", "kx2", "hpc_420t")
    },
    "blinky-digilent-arty/blinky-digilent-arty-a7-100.yaml": ["PART=xc7a100tcsg324-1"],
    **{
        f"{demo}/{demo}.yaml": ["PART=xc7k325tffg676-2"]
        for demo in ("blinky-stlv7325", "hdmi-stlv7325", "litex-ddr-hdmi-stlv7325")
    },
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
# wall clock regardless, and its module tree comment is in no fixed order; `generated_content`
# leaves the comments out.)
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
    interpreter = litex_python(python)
    executable = Path(spec["executable"])
    if executable.parent != Path("."):
        return {}
    shim = work / "litex-bin"
    shim.mkdir(parents=True, exist_ok=True)
    wrapper = shim / executable.name
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(interpreter)} "$@"\n')
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
    """What of a generated file is the design: all of it, but for a Verilog file's comments, which
    yosys does not read. LiteX writes the wall-clock time into two whole-line `//` comments (`// Date`
    and `Auto-Generated by LiteX on`), whatever `SOURCE_DATE_EPOCH` is, and the module tree of the
    SoC into a `/* ... */` block, in an order that changes from one generation to the next. The ROM
    image holds the pinned build date, and that is design content."""
    data = path.read_bytes()
    if path.suffix != ".v":
        return data
    data = re.sub(rb"/\*.*?\*/", b"", data, flags=re.S)
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


def _digest_tree(digest, root: Path) -> None:
    """Every file under *root*, by its path relative to it and its content, in a fixed order."""
    import hashlib

    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())


_TOOLCHAIN_IDS: dict[str, str] = {}


def toolchain_identity(family: str, env: dict[str, str]) -> str:
    """What the Makefile's chip database of a *family* is made from, as a digest: the content of
    `nextpnr-himbaechel` (which reads the database), `bbasm` and the generator tree (which write
    it) and the family's Project X-Ray data. A chip database made by another toolchain is never
    reused: the two sides of a check must place with databases of the same toolchain (xeda's
    cache key hashes the same inputs)."""
    import hashlib

    if family in _TOOLCHAIN_IDS:
        return _TOOLCHAIN_IDS[family]
    prefix = Path(env["NEXTPNR_XILINX_DIR"])
    nextpnr = shutil.which("nextpnr-himbaechel", path=env.get("PATH"))
    if nextpnr is None:
        raise SetupError("`nextpnr-himbaechel` is not on PATH")
    digest = hashlib.sha256()
    for path in (Path(nextpnr).resolve(), prefix / "bin" / "bbasm"):
        digest.update(path.name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    _digest_tree(digest, prefix / "share" / "nextpnr" / "himbaechel")
    _digest_tree(digest, Path(env["PRJXRAY_DB_DIR"]) / family)
    _TOOLCHAIN_IDS[family] = digest.hexdigest()[:16]
    return _TOOLCHAIN_IDS[family]


def chipdb_directory(shared: Path, family: str, env: dict[str, str]) -> Path:
    """The Makefile's chip databases of *family* for the toolchain on PATH: one directory per
    toolchain identity, so a rebuilt toolchain generates its own."""
    return shared / "chipdb" / f"{family}-{toolchain_identity(family, env)}"


def chipdb_ready(
    scratch: Path, chipdb: Path, dbpart: str, env: dict[str, str], log, args: list[str] = []
) -> None:
    """Have the Makefile's own rule generate the part's chip database, once at a time: two checks
    of one part must not both write `<part>.bin` while the other reads it. *args* are the demo's
    `MAKE_ARGS`, which may name the part. The cost of the generation (`/usr/bin/time -l`: wall
    time and the largest process's peak memory) is kept beside the database as `<part>.cost`."""
    import fcntl

    target = chipdb / f"{dbpart}.bin"
    with open(chipdb / f"{dbpart}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.is_file():
            log(f"upstream: generating {target}")
            timer = ["/usr/bin/time", "-l"] if Path("/usr/bin/time").is_file() else []
            run = subprocess.run(
                [*timer, "make", "-C", str(scratch), str(target), *args],
                env=env, capture_output=True, text=True,
            )
            if run.returncode:
                # make's own complaint, without the report of `time` after it
                stderr = re.split(r"\n\s*[\d.]+ real", run.stderr)[0]
                raise SetupError(f"chip database {target}: {stderr[-500:]}")
            cost = generation_cost(run.stderr)
            if cost:
                (chipdb / f"{dbpart}.cost").write_text(cost + "\n")
                log(f"upstream: generated {target}: {cost}")


def generation_cost(stderr: str) -> str:
    """Wall time and peak memory from `/usr/bin/time -l`'s report (macOS), or nothing."""
    real = re.search(r"([\d.]+) real", stderr)
    peak = re.search(r"(\d+)\s+maximum resident set size", stderr)
    if not (real and peak):
        return ""
    return f"{float(real[1]):.0f} s, peak {int(peak[1]) / 2**30:.1f} GiB (largest process)"


def prepare_upstream(demo: Path, work: Path, args: list[str]) -> tuple[Path, dict, dict]:
    """The scratch export of *demo*, the environment its Makefile runs in, and the variables the
    Makefile evaluates to (with *args* given). Every output of the committed tree is deleted:
    make would take a tracked one (`blinky.json`) for an up-to-date target."""
    scratch = export_tree(demo, work)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    variables = make_variables(scratch, env, args)
    for suffix in ("json", "fasm", "bit", "frames"):
        (scratch / f"{variables['PROJECT']}.{suffix}").unlink(missing_ok=True)
    return scratch, env, variables


def build_upstream_netlist(demo: Path, work: Path, args: list[str], log) -> dict:
    """The Makefile's synthesis alone (`make <project>.json`): yosys's netlist, no chip database."""
    scratch, env, variables = prepare_upstream(demo, work, args)
    netlist = scratch / f"{variables['PROJECT']}.json"
    cmd = ["make", "-C", str(scratch), netlist.name, *args]
    log(f"upstream: {' '.join(cmd)}")
    run = subprocess.run(cmd, env=env, capture_output=True, text=True)
    (work / "upstream.log").write_text(run.stdout + run.stderr)
    ok = run.returncode == 0 and netlist.is_file()
    return {
        "ok": ok,
        "netlist": netlist,
        "scratch": scratch,
        "variables": variables,
        "error": None if ok else f"make exited {run.returncode}; see {work / 'upstream.log'}",
    }


def build_upstream(
    demo: Path, work: Path, shared: Path, args: list[str], log, placement: bool = False
) -> dict:
    """The Makefile's build. With *placement*, nextpnr also writes its placement dump
    (`-o placement=`, which xeda always passes too): the physical LUTs are counted from it."""
    scratch, env, variables = prepare_upstream(demo, work, args)
    project, family = variables["PROJECT"], variables["FAMILY"]
    # one chip database cache for all checks, which the Makefile fills the way it always does
    chipdb = chipdb_directory(shared, family, env)
    chipdb.mkdir(parents=True, exist_ok=True)
    env[f"{family.upper()}_CHIPDB"] = str(chipdb)
    chipdb_ready(scratch, chipdb, variables["DBPART"], env, log, args)
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
    design: Path,
    mode: str,
    work: Path,
    shared: Path,
    xeda: str,
    log,
    extra: tuple[str, ...] = (),
    flow: str = "fpga_pack",
    label: str | None = None,
) -> dict:
    """`xeda run <flow> --json`, from here ("repository": the project file is found) or from an
    empty directory ("defaults"). One run root for every check, whose chip databases they share;
    the run directories of the two modes are told apart by the settings hash. *label* names the
    log when one design is built more than once (`--twice`)."""
    cwd = HERE if mode == "repository" else work / "no-project-file"
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [
        xeda,
        "run",
        flow,
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
    log_file = work / f"xeda-{label or mode}.log"
    log_file.write_text(run.stderr)
    try:
        doc = json.loads(run.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"no JSON on stdout (exit {run.returncode}); see {log_file}",
        }
    if not doc.get("success"):
        error = doc.get("error") or {}
        return {
            "ok": False,
            "error": (
                f"{error.get('type')}: {error.get('message')}"
                if error
                else "the run failed; see its results.json"
            ) + f" (see {log_file})",
            "doc": doc,
        }
    nodes = {n["flow"]: Path(n["run_path"]) for n in doc["nodes"]}
    built: dict = {"ok": True, "doc": doc, "nodes": nodes}
    if "yosys_fpga" in nodes:
        built["netlist"] = nodes["yosys_fpga"] / "netlist.json"
    if "nextpnr" in nodes:
        routed = json.loads((nodes["nextpnr"] / "results.json").read_text())
        built.update(
            fasm=Path(routed["outputs"]["config"]["path"]),
            device=routed.get("device"),
            fabric=routed.get("fabric"),
        )
    if flow == "fpga_pack":
        built["bit"] = Path(doc["results"]["outputs"]["bitstream"]["path"])
    return built


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
    return compare_bitstreams_bytes(upstream.read_bytes(), ours.read_bytes())


def compare_bitstreams_bytes(upstream: bytes, ours: bytes) -> tuple[bool, str]:
    """Same part and source tag, same configuration data; the date and time are the build's."""
    try:
        (ha, la, pa), (hb, lb, pb) = (parse_bitstream(data) for data in (upstream, ours))
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
    try:
        theirs, ours = (parse_bitstream(p.read_bytes())[0].get("b") for p in (golden, bit))
    except ValueError as error:
        return f"unreadable bitstream: {error}"
    if theirs != ours:
        # a design file that builds the demo for another part (`MAKE_ARGS`' `PART=`)
        part = (theirs or b"").rstrip(b"\0").decode(errors="replace")
        return f"no committed golden for this part ({golden.name} is for {part})"
    # a `.bit` is git-ignored here, so one that is merely there is a developer's own `make` output
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(golden.relative_to(HERE))],
        cwd=HERE, capture_output=True,
    )
    if tracked.returncode:
        return f"no committed golden ({golden.name} is in {demo.name}/ but not tracked by git)"
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


def inputs_differ_verdict(ours: dict) -> None:
    """A mode built from other generated inputs than the baseline's measures another design:
    whatever the numbers say, nothing is comparable. Kept as `inputs_differ`, apart from the
    verdict, so that `rejudge` puts it back."""
    ours["verdict"] = "not comparable"
    ours["notes"] = (
        f"{', '.join(ours['inputs_differ'])} is not what the baseline built from: different "
        f"netlists (run with --regenerate); " + ours.get("notes", "")
    )


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
            row["modes"][mode]["inputs_differ"] = differ
            inputs_differ_verdict(row["modes"][mode])
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
                if ours.get("inputs_differ"):
                    inputs_differ_verdict(ours)
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
    differ = [(r["design"], m) for r in rows for m, o in r["modes"].items() if o.get("inputs_differ")]
    if differ:
        print("built from other generated inputs than the baseline: " + ", ".join(f"{d} ({m})" for d, m in differ))
    return 1 if worse or differ else 0



# ---- the sweeps: --ci-list, --synth-only, --twice, --from-makefile-netlist ----------------------
#
# What each asks, cheapest first. `--ci-list` builds nothing: it reads upstream's workflows.
# `--synth-only` runs yosys on both sides, which is minutes for the whole repository because no
# chip database is generated. `--twice` and `--from-makefile-netlist` build bitstreams.

EXCLUSIONS = HERE / "xeda-exclusions.yaml"
WORKFLOWS = HERE / ".github" / "workflows"
WORKFLOW_FILES = ("smoke.yml", "heavy.yml")


def load_yaml_file(path: Path):
    try:
        import yaml
    except ImportError as error:
        raise SetupError(f"PyYAML is needed to read {path.name}: {error}") from error
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise SetupError(f"{path}: {error}") from error


def read_exclusions() -> dict[str, str]:
    """`xeda-exclusions.yaml`: what this fork does not build through xeda, each with the reason
    (and, if given, the evidence). An entry names an upstream-CI project (or `regression/<case>`)
    that has no design file, or a design file (`<dir>/<file>.yaml`) of a project upstream's CI
    does not build. Data, not code: an entry is a text, or a mapping with `reason` and optionally
    `evidence`."""
    if not EXCLUSIONS.is_file():
        return {}
    doc = load_yaml_file(EXCLUSIONS) or {}
    if not isinstance(doc, dict) or set(doc) - {"excluded"}:
        raise SetupError(f"{EXCLUSIONS.name}: the only key is `excluded`, a mapping of project: reason")
    excluded = doc.get("excluded") or {}
    if not isinstance(excluded, dict):
        raise SetupError(f"{EXCLUSIONS.name}: `excluded` is a mapping of project: reason")
    result = {}
    for name, entry in excluded.items():
        if isinstance(entry, dict):
            reason, evidence = entry.get("reason"), entry.get("evidence")
            extra = set(entry) - {"reason", "evidence"}
        else:
            reason, evidence, extra = entry, None, set()
        good = isinstance(reason, str) and reason.strip() and not extra
        if not good or not (evidence is None or isinstance(evidence, str)):
            raise SetupError(
                f"{EXCLUSIONS.name}: `{name}` needs a reason (a text, or `reason:` and `evidence:` texts)"
            )
        result[str(name)] = reason.strip() + (f" ({evidence.strip()})" if evidence else "")
    return result


def split_exclusions() -> tuple[dict[str, str], dict[Path, str]]:
    """The exclusions of upstream-CI projects, by project name, and those of design files, by
    path; a design file that does not exist is an error."""
    projects: dict[str, str] = {}
    designs: dict[Path, str] = {}
    for name, reason in read_exclusions().items():
        if Path(name).suffix in (".yaml", ".yml"):
            if not (HERE / name).is_file():
                raise SetupError(f"{EXCLUSIONS.name} names {name}, which is not a design file")
            designs[HERE / name] = reason
        else:
            projects[name] = reason
    return projects, designs


def excluded_design_files() -> dict[Path, str]:
    """The design files `--all` reports as excluded instead of building, with their reasons:
    each of a project upstream's CI does not build (`ci_rows` checks that)."""
    ci_rows()
    return split_exclusions()[1]


@dataclass
class CiRow:
    """One thing upstream's CI builds: a project's directory, or `regression/<case>`."""

    name: str
    where: list[str] = field(default_factory=list)  # smoke, determinism, heavy, regression
    board: str | None = None  # the `BOARD` of the job, which picosoc selects its part by
    state: str = "missing"  # `design file`, `excluded: <reason>` or `missing`
    design: Path | None = None
    problem: bool = True


def regression_cases() -> list[str]:
    """The cases `regression/run.sh` runs by default (its `cases=(...)` list)."""
    text = (HERE / "regression" / "run.sh").read_text()
    match = re.search(r"-eq 0 \] && cases=\(([^)]*)\)", text)
    if match is None:
        raise SetupError("regression/run.sh: no default `cases=(...)` list found")
    return match[1].replace("\\\n", " ").split()


def ci_rows() -> list[CiRow]:
    """Every project upstream's workflows build, in workflow order, each with where it is built
    and whether this fork has a design file for it or an exclusion."""
    rows: dict[str, CiRow] = {}
    for filename in WORKFLOW_FILES:
        path = WORKFLOWS / filename
        if not path.is_file():
            raise SetupError(f"{path} does not exist: upstream's workflow is the list of projects")
        doc = load_yaml_file(path)
        for job_id, job in ((doc or {}).get("jobs") or {}).items():
            steps = job.get("steps") or []
            board = next(
                (str(s["env"]["BOARD"]) for s in steps if "BOARD" in (s.get("env") or {})), None
            )
            matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("include") or []
            for entry in matrix:
                if "project" not in entry:
                    continue
                role = Path(filename).stem if job_id == "project" else job_id
                row = rows.setdefault(str(entry["project"]), CiRow(str(entry["project"])))
                if role not in row.where:
                    row.where.append(role)
                row.board = row.board or board
            if any("regression/run.sh" in str(s.get("run", "")) for s in steps):
                for case in regression_cases():
                    row = rows.setdefault(f"regression/{case}", CiRow(f"regression/{case}"))
                    row.where.append("regression")
    excluded, excluded_designs = split_exclusions()
    for row in rows.values():
        base = Path(row.name).name
        names = [f"{base}.yaml"] + ([f"{base}-{row.board}.yaml"] if row.board else [])
        row.design = next((HERE / row.name / n for n in names if (HERE / row.name / n).is_file()), None)
        if row.design and row.name in excluded:
            row.state = f"CONFLICT: has a design file and is excluded ({excluded[row.name]})"
        elif row.design:
            row.state, row.problem = f"design file {row.design.relative_to(HERE)}", False
        elif row.name in excluded:
            row.state, row.problem = f"excluded: {excluded[row.name]}", False
    stale = sorted(set(excluded) - set(rows))
    if stale:
        raise SetupError(
            f"{EXCLUSIONS.name} names {', '.join(stale)}, which upstream's workflows do not build"
        )
    # a project upstream's CI builds is excluded by its name, and then has no design file
    built = sorted(str(p.relative_to(HERE)) for p in excluded_designs if p in {r.design for r in rows.values()})
    if built:
        raise SetupError(
            f"{EXCLUSIONS.name} names {', '.join(built)}, the design file of a project upstream's "
            "CI builds: exclude that project by its name, with no design file"
        )
    return list(rows.values())


def run_ci_list() -> int:
    rows = ci_rows()

    def label(row: CiRow) -> str:
        # a job's `BOARD` matters only to the project whose design file is named after it (picosoc)
        board = row.board and row.design and row.design.stem == f"{Path(row.name).name}-{row.board}"
        return ", ".join(row.where) + (f" (BOARD={row.board})" if board else "")

    width = max(len(r.name) for r in rows)
    wide = max(len(label(r)) for r in rows)
    print(f"{'upstream CI project':<{width}}  {'built by':<{wide}}  state")
    for row in rows:
        print(f"{row.name:<{width}}  {label(row):<{wide}}  {row.state}")
    missing = [r for r in rows if r.problem]
    covered = {r.design for r in rows if r.design}
    others = [p for p in designs_for([], True) if p not in covered]
    excluded = excluded_design_files()
    print(
        f"\n{len(rows)} projects: {sum(r.design is not None for r in rows)} with a design file, "
        f"{sum(r.state.startswith('excluded') for r in rows)} excluded, "
        f"{len(missing)} missing or inconsistent"
    )
    if others:
        print("design files for projects upstream's CI does not build: "
              + ", ".join(str(p.relative_to(HERE)) for p in others))
    for path, reason in excluded.items():
        print(f"excluded design file (not built by --all): {path.relative_to(HERE)}: {reason}")
    return 1 if missing else 0


# ---- what a design file must agree with the Makefile on, whatever is built -----------------


def disagreements(variables: dict[str, str], demo: Path, settings: dict) -> list[str]:
    """Where the design file, as xeda resolved it (`settings.json` of a flow's run), is not what
    the Makefile builds: the part, the top module, every HDL source in the Makefile's order and its
    constraints file. A dropped source or a wrong part is a different design, whatever the build
    of it says. The design may have more sources (a `Data` file yosys reads by itself)."""
    problems = []
    part = ((settings.get("effective_flow_settings") or {}).get("fpga") or {}).get("part")
    if part != variables["PART"]:
        problems.append(f"part: the Makefile builds {variables['PART']}, the design file {part}")
    rtl = (settings.get("design") or {}).get("rtl") or {}
    if rtl.get("top") != variables.get("TOP_MODULE"):
        problems.append(f"top: the Makefile's is {variables.get('TOP_MODULE')}, the design file's {rtl.get('top')}")
    ours = [
        os.path.realpath(s if isinstance(s, str) else s.get("path") or s.get("file") or "")
        for s in rtl.get("sources", [])
    ]
    wanted = [variables["TOP_VERILOG"], *variables.get("ADDITIONAL_SOURCES", "").split()]
    position = -1
    for source in wanted:
        path = os.path.realpath(demo / source)
        if path not in ours[position + 1 :]:
            problems.append(
                f"source {source}: the Makefile reads it"
                + (" (in this order)" if path in ours else "")
                + ", the design file does not"
            )
        else:
            position = ours.index(path, position + 1)
    constraints = variables.get("XDC")
    if constraints and os.path.realpath(demo / constraints) not in ours:
        problems.append(f"constraints {constraints}: the Makefile builds with it, the design file lacks it")
    return problems


# ---- the table of a sweep --------------------------------------------------------------------


@dataclass
class Row:
    """One design's result in a sweep: the verdict, the cells of its table line, the details."""

    design: str
    passed: bool = False
    cells: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)


def run_sweep(options, designs: list[Path], check, columns: list[str]) -> int:
    """*check* of each design (built *jobs* at once), the details of every design and a table with
    the *columns* of each row's cells. Status 0 when every row passed."""
    from concurrent.futures import ThreadPoolExecutor

    work = options.work.resolve()
    work.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        print(f"  .. {message}", file=sys.stderr, flush=True)

    def one(design: Path) -> Row:
        name = str(design.relative_to(HERE))
        try:
            return check(design, work, options.xeda, options.mode, log)
        except Exception as error:  # one design's trouble must not lose the others' rows
            return Row(name, False, {"verdict": "FAILED"}, [f"{type(error).__name__}: {error}"])

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        rows = list(pool.map(one, designs))
    for row in rows:
        print(f"\n{'PASS' if row.passed else 'FAIL'}  {row.design}")
        print("\n".join(f"  {line}" for line in row.lines))
    table = [{"design": r.design, **r.cells} for r in rows]
    names = ["design", *columns]
    widths = {c: max(len(c), *(len(t.get(c, "")) for t in table)) for c in names}
    print("\n" + "  ".join(f"{c:<{widths[c]}}" for c in names))
    for line in table:
        print("  ".join(f"{line.get(c, ''):<{widths[c]}}" for c in names))
    print(f"\n{sum(r.passed for r in rows)} of {len(rows)} passed")
    return 0 if all(r.passed for r in rows) else 1


# ---- --synth-only ---------------------------------------------------------------------------


def compare_cells(upstream: dict[str, int], ours: dict[str, int]) -> tuple[bool, str]:
    if upstream == ours:
        return True, f"identical ({sum(upstream.values())} cells of {len(upstream)} types)"
    changed = sorted(k for k in upstream.keys() | ours.keys() if upstream.get(k, 0) != ours.get(k, 0))
    detail = ", ".join(f"{k} {upstream.get(k, 0)} -> {ours.get(k, 0)}" for k in changed[:8])
    more = f", and {len(changed) - 8} more" if len(changed) > 8 else ""
    return False, (
        f"{sum(upstream.values())} cells upstream, {sum(ours.values())} through xeda; "
        f"{len(changed)} types differ: {detail}{more}"
    )


def synth_only(design: Path, work: Path, xeda: str, mode: str, log) -> Row:
    """yosys on both sides, no chip database: the Makefile's own `make <project>.json` in a
    scratch export, and `xeda run yosys_fpga`; the netlists' cell-type counts are compared, and the
    design file's part, top and sources are checked against the Makefile's."""
    demo = design.parent
    row = Row(str(design.relative_to(HERE)), cells={"verdict": "FAILED"})
    shared = work
    work = work / design.stem
    work.mkdir(parents=True, exist_ok=True)
    upstream = build_upstream_netlist(demo, work, MAKE_ARGS.get(row.design, []), log)
    if not upstream["ok"]:
        row.lines.append(f"upstream: BUILD FAILED: {upstream['error']}")
        return row
    variables = upstream["variables"]
    row.cells["part"] = variables["PART"]
    ours = build_xeda(design, mode, work, shared, xeda, log, flow="yosys_fpga", label=f"{mode}-synth")
    if not ours["ok"]:
        row.lines.append(f"xeda {mode}: BUILD FAILED: {ours['error']}")
        return row
    settings = json.loads((ours["nodes"]["yosys_fpga"] / "settings.json").read_text())
    problems = disagreements(variables, demo, settings)
    a, b = netlist_cells(upstream["netlist"]), netlist_cells(ours["netlist"])
    same, text = compare_cells(a, b)
    row.cells.update(makefile=str(sum(a.values())), xeda=str(sum(b.values())))
    row.lines.append(f"cells: {text}")
    row.lines += [f"design file: {p}" for p in problems]
    # xeda generates a generated input again on every run (`--clean`): one made under another
    # `SOURCE_DATE_EPOCH` than the Makefile's copy leaves the two with different netlists
    differ = generated_differ(demo, upstream["scratch"])
    if differ:
        row.lines.append(
            f"generated inputs: {', '.join(differ)} is not what the Makefile built from; the two "
            "built different netlists (run with --regenerate)"
        )
    row.passed = same and not problems and not differ
    row.cells["verdict"] = (
        "identical" if row.passed else ("DIFFERENT" if not same else
                                        "DESIGN FILE" if problems else "INPUTS")
    )
    return row


# ---- --twice ----------------------------------------------------------------------------------


def twice(design: Path, work: Path, xeda: str, mode: str, log) -> Row:
    """Two clean xeda builds of the design, bitstreams equal but for the header's date and time
    (upstream's determinism job, which does the same with the Makefile). A design that disagrees
    with the Makefile (`disagreements`) is refused first: determinism of the wrong design is
    nothing to check, and a wrong part would cost a chip database."""
    demo = design.parent
    row = Row(str(design.relative_to(HERE)), cells={"verdict": "FAILED"})
    shared = work
    work = work / design.stem
    work.mkdir(parents=True, exist_ok=True)
    _, _, variables = prepare_upstream(demo, work, MAKE_ARGS.get(row.design, []))
    row.cells["part"] = variables["PART"]
    pre = build_xeda(design, mode, work, shared, xeda, log, flow="yosys_fpga", label=f"{mode}-synth")
    if not pre["ok"]:
        row.lines.append(f"xeda {mode}: BUILD FAILED: {pre['error']}")
        return row
    settings = json.loads((pre["nodes"]["yosys_fpga"] / "settings.json").read_text())
    problems = disagreements(variables, demo, settings)
    if problems:
        row.lines += [f"design file: {p}" for p in problems]
        row.cells["verdict"] = "DESIGN FILE"
        return row
    copies = []
    for n in (1, 2):
        built = build_xeda(design, mode, work, shared, xeda, log, label=f"{mode}-{n}")
        if not built["ok"]:
            row.lines.append(f"build {n}: FAILED: {built['error']}")
            return row
        # the second build is clean: it empties the directory the first wrote its files in
        kept = work / f"build{n}"
        kept.mkdir(exist_ok=True)
        copies.append((kept / "design.fasm", kept / "design.bit"))
        shutil.copyfile(built["fasm"], copies[-1][0])
        shutil.copyfile(built["bit"], copies[-1][1])
    (fasm1, bit1), (fasm2, bit2) = copies
    same_fasm = fasm_features(fasm1) == fasm_features(fasm2)
    same_bit, bit_text = compare_bitstreams(bit1, bit2)
    row.lines.append(
        f"fasm: {len(fasm_features(fasm1))} features, "
        + ("identical" if same_fasm else f"DIFFERENT ({len(fasm_features(fasm2))} in the second build)")
    )
    row.lines.append(f"bit:  {bit_text}")
    row.passed = same_fasm and same_bit
    row.cells["verdict"] = "deterministic" if row.passed else "NONDETERMINISTIC"
    return row


# ---- --from-makefile-netlist ------------------------------------------------------------------


def expand_dotted(section: dict) -> dict:
    """A flow section with its dotted keys (`fpga.part: ...`, which xeda accepts) as nested
    mappings, so that `fpga` is found however the design file spells it."""
    expanded: dict = {}
    for key, value in section.items():
        *parents, leaf = str(key).split(".")
        target = expanded
        for parent in parents:
            target = target.setdefault(parent, {})
        target[leaf] = expand_dotted(value) if isinstance(value, dict) else value
    return expanded


def derive_netlist_design(design: Path, netlist: Path, out: Path) -> Path:
    """A design file that is *design*'s with the Makefile's own netlist as its only HDL: a typed
    `JsonNetlist` source and the design's constraints, and `fpga` given to `nextpnr` and
    `fpga_pack` (with `yosys_fpga` displaced there is no node to carry it). The design's other
    nextpnr settings (`timing_allow_fail`, `extra_args`) stay."""
    import yaml

    doc = load_yaml_file(design) or {}
    flows = {name: expand_dotted(section or {}) for name, section in (doc.get("flows") or {}).items()}
    fpga = (flows.pop("yosys_fpga", None) or {}).get("fpga")
    if fpga is None:
        raise SetupError(f"{design}: `flows.yosys_fpga.fpga` names no part")
    constraints = []
    for entry in (doc.get("rtl") or {}).get("sources") or []:
        path = entry if isinstance(entry, str) else entry.get("file") or entry.get("path")
        if str(path).endswith(".xdc"):
            constraints.append(str((design.parent / path).resolve()))
    # a copy for each: one object in two places would be written as a YAML anchor and alias
    flows["nextpnr"] = {"fpga": copy.deepcopy(fpga), **(flows.get("nextpnr") or {})}
    flows["fpga_pack"] = {"fpga": copy.deepcopy(fpga), **(flows.get("fpga_pack") or {})}
    derived = {
        "name": f"{doc['name']}-from-makefile-netlist",
        "rtl": {
            "top": (doc.get("rtl") or {}).get("top"),
            "sources": [{"file": str(netlist), "type": "JsonNetlist"}, *constraints],
        },
        "flows": flows,
    }
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{design.stem}-from-makefile-netlist.yaml"
    path.write_text(
        f"# {design.name} with the Makefile's own netlist for its HDL (xeda_check.py --from-makefile-netlist)\n"
        + yaml.safe_dump(derived, sort_keys=False)
    )
    return path


def from_makefile_netlist(design: Path, work: Path, xeda: str, mode: str, log) -> Row:
    """xeda's `nextpnr` and `fpga_pack` on the Makefile's own netlist, compared with the Makefile's
    FASM and bitstream. It leaves synthesis out: a design that passes this and fails the normal
    check differs in synthesis alone."""
    demo = design.parent
    row = Row(str(design.relative_to(HERE)), cells={"verdict": "FAILED"})
    shared = work
    work = work / design.stem
    work.mkdir(parents=True, exist_ok=True)
    upstream = build_upstream(demo, work, shared, MAKE_ARGS.get(row.design, []), log)
    if not upstream["ok"]:
        row.lines.append(f"upstream: BUILD FAILED: {upstream['error']}")
        return row
    row.cells["part"] = upstream["variables"]["PART"]
    derived = derive_netlist_design(design, upstream["netlist"], work / "netlist")
    ours = build_xeda(derived, "repository", work, shared, xeda, log, label="netlist")
    if not ours["ok"]:
        row.lines.append(f"xeda from the Makefile's netlist: BUILD FAILED: {ours['error']}")
        return row
    same_fasm, fasm_text = compare_fasm(upstream["fasm"], ours["fasm"])
    same_bit, bit_text = compare_bitstreams(upstream["bit"], ours["bit"])
    row.lines += [f"fasm: {fasm_text}", f"bit:  {bit_text}"]
    row.passed = same_fasm and same_bit
    row.cells["verdict"] = "identical" if row.passed else "DIFFERENT"
    return row



# ---- --regression -----------------------------------------------------------------------------
#
# `regression/run.sh` builds each case with its own recipe (no Makefile) and judges it by the case's
# own files: a non-empty FASM, then `expect.txt`'s patterns, then `check.sh`; a case with an
# `expect_fail` marker must make nextpnr fail and its `check.sh` read `nextpnr.log`. The checker
# runs `run.sh` itself in a scratch export (the upstream verdict) and judges xeda's build of the
# case's design file by the same files (xeda's verdict); the two verdicts must be equal. A
# placement-only case (`no_route`) has no FASM and no xeda verdict: it is excluded
# (`xeda-exclusions.yaml`).

REGRESSION = HERE / "regression"

FAMILIES = {"xc7a": "artix7", "xc7k": "kintex7", "xc7s": "spartan7", "xc7z": "zynq7"}


def regression_verdict_kind(case: Path) -> str:
    """What the case's own markers say it must do: `placed` (`no_route`), `expect-fail`
    (`expect_fail`) or `pass`."""
    if (case / "no_route").exists():
        return "placed"
    return "expect-fail" if (case / "expect_fail").exists() else "pass"


def regression_chipdb(part: str, work: Path, shared: Path, env: dict[str, str], log) -> Path:
    """The Makefile side's chip database for *part* (the Makefile rule of `openXC7.mk`, through a
    one-line demo Makefile in the scratch), shared with the demos of the same part."""
    family = FAMILIES.get(part[:4])
    if family is None:
        raise SetupError(f"no family for part {part}")
    dbpart = re.sub(r"-[0-9]", "", part)
    scratch = work / "chipdb-make" / dbpart
    scratch.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "openXC7.mk", scratch.parent / "openXC7.mk")
    (scratch / "Makefile").write_text(
        f"FAMILY = {family}\nPART = {part}\nPROJECT = chipdb\n"
        f"CHIPDB = ${{{family.upper()}_CHIPDB}}\n\ninclude ../openXC7.mk\n"
    )
    chipdb = chipdb_directory(shared, family, env)
    chipdb.mkdir(parents=True, exist_ok=True)
    env = dict(env, **{f"{family.upper()}_CHIPDB": str(chipdb)})
    chipdb_ready(scratch, chipdb, dbpart, env, log)
    return chipdb / f"{dbpart}.bin"


def regression_case_part(case: Path) -> str:
    """The part a case is built for: its `part.txt`, which may lack the speed grade (`run.sh`
    needs only the chip database); the design file names the full part."""
    return (case / "part.txt").read_text().strip()


def run_upstream_regression(case: str, work: Path, chipdb: Path, env: dict[str, str]) -> dict:
    """`regression/run.sh <case>` in a scratch export of `regression/`, with a `CHIPDB_DIR` that
    holds just the case's chip database under the names `run.sh` looks for."""
    target = work / "upstream"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "HEAD", "regression"], cwd=HERE, check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    links = work / "chipdb-dir"
    if links.exists():
        shutil.rmtree(links)
    links.mkdir()
    part = regression_case_part(target / "regression" / case)
    for name in {f"{part}.bin", f"{part.rsplit('-', 1)[0]}.bin", chipdb.name}:
        (links / name).symlink_to(chipdb)
    env = dict(env, CHIPDB_DIR=str(links))
    env.pop("CHIPDB", None)
    run = subprocess.run(
        ["bash", str(target / "regression" / "run.sh"), case],
        env=env, capture_output=True, text=True,
    )
    (work / "upstream.log").write_text(run.stdout + run.stderr)
    line = next((l.strip() for l in run.stdout.splitlines() if l.strip().startswith(case)), "")
    words = line.split()
    outcome = words[1] if len(words) > 1 else "NOTHING"
    return {
        "ok": outcome == "ok",
        "outcome": line[len(case):].strip() or f"no verdict line (exit {run.returncode})",
        "returncode": run.returncode,
        "case_dir": target / "regression" / case,
    }


def judge_regression(
    case_dir: Path, kind: str, built: dict, check_dir: Path, chipdb: Path
) -> tuple[bool, str]:
    """xeda's build of a case, judged as `run.sh` judges its own: for `pass`, a successful build
    with a non-empty FASM, `expect.txt`'s patterns in it and `check.sh` passing; for
    `expect-fail`, nextpnr failing and `check.sh` passing on its log. `check.sh` runs in a copy of
    the case directory (it reads files beside itself) holding xeda's netlist (`top.json`), its
    routed design (`top_routed.json`), FASM (`top.fasm`) and `nextpnr.log`; `CHIPDB` is the
    Makefile side's database of the part, for a check that runs nextpnr again."""
    nodes = {n["flow"]: n for n in (built.get("doc") or {}).get("nodes", [])}
    pnr = nodes.get("nextpnr")
    if kind == "expect-fail":
        if built["ok"]:
            return False, "xeda's nextpnr succeeded where the case expects it to fail"
        if pnr is None or pnr.get("state") != "failed":
            return False, f"the build failed before nextpnr: {built['error']}"
    elif not built["ok"]:
        return False, f"BUILD FAILED: {built['error']}"
    if check_dir.exists():
        shutil.rmtree(check_dir)
    shutil.copytree(case_dir, check_dir, symlinks=True)
    run_path = Path(pnr["run_path"]) if pnr else None
    fasm = check_dir / "top.fasm"
    if built.get("fasm"):
        shutil.copyfile(built["fasm"], fasm)
    if run_path and (run_path / "nextpnr.log").is_file():
        shutil.copyfile(run_path / "nextpnr.log", check_dir / "nextpnr.log")
    if built.get("netlist") and built["netlist"].is_file():
        shutil.copyfile(built["netlist"], check_dir / "top.json")
    if run_path and (run_path / "results.json").is_file():
        routed = json.loads((run_path / "results.json").read_text()).get("artifacts", {}).get("write")
        if routed:
            shutil.copyfile(run_path / routed, check_dir / "top_routed.json")
    notes = []
    if kind == "pass":
        if not fasm.is_file() or fasm.stat().st_size == 0:
            return False, "empty or missing FASM"
        notes.append(f"{len(fasm_features(fasm))} FASM features")
        expect = case_dir / "expect.txt"
        if expect.is_file():
            text = fasm.read_text()
            missing = [p for p in expect.read_text().splitlines() if p and not re.search(p, text, re.M)]
            if missing:
                return False, f"FASM lacks {', '.join(missing)}"
            notes.append("expect.txt matched")
    check = check_dir / "check.sh"
    if os.access(check, os.X_OK):
        env = dict(os.environ, FASM=str(fasm), CASE_DIR=str(check_dir), CHIPDB=str(chipdb))
        env.pop("CHIPDB_DIR", None)
        run = subprocess.run(["bash", str(check)], env=env, capture_output=True, text=True,
                             cwd=check_dir)
        (check_dir / "check.log").write_text(run.stdout + run.stderr)
        if run.returncode:
            return False, f"check.sh failed: {(run.stdout + run.stderr).strip().splitlines()[-1:]}"
        notes.append("check.sh passed")
    elif kind == "expect-fail":
        return False, "an expected-fail case without check.sh"
    return True, "ok (" + ", ".join(notes or ["nextpnr failed as expected"]) + ")"


def regression(design: Path, work: Path, xeda: str, mode: str, log) -> Row:
    """One regression case: `run.sh`'s verdict and xeda's, in both modes; repository mode must
    give `run.sh`'s, defaults mode is reported."""
    case_dir = design.parent
    case = case_dir.name
    row = Row(str(design.relative_to(HERE)), cells={"verdict": "FAILED"})
    shared = work
    work = work / f"regression-{case}"
    work.mkdir(parents=True, exist_ok=True)
    kind = regression_verdict_kind(case_dir)
    if kind == "placed":
        row.lines.append("placement-only case (no_route): no FASM, nothing for xeda to judge")
        row.cells["verdict"] = "NOT JUDGED"
        return row
    part = regression_case_part(case_dir)
    row.cells["part"] = part
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    chipdb = regression_chipdb(part, work, shared, env, log)
    upstream = run_upstream_regression(case, work, chipdb, env)
    row.lines.append(f"run.sh: {upstream['outcome']}")
    row.cells["run.sh"] = "ok" if upstream["ok"] else "FAIL"
    passed = True
    for mode_ in ("repository", "defaults"):
        built = build_xeda(design, mode_, work, shared, xeda, log, flow="nextpnr", label=mode_)
        ok, text = judge_regression(case_dir, kind, built, work / f"check-{mode_}", chipdb)
        same = ok == upstream["ok"]
        row.cells[mode_] = "ok" if ok else "FAIL"
        required = mode_ == "repository"
        flag = "same as run.sh" if same else ("DIFFERS from run.sh" + ("" if required else " [informational]"))
        row.lines.append(f"xeda {mode_}: {text}; {flag}")
        theirs = upstream["case_dir"] / "top.fasm"
        if built.get("fasm") and theirs.is_file() and theirs.stat().st_size:
            # for information: run.sh's recipe is not the Makefile's, and is no oracle of features
            row.lines.append(f"  fasm vs run.sh's: {compare_fasm(theirs, built['fasm'])[1]}")
        if required and not same:
            passed = False
    row.passed = passed
    row.cells["verdict"] = "same" if passed else "DIFFERENT"
    return row


def regression_designs(names: list[str]) -> list[Path]:
    """The design files of the named cases, or of `run.sh`'s default cases but those excluded
    (`xeda-exclusions.yaml`, which `--ci-list` checks); a case with neither is an error."""
    excluded = {} if names else read_exclusions()
    found = []
    for case in names or regression_cases():
        design = REGRESSION / case / f"{case}.yaml"
        if design.is_file():
            found.append(design)
        elif f"regression/{case}" in excluded:
            print(f"regression/{case}: excluded: {excluded[f'regression/{case}']}")
        else:
            raise SetupError(f"regression/{case} has no design file {design.name}")
    return found


def designs_for(names: list[str], everything: bool) -> list[Path]:
    """The design files named (a demo directory or a design file; an excluded one too), or with
    *everything*, every demo's design file but those excluded (`excluded_design_files`)."""
    if everything:
        excluded = excluded_design_files()
        return sorted(
            p
            for p in HERE.glob("*/*.yaml")
            if (p.stem == p.parent.name or p.stem.startswith(p.parent.name + "-"))
            and p not in excluded
        )
    found = []
    for name in names:
        path = Path(name)
        if path.suffix in (".yaml", ".yml"):
            found.append(path.resolve())
            continue
        # a directory is its own design file, `<dir>/<dir>.yaml`, else the one it holds
        own = HERE / name / f"{Path(name).name}.yaml"
        candidates = [own] if own.is_file() else sorted((HERE / name).glob("*.yaml"))
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
    parser.add_argument("--jobs", type=int, default=1,
                        help="--qor, --synth-only, --twice, --from-makefile-netlist: designs built at once")
    parser.add_argument("--out", default="qor", help="--qor: write <out>.json and <out>.csv")
    parser.add_argument("--ci-list", action="store_true",
                        help="list the projects upstream's CI builds (.github/workflows) and whether "
                        "each has a design file or an exclusion (xeda-exclusions.yaml); exit 1 on a "
                        "missing one. Builds nothing")
    parser.add_argument("--synth-only", action="store_true",
                        help="yosys on both sides, no chip database: the netlists' cell-type counts "
                        "are compared (xeda run yosys_fpga against `make <project>.json`)")
    parser.add_argument("--twice", action="store_true",
                        help="two clean xeda builds, normalized bitstreams equal; without a design, "
                        "upstream's determinism matrix")
    parser.add_argument("--from-makefile-netlist", action="store_true",
                        help="xeda's nextpnr and fpga_pack on the Makefile's own netlist, against the "
                        "Makefile's FASM and bitstream (leaves synthesis out)")
    parser.add_argument("--regression", action="store_true",
                        help="the regression cases (`regression/run.sh`'s default list, or the named "
                        "cases): run.sh's verdict and xeda's must be the same")
    parser.add_argument("--mode", choices=("repository", "defaults"), default="repository",
                        help="--synth-only, --twice: where xeda is started: here (xedaproject.yaml "
                        "applies) or from a directory with no project file")
    options = parser.parse_args()
    modes = [f for f in ("qor", "ci_list", "synth_only", "twice", "from_makefile_netlist", "regression")
             if getattr(options, f)]
    if len(modes) > 1:
        parser.error("choose one of --qor, --ci-list, --synth-only, --twice, --from-makefile-netlist, "
                     "--regression")
    global REGENERATE, LITEX_PYTHON
    REGENERATE, LITEX_PYTHON = options.regenerate, options.litex_python
    # every process below inherits it: see GENERATED
    os.environ.setdefault("SOURCE_DATE_EPOCH", GENERATOR_EPOCH)
    if options.rejudge:
        rows = rejudge(json.loads(Path(options.rejudge).read_text()), options.tolerance)
        Path(options.out).with_suffix(".json").write_text(json.dumps(rows, indent=1, default=str))
        print_qor_summary(rows)
        return 0
    if options.ci_list:
        if options.designs or options.all:
            parser.error("--ci-list takes no design")
        try:
            return run_ci_list()
        except SetupError as error:
            print(f"setup: {error}", file=sys.stderr)
            return 2
    if not options.designs and not options.all and not options.twice and not options.regression:
        parser.error("name a demo, or give --all")
    try:
        tools = check_toolchain(options.xeda)
        if options.twice and not options.designs and not options.all:
            # upstream's determinism job: the projects its determinism matrix rebuilds
            designs = []
            for row in (r for r in ci_rows() if "determinism" in r.where):
                if row.design is None:
                    raise SetupError(f"{row.name} is in upstream's determinism matrix: {row.state}")
                designs.append(row.design)
        elif options.regression:
            if options.all:
                parser.error("--regression takes case names, not --all")
            designs = regression_designs(options.designs)
        else:
            designs = designs_for(options.designs, options.all)
        excluded = excluded_design_files() if options.all else {}
    except SetupError as error:
        print(f"setup: {error}", file=sys.stderr)
        return 2
    print("tools: " + ", ".join(f"{name}={path}" for name, path in tools.items()))
    for path, reason in excluded.items():
        print(f"EXCLUDED  {path.relative_to(HERE)}: {reason}")
    options.work.mkdir(parents=True, exist_ok=True)
    status = run_mode(options, designs)
    if excluded:
        print(f"{len(excluded)} excluded, not built (xeda-exclusions.yaml): "
              + ", ".join(str(p.relative_to(HERE)) for p in excluded))
    return status


def run_mode(options, designs: list[Path]) -> int:
    """Build and check *designs* in the mode *options* select; the exit status."""
    if options.qor:
        return run_qor(options, designs)
    if options.synth_only:
        return run_sweep(options, designs, synth_only, ["part", "makefile", "xeda", "verdict"])
    if options.twice:
        return run_sweep(options, designs, twice, ["part", "verdict"])
    if options.from_makefile_netlist:
        return run_sweep(options, designs, from_makefile_netlist, ["part", "verdict"])
    if options.regression:
        return run_sweep(options, designs, regression, ["part", "run.sh", "repository", "defaults", "verdict"])

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
