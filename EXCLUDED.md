# What this fork does not build through xeda, and why

`xeda-exclusions.yaml` lists what this fork does not build through xeda, each entry with its
reason and evidence. This page gives the evidence in full. An entry is one of two kinds:

- **A project that upstream's CI builds**, by the name its workflows give it. It has no design
  file. Every other CI project has one: `python xeda_check.py --ci-list` checks that, and fails
  on a CI project with neither a design file nor an entry.
- **A design file of a project that upstream's CI does not build**, by its path. It builds on
  neither route, not with the Makefile and not with xeda. `python xeda_check.py --all` reports
  it as excluded, with its reason, and does not build it. Naming the design file builds it all
  the same, to see whether the exclusion is still needed.

This page also lists the regression cases that upstream runs only by name, and the one known
red row of `--all`: an upstream-CI project that does not build in xeda's defaults mode.

Measured on 2026-10-06 with openXC7 at `/opt/openxc7` (yosys 0.69, nextpnr-himbaechel
1.0.0-41-g3e5c2cdd, fpga-as) and xeda `1456fd32c4`.

## Excluded upstream-CI projects

### `regression/bufr-pad-site` and `regression/bufr-sink-region`

Both cases are placement-only. Each has a `no_route` marker, so `regression/run.sh` runs
nextpnr with `--no-route` and judges the placed design (`top_routed.json`) with the case's
`check.sh`. There is no FASM.

xeda's `nextpnr` flow always writes the FASM configuration for a Xilinx part, and a run that
leaves none is a failed run. A placement-only run of these cases through xeda therefore fails,
and there is no xeda build to judge.

Evidence. `regression/run.sh` passes both cases on the placed design:

```
  bufr-pad-site              ok  (128K placed)
  bufr-sink-region           ok  (128K placed)
```

The same case through xeda (`xeda run nextpnr` on a design file with `extra_args: [--no-route]`
and `write: top_routed.json`, from the repository root): nextpnr ends with `Program finished
normally.` and writes the placed design, and xeda fails the run:

```
FlowFatalError: Flow nextpnr failed: FlowFatalException nextpnr did not write enabled fasm
configuration .../reg-bufr-pad-site/nextpnr_39fca93d3aa1dd75/config.fasm.
```

(`bufr-sink-region` the same.) The placement itself is right: each case's `check.sh` passes on
the placed design and the log that this failed xeda run left (`ok: BUFR X263Y130/BUFR_X0Y2.BUFR
is the dedicated site of the pad ...`, `ok: 28 flops inside clock region x141..263 y105..157`).
The checker does not judge them that way, because it would read the outputs of a run that xeda
reports as failed.

## Excluded design files outside upstream CI

Upstream's CI builds neither of these designs. With the toolchain above, the Makefile fails, and
xeda fails the same way, at the same place.

### `blinky-ypcb003381p1/blinky-ypcb003381p1.yaml`

Line 3 of upstream's `ypcb003381p1.xdc` ends with a comment after a semicolon:

```
create_clock -period 20.000 [get_ports clk] ;# 50 MHz
```

The XDC parser of nextpnr-himbaechel 1.0.0-41 does not accept it. `make` stops in nextpnr:

```
ERROR: failed to parse target ';' (on line 3)
make: *** [../openXC7.mk:69: top.fasm] Error 125
```

xeda's nextpnr, given the same chip database, stops at the same line, and xeda names the file:

```
FlowFatalError: Flow nextpnr failed: FlowFatalException nextpnr constraint error: ERROR: failed to
parse target ';' (on line 3 (.../blinky-ypcb003381p1/ypcb003381p1.xdc:3))
```

### `picosoc/picosoc-genesys2.yaml`

`genesys2.v` drives `led[1:0]`, and upstream's `picosoc-genesys2.xdc` places only `led[0]` (it
sets an I/O standard for `led[1]`, but no pin). `make BOARD=genesys2` stops in nextpnr:

```
ERROR: FIXME: unconstrained IO not supported (pad led[1])
make: *** [../openXC7.mk:69: picosoc.fasm] Error 125
```

xeda's nextpnr, given the same chip database, stops with the same error in its `nextpnr.log`.
Upstream's CI builds picosoc for `BOARD=kx2` only, whose design file `picosoc/picosoc-kx2.yaml`
passes.

## Regression cases that upstream runs only by name

`dsp-const-only-pins`, `lutram-ram32x2s` and `lutram-ram32x1s` are not in `run.sh`'s default
list: upstream expects them to fail until fixes ship in the toolchain. They have design files,
and `python xeda_check.py --regression dsp-const-only-pins lutram-ram32x2s lutram-ram32x1s`
reproduces that: `run.sh` and xeda fail each of them the same way (`dsp-const-only-pins`: its
`check.sh` finds an untied pin; the two others: `ERROR: Unable to place cell 'u', no BELs
remaining to implement cell type 'RAM32X2S'` and `'RAM32X1S'`).

## The known red row: defaults mode

Repository mode is the oracle: its FASM must be the Makefile's. Defaults mode (xeda's own
synthesis recipe, with no project file) must build, and a different FASM there is information.
One upstream-CI project does not build in defaults mode, and `--all` reports it as failing:

- `litex-ddr-arty-s7`: xeda's own recipe gives a design that misses the 100 MHz `sys_clk`
  constraint (77.39 MHz; the Makefile's netlist reaches 107.30 MHz), and the demo's Makefile
  does not allow a timing failure, so the design file does not either. Repository mode builds
  it with the Makefile's FASM. The row waits on a change of xeda's default synthesis recipe for
  Xilinx parts (flattening the design; xeda pull request #135), after which it is to be checked
  again.
