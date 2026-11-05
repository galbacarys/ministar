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
- **No observing modes.** One continuous parameter range. Declination, focal length and
  cadence mix smoothly, so the tool takes the worse of the translation and rotation limits
  rather than switching behaviour at a threshold (10 §2.5). A "polar mode" would be a
  relaxation of the same numbers — a second code path that can disagree with the first.
  Declination changes the mix, never the pipeline.

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
| 09 | [python-gui-windows.md](docs/research/09-python-gui-windows.md) | GUI toolkit: PySide6 vs Tk/wx/Dear PyGui/webview, measured cold start, bundle size, 24 MP preview path, threading contract, LGPL bundling. |
| 10 | [target-catalog-and-reframe-envelope.md](docs/research/10-target-catalog-and-reframe-envelope.md) | OpenNGC catalog; the drift envelope and reframe cadence. |
| 11 | [autofocus.md](docs/research/11-autofocus.md) | Lens autofocus: drive primitive, HFD, V-curve fit; MF required. |
| 12 | [session-flow.md](docs/research/12-session-flow.md) | The setup wizard: preflight, target, parameters, focus, point, run. |
| 13 | [head-calibration.md](docs/research/13-head-calibration.md) | Converts degrees into knob turns: the 2x2 head calibration. |
| 14 | [portability-browser-ui-and-linux-sbc.md](docs/research/14-portability-browser-ui-and-linux-sbc.md) | Browser UI (NiceGUI), libgphoto2 port, SBC deployment. |
| 15 | [stacking-pipeline-and-calibration.md](docs/research/15-stacking-pipeline-and-calibration.md) | Per-frame accumulator, why no dark library, flats at the end. |
| 16 | [verified-hardware-eos-60d.md](docs/research/16-verified-hardware-eos-60d.md) | First hardware run: STM focus-by-wire works, 0xA102 is warm-up, HFD fabricates stars. |

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
- **EDSDK belongs to the thread that opened the session, not to a lock.** A blocking SDK call
  from any other thread does not return until the owning thread pumps `EdsGetEvent`. Measured on
  the 60D: the same call took 0.06 s on the owner thread and never returned on a worker. So live
  view had never worked in the app, and a capture taken from the event loop wedged the console.
  One thread now opens, uses and closes the session; every other thread queues a job. See 16 §4.
- **`Evf_Mode` must not be written.** `Evf_OutputDevice = PC` alone starts live view in 0.06 s;
  the legacy mode write has been observed both returning and never returning on this body. It is
  gone. See 16 §4.
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
  camera/          base.py = interface + Tv table; edsdk.py = live view, lens drive,
                   still capture (verified on an EOS 60D), protocol logic injectable
  catalog.py       OpenNGC loader: 13 372 objects, bands folded onto one scale
  optics.py        what a frame covers: plate scale, fill, seconds between reframes
  rig.py           the rig profile and where its numbers came from (solved/exif/typed)
  targets.py       filtering and ranking for tonight, frame-relative when a rig is set
  raw.py           RAW -> FITS via dcraw, undebayered, headers from EXIF
  focus/           autofocus: HFD metric, V-curve fit, motion planner, calibration
  session/state.py the setup wizard as a dependency-checked state machine
  io/              fits.py (reader + writer), tiff.py (EXIF and pixel data)
  stack/           live stacking: frames, transform (the known model), accumulator,
                   stretch, stacker (warp, gate, accumulate, crop, encode)
  run/             the RUN loop: runner.py (capture, stack, pace, reframe) and
                   reframe.py (how long an aim must hold before resuming)
  reframe/         roll.py = which way to turn the body (10 §2.1c); the knob-turn
                   half waits on the head profile (13)
  pointing.py      where the target should start, and which way to nudge (10 §5)
  exposure.py      sub-length limits: pixel trail, NPF, the 500-rule, Canon's Tv grid
  ui/              NiceGUI wizard: screens.py, theme.py (red-on-black), app.py
tools/
  sync.sh          rsync to C:\ministar\build, optionally run there
  setup.bat        create the Windows venv (once)
  ministar.bat     the only Windows entry point
  ui.bat           launch the wizard (the Windows entry point)
  ui.sh            sync + launch the wizard from WSL (the dev entry point)
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

### Running the app

```bash
./tools/ui.sh            # sync, then launch the console against the real camera
./tools/ui.sh --fake     # synthetic stars, no camera attached
./tools/ui.sh --stop     # stop the server
./tools/ui.sh --restart  # stop, then launch
```

That is the whole procedure; `ui.sh` wraps the three steps that cost real time to
rediscover when done by hand.

1. **It syncs first.** Windows cannot open ext4 paths, so the app always runs from
   the mirrored `C:\ministar\build`, never from this checkout (00 §2–3).
2. **It launches detached.** A plain `cmd.exe /c tools\ui.bat` from WSL blocks: the
   interop layer waits on the whole Windows process tree, so a server that is meant
   to keep running looks like a hang. The app starts fine either way — the difference
   is only whether your shell comes back.
3. **It verifies from the Windows stack.** WSL2 runs in mirrored networking here, so
   `curl http://localhost:8080` from bash can hit WSL's own stack and report a failure
   that is not real. The script asks `curl.exe` instead (00 §5).

Three things that bite when you drive it by hand instead:

- **Check reachability from Windows**, not WSL:
  `cmd.exe /c "curl.exe -s -o NUL -w %{http_code} http://127.0.0.1:8080/"`
- **Stop by PID, not by the launcher.** A bound NiceGUI socket outlives the bash
  process group; the next launch then fails with *only one usage of each socket
  address* (00 §8). `netstat -ano | findstr :8080` gives the PID for
  `taskkill /F /PID <pid>` — or just use `./tools/ui.sh --stop`.
- **There is no console window; there is a log.** The server runs in the background and
  writes to `C:\ministar\data\logs\ui.log` — `tail -f` it to watch a session, and read
  it first when a launch fails. Want the live window back? Run
  `C:\ministar\build\tools\ui.bat` from a Windows command prompt; that path blocks and
  shows the output directly. The app binds all interfaces so a phone on the tailnet can
  drive the session.

**EDSDK.** The app borrows a copy that another program installed: N.I.N.A.'s first,
Lightroom's second (`edsdk_ffi.BORROWED_DLLS`). Either is fine for development — that
is what every hardware session so far has used. `EDSDK_PATH` overrides the choice.
Our own copy belongs at `C:\ministar\tools\edsdk\EDSDK.dll` and is what the installer
ships; it stays out of git because it is Canon's binary (08 §4).

The console binds **all interfaces** (`0.0.0.0`) so it can be driven from a phone on
the same private network -- Tailscale in practice. There is no authentication, so the
network is the security boundary: `--host 127.0.0.1` restores loopback-only.

Verified working end to end: hinting a solve from a previous solution takes
**~860 ms** including interpreter startup, versus 4-8 s for `-fov 0`.

Two things worth knowing before editing:

- **`tools/*.bat` must keep CRLF endings.** With LF, `cmd.exe` misparses the file
  and silently loses the exit code (00 §4). `.gitattributes` enforces this.
- **Anything printed must be ASCII.** The Windows console is code page 437/850
  and em-dashes render as boxes (00 §7). `tests/test_cli_ascii.py` enforces it.

## Status

The wizard runs end to end, from PREFLIGHT to DONE, on Windows. 575 tests pass, and the
commit history records the defects found by running against the user's own frames and the real
body rather than only synthetic data. Two different kinds of confidence apply, and the
difference matters when reading the table below: the **camera** path has been verified on the
hardware it will ship against (an EOS 60D), while the **stack** path has so far only seen
synthetic frames. The fake rig is a real star field -- decoded from ASTAP's own database -- but
it is still one mono plane, not three CFA planes of independent sensor noise.

**Working**

| Area | State |
|---|---|
| ASTAP driver | complete; hinted solve measured at **0.74 s** |
| Focus: HFD metric, V-curve fit, motion planner, routine | complete, validated on real frames. Each sample is the median of ~10 frames (spread 0.6% -> 0.2%, 1.5 s) and a failed run keeps the hand focus one button away |
| Canon backend (EDSDK) | enumerate, session, live view, still capture to file, ISO/Tv/Av write + read-back: **verified on the 60D**. The preview is STF-stretched (8.6 fps, 71 KB/frame) and live view is stood down -- mirror down -- whenever no screen is showing it. `capture()` (a decoded array) is deliberately absent: the loop reads the file the body produced |
| Live stacking | complete and measured on the fake rig: **~0.59 s/frame** at 4.5 MP, projective transform, sigma gate, frozen STF (15) |
| RUN loop + reframe sub-cycle | complete: start/pause/stop, measured pacing, bump -> re-solve -> rebase, auto-resume after the aim holds 15 s |
| POINT: roll advice, entry point, nudges | complete (10 §2.1c, §5) |
| Exposure limits | trail model, NPF, 500-rule shown side by side rather than a gate (03) |
| Session state machine | complete (linear wizard, guards, persistence) |
| Preflight / config / Windows entry points | complete, verified on Windows |
| UI wizard | complete through RUN and DONE (NiceGUI); `ui.bat`, plus `--fake` with no hardware |
| FITS reader, `ministar hfd` | complete |

**Pending implementation** (designs exist, code does not)

| Item | Brief | Note |
|---|---|---|
| `CALIBRATE` phase + head profile store | 12, 13 | no `Phase` member; `render_calibrate` is a `_not_implemented` stub |
| Knob-turn instructions | 07, 13 | roll advice exists; degrees -> turns needs the head profile above |
| A run on real frames | 15 | the stack path has never seen a CFA frame; dcraw decode cost is the largest unmeasured term in the per-frame budget |
| Calibration frames | 15 | the flat divide is wired per frame in sensor coordinates; nothing produces a flat yet. No darks, no bias |
| End-of-session restack | 06 | Siril as a subprocess; live stacking does not use it |
| libgphoto2 camera backend | 14 | for the Linux/SBC target |
| Native window / packaging | 09, 14 | browser UI works; `--native` and an installer do not exist yet |

**Outstanding research / validation**

- The **star-database ceiling is a live problem**, not theoretical. A real 100 mm frame has an
  8.53° field, which exceeds D80's 5.14° limit; ASTAP silently cropped it and reported a solution
  for the *cropped* centre. G05/W08 need installing, and the database must be chosen from the
  computed field *before* solving.
- **A real head's degrees-per-turn is unmeasured.** 13 uses 7.2°/turn, cited rather than observed;
  the entire instruction vocabulary depends on it.
- Backlash magnitude on a real 3-way head is unmeasured, and it sets whether knob instructions can
  hit the aiming tolerance at all.
- The reframe's auto-resume has only been exercised on the fake rig. On a real body it depends on a
  solve taken *after* the reframe began, and neither a meridian flip nor a long gap between
  reframes has been observed (15, still open).

## Licence

MIT -- see [LICENSE](LICENSE). It covers this project's own code.

Four dependencies need care, and none of them change the licence of *our* code:

- **EDSDK.dll (Canon) is proprietary and is not covered by MIT.** It is never committed
  (`.gitignore` blocks `*.dll`) and is currently loaded from an already-installed application.
  Shipping it inside an installer, as 08 section 4 plans, is governed by Canon's SDK agreement
  rather than by this one.
- **PySide6 is gone, and should stay gone.** It is LGPL-3.0, it was only ever an optional `ui`
  extra, and brief 14 replaced it with NiceGUI (MIT). Nothing imports it, so the runtime carries
  no copyleft dependency; adding one back is the thing to avoid, not a thing to plan around.
- **Siril (GPL-3.0)** and **ASTAP (MPL-2.0)** are only ever invoked as separate processes and never
  linked, which is what keeps this project's licence unaffected. The same reasoning must keep
  applying: do not import Siril.

- **The OpenNGC catalogue is CC-BY-SA-4.0**, vendored *unmodified* in `third_party/openngc/`
  with its licence text and attribution. Share-alike covers the data file, not this project's
  code. Shipping it verbatim is deliberate: a reduced or converted copy would be a derivative
  needing a "changes were made" notice, and a JSON conversion would become an artifact to
  maintain. Parsing the CSV costs 59 ms for 14 034 rows, so there is nothing to gain.

Runtime dependencies otherwise are permissive: numpy (BSD-3-Clause), OpenCV (Apache-2.0),
Pillow (MIT-CMU), and NiceGUI (MIT). SEP and python-gphoto2 are LGPL, so if either is adopted as a
library rather than a subprocess, revisit this section.
