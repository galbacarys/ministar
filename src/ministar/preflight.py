"""Startup environment checks.

The point of this module is the lesson from 08 sec.7.2b: the failures that cost
real sessions were **settings**, discovered by a human reading a message box.
N.I.N.A. surfaced mirror lockup only as

    "Camera settings: Mirror lockup, noise reduction off."

in a sequencer MessageBox, which is a reminder, not a check. We would rather
report the *actual* state and let the user see it.

Checks that do not need hardware run here. Checks that need a live camera
(mirror lockup, LENR, mode dial) run at session start in the camera layer and
are listed in :data:`CAMERA_CHECKS` so they cannot be silently dropped.
"""

from __future__ import annotations

import grp
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ministar.config import Settings

Status = Literal["ok", "warn", "fail", "info"]

#: Settings that have no dependable EDSDK property, or that we must confirm
#: before a session. Long-exposure noise reduction is menu-only: there is no
#: EDSDK property to read or set it (02, 05), and left on it doubles every
#: exposure -- halving throughput on a 2 s cadence.
#:
#: MF is here because the lens drive is a *manual* focus command
#: (``DriveManualFocus`` in the reference plugin, ``eNkMAIDMFDrive`` on Nikon) and
#: needs the switch on MF -- which is the mode a run wants anyway (11 sec. 1). It
#: cannot be read reliably from EDSDK, so it is a human check plus a motion
#: verification (11 sec. 9.1).
CAMERA_CHECKS = (
    "Mirror lockup matches the configured expectation",
    "Long-exposure noise reduction is OFF (menu-only; cannot be read or set)",
    "Mode dial is in M",
    "Lens AF/MF switch is on MF (required for lens drive; also stops it hunting)",
    "Auto power off is disabled or the shutdown timer is extended",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        return Check("python", "ok", f"{major}.{minor}")
    return Check("python", "fail", f"{major}.{minor} -- need >= 3.11")


def _check_platform() -> Check:
    if sys.platform == "win32":
        return Check("platform", "ok", "Windows")
    if sys.platform.startswith("linux"):
        return Check("platform", "ok", "Linux")
    return Check(
        "platform",
        "warn",
        f"{sys.platform} -- no camera backend for this platform; pure logic is fine here",
    )


def _database_tiles(astap: Path) -> list[str]:
    """Star database tiles beside the executable, then the Linux data directory."""
    suffixes = ("*.1476", "*.290", "*.001")
    roots = (astap.parent, Path("/usr/share/astap/data"))
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        for suffix in suffixes:
            for path in root.glob(suffix):
                if path.name not in seen:
                    seen.add(path.name)
                    found.append(path.name)
    return found


def _check_astap(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    if settings.astap is None:
        if sys.platform == "win32":
            hint = "set MINISTAR_ASTAP or install to C:\\Program Files\\astap"
            name = "astap_cli.exe"
        else:
            hint = "set MINISTAR_ASTAP or install to /opt/astap"
            name = "astap_cli"
        return [Check("astap", "fail", f"{name} not found -- {hint}")]

    checks.append(Check("astap", "ok", str(settings.astap)))

    tiles = _database_tiles(settings.astap)
    if not tiles:
        checks.append(
            Check(
                "astap database",
                "fail",
                "no star database tiles beside the executable; ASTAP will exit 32",
            )
        )
    elif not any(t.endswith(".001") or t.endswith(".290") for t in tiles):
        # .1476 tiles top out at 5.14 deg of field. Our users shoot 2-9 deg, so
        # a wide field will silently be cropped and CRVAL will describe the
        # crop, not the frame (01 sec.6).
        checks.append(
            Check(
                "astap database",
                "warn",
                f"{len(tiles)} tiles, all .1476 (max 5.14 deg field) -- "
                "install G05/W08 for fields wider than ~5 deg",
            )
        )
    else:
        checks.append(Check("astap database", "ok", f"{len(tiles)} tiles"))

    return checks


def _check_edsdk(settings: Settings) -> list[Check]:
    """Report the same DLL the loader would pick, not a separate guess.

    These used to disagree: the loader preferred N.I.N.A.'s bundled copy while this
    check scanned only Lightroom and ON1, so the app could be running against an SDK
    preflight had not noticed. One resolver, one answer -- see
    :func:`ministar.camera.edsdk_ffi.resolve_dll`.
    """
    from ministar.camera.edsdk_ffi import resolve_dll

    if settings.edsdk is not None:
        return [Check("edsdk", "ok", str(settings.edsdk))]

    try:
        path, origin = resolve_dll(None)
    except RuntimeError:
        return [
            Check(
                "edsdk",
                "fail",
                "EDSDK.dll not found -- set EDSDK_PATH or place it in <home>/tools/edsdk",
            )
        ]

    if origin == "borrowed":
        return [
            Check(
                "edsdk",
                "warn",
                f"development copy borrowed from {path.parent.parent.parent} "
                "-- fine for development; the installer ships our own unmodified copy "
                "in <home>/tools/edsdk (08 §4)",
            )
        ]
    return [Check("edsdk", "ok", str(path))]


def _check_gphoto2() -> list[Check]:
    """The Linux camera prerequisites, in the order that actually bites.

    libgphoto2 itself ships inside the Python wheel, so the import is the whole
    dependency. Permissions, the 32 MB usbfs default and USB autosuspend are the three
    failures that look like a dead camera rather than a configuration problem.
    """
    checks: list[Check] = []
    try:
        import gphoto2
    except ImportError:
        return [
            Check(
                "gphoto2",
                "fail",
                "python-gphoto2 is not installed -- `sudo apt install python3-gphoto2` "
                "or `pip install gphoto2` in the venv",
            )
        ]
    checks.append(Check("gphoto2", "ok", f"python-gphoto2 {gphoto2.__version__}"))

    usbfs = Path("/sys/module/usbcore/parameters/usbfs_memory_mb")
    if usbfs.is_file():
        try:
            value = int(usbfs.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            value = 0
        if value < 64:
            checks.append(
                Check(
                    "usbfs memory",
                    "warn",
                    f"{value} MB -- large CR2 downloads can fail; add "
                    "usbcore.usbfs_memory_mb=256 to cmdline.txt",
                )
            )
        else:
            checks.append(Check("usbfs memory", "ok", f"{value} MB"))

    rules = [
        *Path("/usr/lib/udev/rules.d").glob("*gphoto*"),
        *Path("/etc/udev/rules.d").glob("*gphoto*"),
    ]
    if rules:
        checks.append(Check("udev", "ok", rules[0].name))
    else:
        checks.append(
            Check(
                "udev",
                "warn",
                "no libgphoto2 udev rule; the camera may need root or a manual ACL",
            )
        )

    try:
        plugdev = grp.getgrnam("plugdev").gr_gid
    except KeyError:
        checks.append(Check("camera group", "info", "no plugdev group on this system"))
    else:
        if plugdev in os.getgroups():
            checks.append(Check("camera group", "ok", "plugdev"))
        else:
            checks.append(
                Check(
                    "camera group",
                    "warn",
                    "this user is not in plugdev; the service must be, or USB access "
                    "will be denied",
                )
            )

    canon = []
    for device in Path("/sys/bus/usb/devices").glob("*"):
        try:
            vendor = (device / "idVendor").read_text(encoding="utf-8").strip()
            product = (device / "idProduct").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if vendor == "04a9":  # Canon
            canon.append(f"{vendor}:{product}")
    if canon:
        checks.append(Check("camera usb", "ok", ", ".join(sorted(canon))))
    else:
        checks.append(
            Check("camera usb", "warn", "no Canon device on the USB bus (plug it in and power on)")
        )
    return checks


def _check_dirs(settings: Settings) -> Check:
    try:
        settings.paths.ensure()
    except OSError as exc:
        return Check("data dirs", "fail", str(exc))
    return Check("data dirs", "ok", str(settings.paths.data))


def run(settings: Settings) -> list[Check]:
    """Run every check that does not require hardware."""
    checks = [
        _check_platform(),
        _check_python(),
        _check_dirs(settings),
    ]
    checks.extend(_check_astap(settings))
    if sys.platform == "win32":
        checks.extend(_check_edsdk(settings))
    else:
        checks.extend(_check_gphoto2())
    return checks


_SYMBOL = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "info": "INFO"}


def render(checks: list[Check]) -> str:
    lines = [f"{_SYMBOL[c.status]}  {c.name:<16} {c.detail}" for c in checks]
    failures = sum(1 for c in checks if c.status == "fail")
    warnings = sum(1 for c in checks if c.status == "warn")
    lines.append("")
    lines.append(f"{failures} failure(s), {warnings} warning(s)")
    return "\n".join(lines)


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == "fail"]
