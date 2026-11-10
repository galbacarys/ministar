# ministar

Automation for **untracked** DSLR astrophotography.

A camera, a tripod with a 3-way pan/tilt head, and a browser. No tracker, no
autoguiding, no GoTo. ministar plate-solves, focuses the lens, live-stacks short
exposures, and tells you which way to nudge the head. You supply the pointing.

## What it does

- **Plate-solves in a loop** with ASTAP, so it always knows where the camera is
  pointed and how it is rotated.
- **Drives the camera** over USB: ISO/Tv/Av, live view for manual focus, and still
  capture to a file.
- **Live-stacks** many short exposures, correcting translation and rotation, so a
  recognisable image appears while you are still at the tripod.
- **Tells you how to re-aim** when the target drifts, and how long an aim must hold
  before the run resumes.
- **Keeps the full-resolution originals.** The live stack is a preview; the final
  full-res stack is built offline from the saved RAW files.

It does **not** track, guide, GoTo, change filters, or do lucky imaging. It is not a
replacement for N.I.N.A., INDI or an equatorial mount.

## Required equipment

| | |
|---|---|
| **Camera** | Canon EOS DSLR with USB tethering. EOS 60D verified. A lens with electronic focus; body in PTP/"PC connection" mode, M mode, lens switch on MF. Battery or dummy battery, SD card. |
| **Mount** | A sturdy tripod with a 3-way pan/tilt head. No tracker, guiding or GoTo required. |
| **Appliance** | Raspberry Pi 4 or 5, 4 GB RAM, 32 GB+ SD card, 5 V PSU, case/cooling, and a USB SSD or exFAT flash drive for frames. Pi 5 is faster; Pi 4 works. |
| **Console** | A phone or tablet with a browser. |
| **Network** | Home WiFi or Ethernet for installation. In the field the Pi hosts its own access point; no internet is required. |
| **Optional** | Red screen filter, USB power bank, spare battery, larger SSD. |

The original Windows/EDSDK path remains available for desktop use and development;
the Raspberry Pi is the field appliance.

## Install on a Raspberry Pi

The MVP packaging is a single idempotent script that turns a fresh **Raspberry Pi OS
Lite 64-bit Trixie** (or Debian 13 arm64) system into the appliance. It installs the
camera stack, ASTAP + G05/D20, the app, a per-device CA/leaf, the access point and
boot-mode (one-shot WLAN), the systemd unit, and USB auto-mount.

### 1. Flash and boot

Use Raspberry Pi Imager:

- Raspberry Pi OS Lite, **64-bit, Trixie**
- enable SSH
- set a login user and password (use `astroberry` or `pi`, not `ministar`; the installer
  creates a system `ministar` account for the service)
- optionally set WiFi in Imager: it is kept as a client profile for the SETUP
  "reboot in WLAN mode" one-shot, but every normal boot still starts the AP
- set the WiFi country in Imager, or pass the same code to the installer

### 2. Install

Inspect first:

```bash
curl -fsSLO https://raw.githubusercontent.com/galbacarys/ministar/main/deploy/install.sh
less install.sh
sudo bash install.sh --country US --reboot
```

Or the one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/galbacarys/ministar/main/deploy/install.sh \
  | sudo bash -s -- --country US --reboot
```

`US` is only an example. `--country` is the **WiFi regulatory domain**, not a locale:
it decides the legal channels, transmit power and whether the radio may host an
access point. Use the two-letter code for the country where the Pi will operate.
There is no worldwide default; `00` is rejected. A missing or wrong domain can stop
the AP from starting or break local radio rules.

Run `--check` first if you want a no-change validation. Other options:

| Option | Meaning |
|---|---|
| `--country CC` | WiFi regulatory country where the Pi will operate (required) |
| `--ap-psk PSK` | AP passphrase, default `ministar`, minimum 8 characters |
| `--astap-db both\|g05\|d20\|none` | Star databases; default `both` |
| `--ref REF` | App branch/tag/commit, default `main` |
| `--check` | Validate and exit without changing anything |
| `--reboot` | Reboot at the end |

The installer refuses the astroberry development unit on the project's dev Pi. It
logs to `/var/log/ministar-install.log`. Full options and troubleshooting are in
[`deploy/README.md`](deploy/README.md).

### 3. After the reboot

The Pi boots into **AP mode by default**. It does not join a known WiFi automatically.
The SETUP screen has **"reboot in WLAN mode"**: that writes a one-shot flag, reboots,
and the next boot joins the known WiFi client instead. The flag is consumed at boot,
so the boot after that returns to AP.

- **AP:** `ministar-xxxx`, where `xxxx` is the last four hex digits of `wlan0`
- **PSK:** `ministar` (fixed by decision; the AP is the security boundary)
- **Console:** `https://ministar.local` (or `https://10.42.0.1` on the AP)
- **Card file:** `/boot/firmware/MINISTAR.txt` has the exact SSID, PSK, URL and CA
  fingerprint; readable on a PC
- **WLAN one-shot:** the next boot joins the known WiFi, the AP is down, and the
  console is at the Pi's LAN address (or `ministar.local`); the following boot
  returns to AP

The first HTTPS visit warns because the device has its own CA. You can continue once
-- geolocation still works -- or install the CA for a clean experience:

- **iOS:** open `http://10.42.0.1/ca.crt`, install the profile, then Settings ->
  General -> About -> Certificate Trust Settings -> enable full trust.
- **Android:** download the same file, then Settings -> Security -> Encryption &
  credentials -> Install a certificate -> CA certificate. Restart Chrome if needed.

If the phone cannot resolve `.local`, use `https://10.42.0.1` on the AP. On a WLAN
boot use the Pi's LAN address from `hostname -I`.

**Phone VPN note:** Tailscale or another VPN on the phone can intercept `.local` and
local-network traffic. Turn it off while using the console; the Pi's own Tailscale
can stay for remote administration.

### USB storage

Format a stick as **exFAT** (or FAT32), give it a label, and plug it in. udev mounts
it at `/media/<label>` and gives the `ministar` user ownership. The SETUP screen lists
internal storage and every mounted stick, with free space; the first writable USB
stick is the default output. Sessions go to `/media/<label>/ministar/`.

ext4 sticks are not auto-mounted (their `uid`/`gid` mount options differ); mount one
manually if needed. The app refuses to create a `/media/...` path when the stick is
not actually mounted, so a missing drive cannot silently fill the SD card.

## First session

1. Connect the camera by USB, set **PTP/PC connection**, **M** mode, and the lens
   switch to **MF**.
2. Open the console. The wizard runs **PREFLIGHT -> SETUP -> TARGET -> PARAMETERS ->
   FOCUS -> POINT -> RUN -> DONE**.
3. In SETUP, set the observing site and choose the output drive (USB by default).
4. In FOCUS, use live view and the autofocus routine (or the manual drive buttons).
5. In POINT, solve, then follow the nudge/roll advice to place the target.
6. In RUN, capture. The live stack is the framing/noise preview; the CR2s are the
   full-resolution originals.
7. For the final image, copy the session's CR2s to a laptop and stack them at full
   resolution (Siril or a future offline pass). The live stack is not the final image.

The Pi's live loop is currently serial: capture, RAW decode, stack, and an occasional
solve happen one after another. A capture-first worker is pending; see the status
below before planning a long session.

## Windows and development

The desktop path uses Canon's EDSDK on native Windows. Development checkout:

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
```

Run the console:

```bash
python -m ministar.ui.app            # real EDSDK camera on Windows
python -m ministar.ui.app --fake     # synthetic stars, no camera
./tools/ui.sh                        # WSL sync + launch on Windows
```

EDSDK is borrowed from an installed application during development
(`EDSDK_PATH` overrides it); it is Canon proprietary and is never committed. ASTAP
and dcraw are driven as subprocesses. The WSL-to-Windows mechanics, socket and
logging gotchas are in [`docs/research/00-windows-interop.md`](docs/research/00-windows-interop.md).

On the Pi the service is `ministar.service`:

```bash
systemctl status ministar
journalctl -u ministar -f
curl -sk https://127.0.0.1/api/status
```

## Design notes

- **The live stack is a preview.** Full-resolution CR2s are kept; the final stack is
  offline. Preview resolution and frame sampling may be sacrificed for cadence.
- **Inform, don't block.** The UI shows what a choice costs (duty cycle, trailing,
  cadence); it does not refuse the choice.
- **One pipeline, no observing modes.** Declination, focal length and cadence vary
  smoothly; the tool takes the worse of the translation and rotation limits.
- **Robustness over efficiency.** Short exposures, hand aiming and a cheap tripod
  mean things go wrong; failing visibly beats failing silently.
- **No cloud, no login.** The network is the security boundary. At a dark site that
  is the Pi's WPA2 AP; do not expose it on an untrusted LAN.

## Research

Each brief records what was verified, how, and what remains unverified. Where
documentation and observed behaviour disagreed, observed behaviour wins.

| # | Document | Covers |
|---|---|---|
| 00 | [windows-interop.md](docs/research/00-windows-interop.md) | The WSL2 -> Windows build/run pattern. Verified mechanics. |
| 01 | [astap-cli.md](docs/research/01-astap-cli.md) | Plate solving: CLI contract, `.ini`, exit codes, timings. |
| 02 | [canon-usb-control.md](docs/research/02-canon-usb-control.md) | Canon bodies over USB: EDSDK vs. open-source paths. |
| 03 | [live-stacking-lucky-imaging.md](docs/research/03-live-stacking-lucky-imaging.md) | Registration, stacking math, why lucky imaging is not for this. |
| 04 | [reframing-and-untracked-geometry.md](docs/research/04-reframing-and-untracked-geometry.md) | Which way to turn the knob; drift rates; nudge UX. |
| 05 | [python-on-windows.md](docs/research/05-python-on-windows.md) | Interpreter, packaging, `pyedsdk` feasibility. |
| 06 | [stacking-backends.md](docs/research/06-stacking-backends.md) | Siril/ASTAP/DSS vs. owning the numerics in numpy. |
| 07 | [altaz-reframing-spec.md](docs/research/07-altaz-reframing-spec.md) | Alt/az reframing: per-axis error, `cos(alt)`, tolerances. |
| 08 | [edsdk-binding-plan.md](docs/research/08-edsdk-binding-plan.md) | Fork pyedsdk; port N.I.N.A.'s Canon knowledge. |
| 09 | [python-gui-windows.md](docs/research/09-python-gui-windows.md) | GUI toolkit measurements and the 24 MP preview path. |
| 10 | [target-catalog-and-reframe-envelope.md](docs/research/10-target-catalog-and-reframe-envelope.md) | OpenNGC catalog; drift envelope and reframe cadence. |
| 11 | [autofocus.md](docs/research/11-autofocus.md) | Lens drive primitive, HFD, V-curve fit. |
| 12 | [session-flow.md](docs/research/12-session-flow.md) | The setup wizard and its dependency order. |
| 13 | [head-calibration.md](docs/research/13-head-calibration.md) | Degrees to knob turns: the 2x2 head calibration. |
| 14 | [portability-browser-ui-and-linux-sbc.md](docs/research/14-portability-browser-ui-and-linux-sbc.md) | NiceGUI, libgphoto2, SBC deployment. |
| 15 | [stacking-pipeline-and-calibration.md](docs/research/15-stacking-pipeline-and-calibration.md) | Per-frame accumulator, no dark library, flats at the end. |
| 16 | [verified-hardware-eos-60d.md](docs/research/16-verified-hardware-eos-60d.md) | First hardware run: focus-by-wire, warm-up, HFD defects. |
| 17 | [raspberry-pi-port-and-provisioning.md](docs/research/17-raspberry-pi-port-and-provisioning.md) | Pi port: gphoto2, appliance runtime, AP/mDNS, HTTPS, installer vs image, risks. |

Field notes from real sessions are in [`docs/field-notes/`](docs/field-notes/). The
first, [2026-11-07](docs/field-notes/2026-11-07.md), records the first real solve,
focus run and captures, plus the flats/packaging/Pi work that followed.

## Status

The wizard runs end to end on Windows (EDSDK). On a Raspberry Pi 4 with an EOS 60D,
the gphoto2 backend, focus, live view, HTTPS, AP, session persistence and USB output
are verified; a full RUN on the Pi is the next field test. The one-command installer
is the packaging MVP; the first fresh-image run is the current validation step.

**Working**

| Area | State |
|---|---|
| ASTAP driver | Complete; hinted solve ~0.74 s on the laptop and 1.2 s on the Pi for a real 100 mm frame |
| Focus | HFD metric, V-curve, motion planner and routine verified on the 60D over both EDSDK and gphoto2 live view |
| Canon EDSDK backend | Enumerate, session, live view, capture, ISO/Tv/Av read-back on the 60D |
| gphoto2 backend | Settings read/write, live view, focus pulses, capture; verified on the Pi 4 + 60D |
| Live stacking | 2K preview, projective warp, sigma gate, frozen STF; full-res originals retained |
| RUN + reframe | Start/pause/stop, pacing, bump re-solve/rebase, auto-resume after a stable aim |
| POINT | Roll advice, entry point, nudge guidance |
| Exposure limits | Trail, NPF and 500-rule shown side by side |
| Session state | Wizard guards plus `session.json` persistence; RUN resumes at POINT after a restart |
| UI | NiceGUI wizard over HTTPS, red-on-black, phone-friendly |
| Pi appliance | systemd, AP default on boot, one-shot WLAN button, avahi `ministar.local`, per-device CA/leaf, port-80 landing/CA page |
| USB output | exFAT/FAT auto-mount, default output selection, mount-safe validation |
| Tests | 718 locally; 716 pass on the Pi with 2 skips |

**Pi 4 measurements** (EOS 60D, Debian 13, real 100 mm frame)

| Measurement | Result |
|---|---|
| Live view | 30 fps at 1056x704, ~338 KB JPEG |
| Capture at 1/60 | 1.9 s, 19.3 MB CR2 |
| ASTAP on a real frame | 1.2 s with G05, full frame |
| RAW decode, pattern cached | 2.0 s (was 4.0 s with a per-frame FITS) |
| Live stack | ~2.0 s/frame at bin 4, ~3.9 s at bin 2 |
| RUN loop | serial: capture + decode + stack are not yet overlapped |

**Pending**

| Item | Note |
|---|---|
| Capture-first worker | Single-slot latest-frame mailbox, 12 s preview throttle; the key to full-res cadence on a Pi 4 |
| Auto-POINT on a confirmed bump | Current code recovers/re-bases and warns; the automatic phase transition and in-app alert are designed but not implemented |
| Full-res restack | Siril/offline pass from the saved CR2s; the live stack is preview only |
| Flats, darks, bias | Flat divide is wired per frame; nothing produces the calibration frames yet |
| Head calibration | Degrees-per-turn and backlash; needed for knob-turn instructions |
| Real-frame stack quality review | First real captures exist; the stacked output has not been judged |
| Image/firstboot | The installer is the MVP; the flashable image and firstboot are next |
| PWA/notifications | HTTPS and the landing page exist; offline notification delivery is not yet built |

**Open validation**

- Real head degrees-per-turn and backlash are unmeasured.
- The reframe auto-resume has only run on the fake rig; a real meridian flip or long
  gap between reframes has not been observed.
- The live stack's cadence on the Pi is currently serial; measure again after the
  capture-first worker.

## Repository

```
src/ministar/      camera backends, solver, focus, stack, run loop, session, UI
deploy/            Pi installer, systemd units, AP/TLS/USB helpers
docs/research/     the briefs above
docs/field-notes/  real sessions and their defects
tests/             unit and fake-camera tests
tools/             Windows dev entry points and the M0 hardware probe
third_party/       OpenNGC catalogue, vendored unmodified
```

## Licence

MIT -- see [LICENSE](LICENSE). It covers this project's own code.

- **EDSDK.dll (Canon)** is proprietary, never committed, and only used on the Windows
  path. Shipping it in an installer is governed by Canon's SDK agreement.
- **ASTAP (MPL-2.0)**, **Siril (GPL-3.0)** and **dcraw (GPL-2.0)** are separate
  subprocesses and are never linked/imported.
- **python-gphoto2 (LGPL)** is used on Linux as a dynamically loaded dependency, not
  vendored into this source tree.
- **OpenNGC** is CC-BY-SA-4.0, vendored unmodified with its licence and attribution.
- Runtime dependencies otherwise are permissive: numpy (BSD-3), OpenCV (Apache-2.0),
  Pillow (MIT-CMU), NiceGUI (MIT).
