# What this fork does not build through xeda, and why

Every project that upstream's CI builds has a design file in this fork, or an entry in
`xeda-exclusions.yaml` with its reason. `python xeda_check.py --ci-list` checks that, and fails
on a project with neither. This page gives the evidence behind each entry. It also lists the
design files that build on neither route (not with the Makefile, not with xeda), the regression
cases that upstream runs only by name, and the one upstream-CI project that does not build in
xeda's defaults mode.

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

## Design files that build on neither route

These designs are not in upstream's CI. The Makefile fails with the toolchain above, and xeda
fails the same way, at the same place.

- `blinky-ypcb003381p1`: nextpnr stops at line 3 of `ypcb003381p1.xdc`,
  `create_clock -period 20.000 [get_ports clk] ;# 50 MHz`, with
  `ERROR: failed to parse target ';' (on line 3)`. The XDC parser of this nextpnr does not
  accept a `;#` comment at the end of a command.
- `picosoc/picosoc-genesys2.yaml`: `genesys2.v` drives `led[1:0]`, and
  `picosoc-genesys2.xdc` places only `led[0]`. nextpnr stops with
  `ERROR: FIXME: unconstrained IO not supported (pad led[1])`.

## Regression cases that upstream runs only by name

`dsp-const-only-pins`, `lutram-ram32x2s` and `lutram-ram32x1s` are not in `run.sh`'s default
list: upstream expects them to fail until fixes ship in the toolchain. They have design files,
and `python xeda_check.py --regression dsp-const-only-pins lutram-ram32x2s lutram-ram32x1s`
reproduces that: `run.sh` and xeda fail each of them the same way (`dsp-const-only-pins`: its
`check.sh` finds an untied pin; the two others: `ERROR: Unable to place cell 'u', no BELs
remaining to implement cell type 'RAM32X2S'` and `'RAM32X1S'`).

## Defaults mode

Repository mode is the oracle: its FASM must be the Makefile's. Defaults mode (xeda's own
synthesis recipe, with no project file) must build, and a different FASM there is information.
One upstream-CI project does not build in defaults mode:

- `litex-ddr-arty-s7`: xeda's own recipe gives a design that misses the 100 MHz `sys_clk`
  constraint (77.39 MHz; the Makefile's netlist reaches 107.30 MHz), and the demo's Makefile
  does not allow a timing failure, so the design file does not either. Repository mode builds
  it with the Makefile's FASM.
