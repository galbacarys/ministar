# ministar

Automation for **untracked** DSLR astrophotography.

The user has three things: a camera, a laptop, and a tripod with a 3-way (pan/tilt)
head. No mount. No guiding. No GoTo. Nothing that moves on its own.

That constraint is the product. Everything expensive in this hobby is usually
solved by buying a tracker; this tool solves the annoying parts with software and
a human's hands instead.

## Design stance: it is allowed to be bad

This is a low-cost, low-equipment entry point — not a competitor to a tracked,
cooled, guided rig. A 60D on a second-hand tripod *is* the target hardware, and a
good night should produce a slightly soft, slightly grainy image of a real
deep-sky object. That is a good outcome, not a compromise.

That has consequences, and they are design rules rather than vibes:

- **Inform, never block.** Every efficiency number in `docs/research/` exists so the
  UI can tell the truth about what a choice costs — not so the app can refuse the
  choice. 400 mm on a fixed tripod is a 14% duty cycle (10 §2.1e); a user who knows
  that should be allowed to do it anyway. The failure mode to avoid is a *silent*
  bad result that looks like a bug, not a *chosen* bad result the user was warned
  about.
- **Fun first, quality second.** The first recognisable stack should appear
  quickly, at low resolution, slightly ugly. Polish is what the end-of-session
  restack is for. A tool that shows nothing for an hour and then a beautiful image
  does not get used.
- **Robustness beats efficiency.** Short exposures, hand aiming and a cheap tripod
  mean things go wrong constantly. Not hanging, not crashing, and not silently
  stopping matters far more than shaving 200 ms off a frame.
- **No purchase may be required.** If a feature only works with a different lens, a
  tracker, or a filter wheel, it is out of scope.

The tradeoffs stay in the docs and on screen; they just stop being gates.

## What it must do

1. **Plate-solve in a loop.** ASTAP over the CLI, continuously, so the tool always
   knows where the camera is pointed and how it is rotated.
2. **Drive the camera.** Exposure capture, ISO/aperture/shutter settings, live view
   for manual focusing, and a reliable intervalometer.
3. **Live-stack many short exposures.** Because the mount does not move, subs must be
   short, so there are hundreds of them. Align (translation *and* rotation), stack
   incrementally, and show a progressively better image.
4. **Help the human re-aim.** Tell the operator which way to nudge the head to acquire
   a target and to recover one that has drifted out of frame.

## Constraints

- **Windows only.** No Linux/macOS support, ever, for now.
- **Driven from a Windows shell.** Development happens in WSL2; runtime is native
  Windows. See [`docs/research/00-windows-interop.md`](docs/research/00-windows-interop.md).
- **Canon first.** Other brands later, behind an interface.
- **Laptop-class compute.** No GPU assumption. A real-time loop must survive on a CPU.

## Research

Everything below is research, not design-by-committee. Each brief records what was
verified, how, and what is still unverified. Where published documentation and observed
behaviour disagreed, observed behaviour wins and the discrepancy is recorded.

| # | Document | Covers |
|---|---|---|
| 00 | [windows-interop.md](docs/research/00-windows-interop.md) | The WSL2→Windows build/run pattern. Verified mechanics. |
| 01 | [astap-cli.md](docs/research/01-astap-cli.md) | Plate solving: exact CLI contract, `.ini` format, exit codes, timings. |
| 02 | [canon-usb-control.md](docs/research/02-canon-usb-control.md) | Canon bodies over USB: EDSDK vs. open-source paths. |
| 03 | [live-stacking-lucky-imaging.md](docs/research/03-live-stacking-lucky-imaging.md) | Registration, stacking math, what "lucky imaging" is actually for. |
| 04 | [reframing-and-untracked-geometry.md](docs/research/04-reframing-and-untracked-geometry.md) | Which way to turn the knob; drift rates; nudge UX. |
| 05 | [python-on-windows.md](docs/research/05-python-on-windows.md) | Interpreter, packaging, and whether `pyedsdk` can carry the capture path. |
| 06 | [stacking-backends.md](docs/research/06-stacking-backends.md) | Siril/ASTAP/DSS CLI vs. owning the numerics in numpy. |
| 07 | [altaz-reframing-spec.md](docs/research/07-altaz-reframing-spec.md) | Alt/az reframing: per-axis error, `cos(alt)`, tolerances, degeneracies. |
| 08 | [edsdk-binding-plan.md](docs/research/08-edsdk-binding-plan.md) | Fork pyedsdk; port N.I.N.A.'s Canon knowledge. |
| 09 | [python-gui-windows.md](docs/research/09-python-gui-windows.md) | GUI toolkit choice; PySide6 vs Tk, threading, packaging. |
| 10 | [target-catalog-and-reframe-envelope.md](docs/research/10-target-catalog-and-reframe-envelope.md) | OpenNGC catalog; the drift envelope and reframe cadence. |
| 11 | [autofocus.md](docs/research/11-autofocus.md) | Lens autofocus: drive primitive, HFD, V-curve fit; MF required. |
| 12 | [session-flow.md](docs/research/12-session-flow.md) | The setup wizard: preflight, target, parameters, focus, point, run. |
| 13 | [head-calibration.md](docs/research/13-head-calibration.md) | Converts degrees into knob turns: the 2x2 head calibration. |
| 14 | [portability-browser-ui-and-linux-sbc.md](docs/research/14-portability-browser-ui-and-linux-sbc.md) | Browser UI (NiceGUI), libgphoto2 port, SBC deployment. |
| 15 | [stacking-pipeline-and-calibration.md](docs/research/15-stacking-pipeline-and-calibration.md) | Per-frame accumulator, why no dark library, flats at the end. |
| 09 | [python-gui-windows.md](docs/research/09-python-gui-windows.md) | GUI toolkit: PySide6 vs Tk/wx/Dear PyGui/webview, measured cold start, bundle size, 24 MP preview path, threading contract, LGPL bundling. |

## Findings that change the design

These came out of the research and are load-bearing:

- **"Lucky imaging" is the wrong tool for deep sky at these focal lengths.** At 14–85 mm
  the PSF is a fraction of a pixel; there is nothing for frame selection to sharpen.
  Selection is for planets and long focal lengths. What we actually need is rotation-aware
  incremental stacking. See 03.
- **Rotation, not translation, is what breaks naive stacking** on a fixed tripod, and the
  rate is `Ω·sin δ` — independent of latitude. The widely-quoted
  `15.04·cos(lat)·cos(az)/cos(alt)` is a *tracking mount* quantity and is wrong here. See 03/04.
- **`-fov` in ASTAP is image HEIGHT, not diameter**, despite its own help text saying
  "diameter_field". Verified by observing the solver echo it back. See 01.
- **libgphoto2 has no Windows support.** The open-source Canon path on Windows is
  effectively digiCamControl, or WSL2 + `usbipd-win` (which is a dev convenience, not a
  shippable product). EDSDK is the real answer. See 02.
- **N.I.N.A. has a full native Canon driver**, in `EDCamera.cs`. It is the reference
  implementation to port domain knowledge from — bulb dialects, per-body quirks, the
  live-view property dance. MPL-2.0. See 08.
- **`pyedsdk` is MIT and 90% the way there** — it has the ctypes plumbing, the enum tables, the
  Windows message pump, and a working live-view stream. It lacks bulb, hard-codes a 15 s
  exposure timeout, and can't enumerate legal settings. Fork it; don't rewrite it. See 08.
- **`EDSDK.dll` is on this machine already** (13.19.0.6400, x64, inside Lightroom), and
  N.I.N.A. ships it inside its own installer. It may be redistributed *as part of an app* —
  just never committed to git. See 08.
- **A resident daemon is mandatory, not a preference.** Bare `python -c pass` is ~60 ms and
  `import numpy` ~235 ms, against a ~43 ms process-spawn budget. See 05.
- **Siril is the right tool for the end-of-session restack and the wrong one for the live
  loop.** Its headless live-stack rejects over a 2-sample window (a no-op) and round-trips a
  48 MB FITS per frame. See 06.
- **A frame with no detectable stars is indistinguishable from a bug.** ASTAP returns
  exit 1 for "0 stars found" just as it does for "searched the wrong patch of sky". Frame
  quality gating is not optional. See 01.
- **EDSDK has no absolute focus position** — only relative nudge pulses, three sizes each way,
  ~100 ms apart. Autofocus has to be built on a coordinate system we maintain ourselves,
  anchored at the far focus stop. See 11.
- **There is already a working implementation, and it is calibrated for this lens.** N.I.N.A.'s
  `Lens AF` plugin does exactly the "home to infinity, step back" algorithm, and its
  `lenses.json` on this machine records your 100-400L as focus position 0 at 100 mm and 5030 at
  400 mm. See 11 §2.

## Layout

```
src/ministar/
  config.py        runtime paths + ASTAP/EDSDK discovery
  preflight.py     environment checks, and the camera settings checklist
  cli.py           `ministar preflight | solve | version`
  solver/astap.py  ASTAP driver; the .ini contract is fully verified
  camera/          base.py = interface + Tv table; edsdk.py = not written yet
  focus/           autofocus: HFD metric, V-curve fit, motion planner, calibration
  session/state.py the setup wizard as a dependency-checked state machine
  io/fits.py       minimal FITS reader (no astropy)
  stack/           not written yet (design in 03, 06)
  reframe/         not written yet (spec in 07)
  ui/              not written yet (toolkit in 09)
tools/
  sync.sh          rsync to C:\ministar\build, optionally run there
  setup.bat        create the Windows venv (once)
  ministar.bat     the only Windows entry point
```

## Development

Source of truth is this checkout. Windows only ever sees a disposable copy.

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ministar hfd <frame.fit>   # star sharpness on a real frame
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .

./tools/sync.sh                       # mirror to C:\ministar\build
./tools/sync.sh preflight             # mirror, then run `ministar preflight` on Windows
```

One-time on the Windows side: `C:\ministar\build\tools\setup.bat`, then
`tools\ministar.bat preflight`.

Verified working end to end: hinting a solve from a previous solution takes
**~860 ms** including interpreter startup, versus 4-8 s for `-fov 0`.

Two things worth knowing before editing:

- **`tools/*.bat` must keep CRLF endings.** With LF, `cmd.exe` misparses the file
  and silently loses the exit code (00 §4). `.gitattributes` enforces this.
- **Anything printed must be ASCII.** The Windows console is code page 437/850
  and em-dashes render as boxes (00 §7). `tests/test_cli_ascii.py` enforces it.

## Status

Scaffolded. The algorithms that can be proven without hardware **are** proven: 77 tests, and the
commit history records five real defects found by running against the user's own frames rather
than only synthetic data. Nothing has yet touched the camera.

**Working**

| Area | State |
|---|---|
| ASTAP driver | complete; hinted solve measured at **0.74 s** |
| Focus: HFD metric, V-curve fit, motion planner, routine | complete, validated on real frames |
| Session state machine | complete (linear wizard, guards, persistence) |
| Preflight / config / Windows entry points | complete, verified on Windows |
| FITS reader, `ministar hfd` | complete |

**Pending implementation** (designs exist, code does not)

| Item | Brief | Note |
|---|---|---|
| EDSDK camera backend | 08 | the last untested assumption in the whole project |
| Live stacking | 03, 06 | in-process numpy; Siril only for the end-of-session restack |
| Reframe geometry (envelope, corner, turns) | 07, 10, 13 | the maths is verified; the code is not written |
| `CALIBRATE` phase + head profile store | 12, 13 | enum member, guard, JSON store |
| UI | 09, 14 | NiceGUI (supersedes PySide6); browser + native wrapper |
| libgphoto2 camera backend | 14 | for the Linux/SBC target |
| Live stack accumulator | 15 | sum/count/M2 at display res; no dark library |
| Calibration frames | 15 | flats terminal + optional; no darks, no bias |

**Outstanding research / validation**

- The **star-database ceiling is a live problem**, not theoretical. A real 100 mm frame has an
  8.53° field, which exceeds D80's 5.14° limit; ASTAP silently cropped it and reported a solution
  for the *cropped* centre. G05/W08 need installing, and the database must be chosen from the
  computed field *before* solving.
- **A real head's degrees-per-turn is unmeasured.** 13 uses 7.2°/turn, cited rather than observed;
  the entire instruction vocabulary depends on it.
- Backlash magnitude on a real 3-way head is unmeasured, and it sets whether knob instructions can
  hit the aiming tolerance at all.
- No decision recorded on the exposure planner that snaps to Canon's discrete Tv table.
