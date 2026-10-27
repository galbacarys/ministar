"""Paths and third-party tool discovery.

Encodes the layout established in ``docs/research/00-windows-interop.md``:

* source lives on the WSL filesystem; runtime I/O happens on NTFS;
* nothing is installed under ``Program Files`` because several of the tools we
  drive write sidecar files next to their input (ASTAP is the concrete case);
* third-party binaries live under ``<home>/tools``.

Nothing here imports Windows-only modules at import time, so the pure logic is
testable on any OS.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "ministar"

ASTAP_EXE = "astap_cli.exe"
EDSDK_DLL = "EDSDK.dll"


def default_home() -> Path:
    """Root for runtime data and vendored tool binaries."""
    if env := os.environ.get("MINISTAR_HOME"):
        return Path(env)
    if sys.platform == "win32":
        return Path(r"C:\ministar")
    # Developer fallback when running the pure-logic parts off-Windows.
    return Path.home() / ".ministar"


@dataclass(frozen=True)
class Paths:
    """Runtime directory layout.

    ``build`` is the rsync target described in 00 §3 and is deliberately
    disposable — never edit files there.
    """

    home: Path
    build: Path
    data: Path
    frames: Path
    scratch: Path
    output: Path
    logs: Path
    tools: Path

    @classmethod
    def under(cls, home: Path) -> Paths:
        data = home / "data"
        return cls(
            home=home,
            build=home / "build",
            data=data,
            frames=data / "frames",
            scratch=data / "scratch",
            output=data / "output",
            logs=data / "logs",
            tools=home / "tools",
        )

    def ensure(self) -> None:
        for path in (self.frames, self.scratch, self.output, self.logs, self.tools):
            path.mkdir(parents=True, exist_ok=True)


#: Standard ASTAP install locations on Windows. ASTAP must find its star
#: database beside the executable, so we look for the exe, not the folder.
_ASTAP_DIRS = (
    Path(r"C:\Program Files\astap"),
    Path(r"C:\Program Files (x86)\astap"),
    Path(r"C:\astap"),
)


def find_astap(paths: Paths) -> Path | None:
    """Locate ``astap_cli.exe``. Override with ``MINISTAR_ASTAP``."""
    if env := os.environ.get("MINISTAR_ASTAP"):
        candidate = Path(env)
        return candidate if candidate.is_file() else None

    for directory in (paths.tools / "astap", *_ASTAP_DIRS):
        candidate = directory / ASTAP_EXE
        if candidate.is_file():
            return candidate
    return None


def find_edsdk(paths: Paths) -> Path | None:
    """Locate ``EDSDK.dll``. Override with ``EDSDK_PATH`` (file or directory).

    ``EDSDK.dll`` is Canon proprietary and must never be committed. See
    08 §4: ship it inside the installer, keep it out of version control. We
    only ever look in places the user controls.
    """
    env = os.environ.get("EDSDK_PATH") or os.environ.get("MINISTAR_EDSDK")
    if env:
        candidate = Path(env)
        if candidate.is_dir():
            candidate = candidate / EDSDK_DLL
        return candidate if candidate.is_file() else None

    for directory in (paths.tools / "edsdk", paths.tools):
        candidate = directory / EDSDK_DLL
        if candidate.is_file():
            return candidate
    return None


def find_borrowed_edsdk() -> list[Path]:
    """Other applications' bundled ``EDSDK.dll`` copies, for diagnostics only.

    The list itself now lives beside the loader (:data:`edsdk_ffi.BORROWED_DLLS`) so
    there is exactly one of it. It used to be duplicated here, which is how the
    loader came to prefer N.I.N.A.'s copy while this scan never looked there.
    """
    from ministar.camera.edsdk_ffi import BORROWED_DLLS

    return [path for path in BORROWED_DLLS if Path(path).is_file()]


DCRAW_EXE = "dcraw.exe"


def find_dcraw(astap: Path | None, paths: Paths) -> Path | None:
    """Locate ``dcraw.exe``, which ships inside the ASTAP installation (01 §9).

    Override with ``MINISTAR_DCRAW`` (file or directory). This is the RAW decoder, so
    "not found" is a hard error for conversion and a warning for everything else --
    ``astap_cli.exe`` cannot read RAW, so nothing else can fill the gap.
    """
    env = os.environ.get("MINISTAR_DCRAW")
    if env:
        candidate = Path(env)
        if candidate.is_dir():
            candidate = candidate / DCRAW_EXE
        return candidate if candidate.is_file() else None

    directories = [paths.tools / "astap"]
    if astap is not None:
        directories.insert(0, astap.parent)
    for directory in directories:
        candidate = directory / DCRAW_EXE
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class Settings:
    """Everything resolved at startup."""

    paths: Paths
    astap: Path | None
    edsdk: Path | None
    dcraw: Path | None = None

    @classmethod
    def load(cls, home: Path | None = None) -> Settings:
        paths = Paths.under(home or default_home())
        astap = find_astap(paths)
        return cls(
            paths=paths,
            astap=astap,
            edsdk=find_edsdk(paths),
            dcraw=find_dcraw(astap, paths),
        )
