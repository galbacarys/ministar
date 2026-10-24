# ministar

Automation for **untracked** DSLR astrophotography.

The user has three things: a camera, a laptop, and a tripod with a 3-way (pan/tilt)
head. No mount. No guiding. No GoTo. Nothing that moves on its own.

That constraint is the product. Everything expensive in this hobby is usually
solved by buying a tracker; this tool solves the annoying parts with software and
a human's hands instead.

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
| 07 | *(planned)* | Alt/az reframing readout spec — per-axis error, tolerances, degeneracies. |
| 08 | [edsdk-binding-plan.md](docs/research/08-edsdk-binding-plan.md) | Fork pyedsdk; port N.I.N.A.'s Canon knowledge. |

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

## Status

Research phase. No code yet. The unresolved pieces are the alt/az readout spec (07) and a
confirmed good plate-solve frame — the only FITS files on this machine are starless N.I.N.A.
failures, so the success-path `.ini` contract in 01 §4 is still unconfirmed.
