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
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

APP_NAME = "ministar"


def _exe(stem: str) -> str:
    """The platform's executable suffix. Linux has none; Windows always has .exe."""
    return f"{stem}.exe" if sys.platform == "win32" else stem


ASTAP_EXE = _exe("astap_cli")
EDSDK_DLL = "EDSDK.dll"


def session_token(text: str) -> str:
    """A piece of a target name that is safe as one path component on Windows."""
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    return token.strip("._-")[:48]


def safe_folder_name(text: str, *, limit: int = 80) -> str:
    """Keep a name the user typed, but never let it name a different directory.

    Spaces are fine on every platform; separators, drive colons and the Windows wildcard
    set are not, because ``output / "../elsewhere"`` would write outside the session root.
    """
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", text.strip())
    return cleaned.strip(" .")[:limit]


def default_session_name(target: str | None = None, when: datetime | None = None) -> str:
    """``YYYY_MM_DD-HH-MM-TARGETCODE``, with the target omitted when there is none.

    Local time, not UTC: this is the name a person reads on disk after the session, and
    it should match the clock on the phone they were using. Sanitised because a catalogue
    label can contain spaces and a common name, and Windows folder names cannot.
    """
    stamp = (when or datetime.now()).strftime("%Y_%m_%d-%H-%M")
    token = session_token(target or "")
    return f"{stamp}-{token}" if token else stamp


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


#: Standard ASTAP install locations. ASTAP finds its star database beside the
#: executable, so we look for the binary, not the folder. The Windows entries are
#: inert on Linux and vice versa; keeping one tuple makes the search order obvious.
_ASTAP_DIRS = (
    Path(r"C:\Program Files\astap"),
    Path(r"C:\Program Files (x86)\astap"),
    Path(r"C:\astap"),
    Path("/opt/astap"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
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
    if found := shutil.which(ASTAP_EXE):
        return Path(found)
    return None


def find_edsdk(paths: Paths) -> Path | None:
    """Locate ``EDSDK.dll``. Override with ``EDSDK_PATH`` (file or directory).

    ``EDSDK.dll`` is Canon proprietary and must never be committed. See
    08 §4: ship it inside the installer, keep it out of version control. We
    only ever look in places the user controls.
    """
    if sys.platform != "win32" and not (
        os.environ.get("EDSDK_PATH") or os.environ.get("MINISTAR_EDSDK")
    ):
        return None

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


DCRAW_EXE = _exe("dcraw")

#: On Linux either the original dcraw or LibRaw's emulation may be installed. Both
#: accept the flags in ``ministar.raw``; the original is preferred because that is what
#: the Windows build ships and what the FITS conversion was verified against.
_DCRAW_NAMES = ("dcraw", "dcraw_emu") if sys.platform != "win32" else (DCRAW_EXE,)


def find_dcraw(astap: Path | None, paths: Paths) -> Path | None:
    """Locate the RAW decoder, which ships inside the ASTAP installation on Windows.

    Override with ``MINISTAR_DCRAW`` (file or directory). This is the RAW decoder, so
    "not found" is a hard error for conversion and a warning for everything else --
    ``astap_cli`` cannot read RAW, so nothing else can fill the gap.
    """
    env = os.environ.get("MINISTAR_DCRAW")
    if env:
        candidate = Path(env)
        if candidate.is_dir():
            for name in _DCRAW_NAMES:
                if (candidate / name).is_file():
                    return candidate / name
            return None
        return candidate if candidate.is_file() else None

    directories = [paths.tools / "astap", Path("/opt/astap")]
    if astap is not None:
        directories.insert(0, astap.parent)
    for directory in directories:
        for name in _DCRAW_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    for name in _DCRAW_NAMES:
        if found := shutil.which(name):
            return Path(found)
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
