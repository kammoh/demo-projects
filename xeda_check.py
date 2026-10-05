#!/usr/bin/env python3
"""Build a demo with the upstream Makefile and with xeda, and say whether the FASM matches.

    source /opt/openxc7/export.sh
    python xeda_check.py blinky-digilent-arty            # a demo with one design file
    python xeda_check.py picosoc/picosoc-kx2.yaml        # or a design file
    python xeda_check.py --all                           # every demo that has a design file

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
MAKE_ARGS: dict[str, list[str]] = {}

# directories next to a demo that its Makefile may name (`../vexriscv/VexRiscv.v`)
LIBRARY_DIRECTORIES = ("vexriscv", "vexriscv_smp", "serv")

TOOLS = ("yosys", "nextpnr-himbaechel", "nextpnr-xilinx", "fpga-as")


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
    return target / demo.name


def make_variables(directory: Path, env: dict[str, str], args: list[str]) -> dict[str, str]:
    """`PROJECT`, `PART`, `FAMILY` as make evaluates them (never by reading the Makefile)."""
    out = subprocess.run(
        ["make", "-C", str(directory), "-pn", "--no-print-directory", "all", *args],
        env=env,
        capture_output=True,
        text=True,
    ).stdout
    wanted = ("PROJECT", "PART", "FAMILY", "DBPART")
    values = {}
    for name in wanted:
        match = re.search(rf"^{name} :?= (.*)$", out, re.MULTILINE)
        if match:
            values[name] = match.group(1).strip()
    missing = [name for name in wanted if name not in values]
    if missing:
        raise SetupError(f"make reports no {', '.join(missing)} in {directory}")
    return values


def build_upstream(demo: Path, work: Path, args: list[str], log) -> dict:
    scratch = export_tree(demo, work)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    variables = make_variables(scratch, env, args)
    project, family = variables["PROJECT"], variables["FAMILY"]
    # every output of the committed tree goes: make would take a tracked one for up to date
    for suffix in ("json", "fasm", "bit", "frames"):
        (scratch / f"{project}.{suffix}").unlink(missing_ok=True)
    # one chip database cache for all checks, which the Makefile fills the way it always does
    chipdb = work / "chipdb" / family
    chipdb.mkdir(parents=True, exist_ok=True)
    env[f"{family.upper()}_CHIPDB"] = str(chipdb)
    cmd = ["make", "-C", str(scratch), *args]
    log(f"upstream: {' '.join(cmd)}  (chipdb {chipdb})")
    run = subprocess.run(cmd, env=env, capture_output=True, text=True)
    (work / "upstream.log").write_text(run.stdout + run.stderr)
    fasm, bit = scratch / f"{project}.fasm", scratch / f"{project}.bit"
    ok = run.returncode == 0 and fasm.is_file() and bit.is_file()
    return {
        "ok": ok,
        "fasm": fasm,
        "bit": bit,
        "variables": variables,
        "log": work / "upstream.log",
        "error": None if ok else f"make exited {run.returncode}; see {work / 'upstream.log'}",
    }


# ---- the xeda build -----------------------------------------------------------------------


def build_xeda(design: Path, mode: str, work: Path, xeda: str, log) -> dict:
    """`xeda run fpga_pack --json`, from here ("repository": the project file is found) or
    from an empty directory ("defaults"). One run root for both, whose chip databases they
    share; their run directories are told apart by the settings hash."""
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
        str(work / "xeda_run"),
    ]
    log(f"xeda ({mode}): {' '.join(cmd)}  (from {cwd})")
    run = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
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
        "device": routed.get("device"),
        "fabric": routed.get("fabric"),
    }


# ---- the comparison -----------------------------------------------------------------------


def fasm_features(path: Path) -> list[str]:
    """Every FASM line that is not a comment or blank, in order."""
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def compare_fasm(upstream: Path, ours: Path) -> tuple[bool, str]:
    a, b = fasm_features(upstream), fasm_features(ours)
    if a == b:
        return True, f"identical ({len(a)} features, same order)"
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
    work = work / design.stem
    work.mkdir(parents=True, exist_ok=True)

    upstream = build_upstream(demo, work, args, log)
    if not upstream["ok"]:
        verdict.lines.append(f"upstream: BUILD FAILED: {upstream['error']}")
        return verdict
    project = upstream["variables"]["PROJECT"]
    verdict.lines.append(f"upstream: built {project}.bit for {upstream['variables']['PART']}")
    verdict.lines.append("          golden: " + golden_verdict(demo, project, upstream["bit"]))

    passed = True
    for mode in ("repository", "defaults"):
        ours = build_xeda(design, mode, work, xeda, log)
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
    verdict.passed = passed
    return verdict


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
    options = parser.parse_args()
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
