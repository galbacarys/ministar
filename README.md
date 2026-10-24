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
- **This machine has no usable Windows Python** — only the Microsoft Store placeholder
  stub. Toolchain choice is therefore a real decision, not a default. See 00.
- **A frame with no detectable stars is indistinguishable from a bug.** ASTAP returns
  exit 1 for "0 stars found" just as it does for "searched the wrong patch of sky". Frame
  quality gating is not optional. See 01.

## Status

Research phase. No code yet.
