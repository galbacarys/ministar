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

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ministar.config import Settings, find_borrowed_edsdk

Status = Literal["ok", "warn", "fail", "info"]

#: Settings that have no dependable EDSDK property, or that we must confirm
#: before a session. Long-exposure noise reduction is menu-only: there is no
#: EDSDK property to read or set it (02, 05), and left on it doubles every
#: exposure -- halving throughput on a 2 s cadence.
CAMERA_CHECKS = (
    "Mirror lockup matches the configured expectation",
    "Long-exposure noise reduction is OFF (menu-only; cannot be read or set)",
    "Mode dial is in M",
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
    return Check(
        "platform",
        "warn",
        f"{sys.platform} -- camera and solver need Windows; pure logic is fine here",
    )


def _database_tiles(astap: Path) -> list[str]:
    """Star database tiles beside the executable (ASTAP looks there first)."""
    suffixes = ("*.1476", "*.290", "*.001")
    found: list[str] = []
    for suffix in suffixes:
        found.extend(p.name for p in astap.parent.glob(suffix))
    return found


def _check_astap(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    if settings.astap is None:
        return [
            Check(
                "astap",
                "fail",
                "astap_cli.exe not found -- set MINISTAR_ASTAP or install to "
                "C:\\Program Files\\astap",
            )
        ]

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
    if settings.edsdk is not None:
        return [Check("edsdk", "ok", str(settings.edsdk))]

    borrowed = find_borrowed_edsdk()
    if borrowed:
        detail = (
            f"not configured; a copy exists at {borrowed[0]} "
            "(another app's licence -- set EDSDK_PATH to use it for development)"
        )
        return [Check("edsdk", "warn", detail)]

    return [
        Check(
            "edsdk",
            "fail",
            "EDSDK.dll not found -- set EDSDK_PATH or place it in <home>/tools/edsdk",
        )
    ]


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
    checks.extend(_check_edsdk(settings))
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
