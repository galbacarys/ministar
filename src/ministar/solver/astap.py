"""ASTAP plate solver driver.

Contract verified against ``astap_cli.exe`` **CLI-2025.10.11** on real frames —
see ``docs/research/01-astap-cli.md``. The parts that cost real time to learn:

* The success ``.ini`` has **exactly 13 keys and never contains ``DIMENSIONS``**.
* The file may begin with a **blank line** (the CLI build's failure output does).
* ``WARNING=`` can appear **alongside** ``PLTSOLVD=T``.
* ``-fov`` is the image **height** in degrees, not the diameter.
* ``CDELT1`` is negative and the CD determinant is negative for a normal
  (non-mirrored) FITS solution.
* Position angle uses ``CD1_2``/``CD2_2`` — column 2 — *not* ``CD2_1``.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Exit codes (01 §5). Note that codes >255 cannot survive a Unix ``$?``, and
#: ``-analyse``/``-focus`` deliberately smuggle values through the exit code, so
#: never rely on this alone — ``PLTSOLVD`` is authoritative.
EXIT_OK = 0
EXIT_NO_SOLUTION = 1
EXIT_NOT_ENOUGH_STARS = 2
EXIT_CANT_READ_IMAGE = 16
EXIT_NO_DATABASE = 32
EXIT_CANT_READ_DATABASE = 33
EXIT_UPDATE_FAILED = 34

EXIT_MEANINGS = {
    EXIT_OK: "solved",
    EXIT_NO_SOLUTION: "no solution",
    EXIT_NOT_ENOUGH_STARS: "not enough stars detected",
    EXIT_CANT_READ_IMAGE: "could not read image file",
    EXIT_NO_DATABASE: "star database not found",
    EXIT_CANT_READ_DATABASE: "could not read star database",
    EXIT_UPDATE_FAILED: "could not update input header",
}


@dataclass(frozen=True)
class Solution:
    """A successful WCS solution, as reported in the ``.ini`` sidecar."""

    crpix: tuple[float, float]
    crval: tuple[float, float]
    cdelt: tuple[float, float]
    crota: tuple[float, float]
    cd: tuple[float, float, float, float]
    cmdline: str = ""

    @property
    def pixel_scale_deg(self) -> float:
        """Degrees per pixel along the image +x axis."""
        cd1_1, _, cd2_1, _ = self.cd
        return math.hypot(cd1_1, cd2_1)

    @property
    def pixel_scale_arcsec(self) -> float:
        return self.pixel_scale_deg * 3600.0

    @property
    def position_angle_deg(self) -> float:
        """Position angle of the image +y axis, East of North, in [0, 360).

        Uses ``CD1_2`` (column 2, row 1) and ``CD2_2``. Using ``CD2_1`` instead
        is a classic sign error; see 01 §4 and 07 §2.1.
        """
        _, cd1_2, _, cd2_2 = self.cd
        return math.degrees(math.atan2(cd1_2, cd2_2)) % 360.0

    @property
    def parity(self) -> int:
        """``-1`` for a normal FITS solution, ``+1`` if mirrored.

        A mirrored solution flips every left/right instruction in the
        reframing UI (07 §2.1).
        """
        cd1_1, cd1_2, cd2_1, cd2_2 = self.cd
        det = cd1_1 * cd2_2 - cd1_2 * cd2_1
        return 1 if det > 0 else -1

    @property
    def is_mirrored(self) -> bool:
        return self.parity > 0

    def field_height_deg(self, height_px: int) -> float:
        """Field height in degrees for an image of ``height_px`` rows."""
        return abs(self.cdelt[1]) * height_px

    def field_width_deg(self, width_px: int) -> float:
        return abs(self.cdelt[0]) * width_px


@dataclass(frozen=True)
class AstapResult:
    """Outcome of a solve attempt, whether or not it succeeded."""

    solved: bool
    solution: Solution | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    cmdline: str = ""
    exit_code: int | None = None

    @property
    def error_meaning(self) -> str | None:
        if self.exit_code is None:
            return None
        return EXIT_MEANINGS.get(self.exit_code, f"unknown exit code {self.exit_code}")

    @property
    def starved(self) -> bool:
        """True if the frame simply had no detectable stars.

        This is the overwhelmingly common failure and it is *not* a solver
        problem — it means cloud, dew, defocus, cap on, or too-short exposure.
        It shares exit code 1 with "searched the wrong patch of sky", which is
        why frame quality gating must sit in front of the solver (01 §7).
        """
        return (self.error or "").lower().startswith("not enough stars")


def _read_key_value(path: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse ASTAP's ``key=value`` sidecar.

    Tolerates the leading blank line, CRLF endings, and any number of
    ``WARNING`` lines (which we collect rather than overwrite).
    """
    values: dict[str, str] = {}
    warnings: list[str] = []

    # ASCII per the contract; errors='replace' keeps a malformed file from
    # taking down a live capture loop.
    for raw in path.read_text(encoding="ascii", errors="replace").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "WARNING":
            warnings.append(value)
        elif key:
            values[key] = value

    return values, tuple(warnings)


def _floats(values: dict[str, str], *keys: str) -> tuple[float, ...]:
    return tuple(float(values[k]) for k in keys)


def parse_ini(path: Path) -> AstapResult:
    """Parse an ASTAP ``.ini`` sidecar.

    Raises :class:`FileNotFoundError` if absent — the caller should treat a
    missing sidecar as "the solver never ran".
    """
    values, warnings = _read_key_value(path)
    solved_flag = values.get("PLTSOLVD", "F").upper() == "T"
    cmdline = values.get("CMDLINE", "")

    if not solved_flag:
        return AstapResult(
            solved=False,
            error=values.get("ERROR"),
            warnings=warnings,
            cmdline=cmdline,
        )

    try:
        solution = Solution(
            crpix=_floats(values, "CRPIX1", "CRPIX2"),
            crval=_floats(values, "CRVAL1", "CRVAL2"),
            cdelt=_floats(values, "CDELT1", "CDELT2"),
            crota=_floats(values, "CROTA1", "CROTA2"),
            cd=_floats(values, "CD1_1", "CD1_2", "CD2_1", "CD2_2"),
            cmdline=cmdline,
        )
    except KeyError as exc:
        # PLTSOLVD=T but a required key is missing: treat as unsolved rather
        # than crashing a capture loop.
        return AstapResult(
            solved=False,
            error=f"solved but missing key {exc}",
            warnings=warnings,
            cmdline=cmdline,
        )

    return AstapResult(solved=True, solution=solution, warnings=warnings, cmdline=cmdline)


@dataclass(frozen=True)
class SolveHints:
    """Search hints. Always supply these in a live loop.

    A hinted solve measured 0.74 s against 4.0-8.2 s with ``-fov 0`` (01 §8).
    """

    fov_height_deg: float
    ra_hours: float | None = None
    south_pole_distance_deg: float | None = None
    radius_deg: float = 3.0
    max_stars: int = 300
    downsample: int = 0

    @classmethod
    def around(cls, solution: Solution, fov_height_deg: float, radius_deg: float = 3.0):
        """Hint from a previous solution on the same target."""
        return cls(
            fov_height_deg=fov_height_deg,
            ra_hours=solution.crval[0] / 15.0,
            south_pole_distance_deg=solution.crval[1] + 90.0,
            radius_deg=radius_deg,
        )


def build_args(
    image: Path,
    out_base: Path,
    hints: SolveHints | None = None,
    database: str | None = None,
    write_wcs: bool = True,
) -> list[str]:
    """Compose the argv (excluding the executable).

    Flags are single-dash and case-sensitive; ``-d`` is a path and ``-D`` is a
    database abbreviation (01 §2).
    """
    args = ["-f", str(image), "-o", str(out_base)]

    if hints is not None:
        args += ["-fov", f"{hints.fov_height_deg:.4f}", "-r", f"{hints.radius_deg:g}"]
        args += ["-s", str(hints.max_stars), "-z", str(hints.downsample)]
        if hints.ra_hours is not None:
            args += ["-ra", f"{hints.ra_hours:.6f}"]
        if hints.south_pole_distance_deg is not None:
            args += ["-spd", f"{hints.south_pole_distance_deg:.6f}"]
    else:
        args += ["-r", "60"]

    if database:
        args += ["-D", database]
    if write_wcs:
        args.append("-wcs")

    return args


def solve(
    image: Path,
    astap: Path,
    *,
    out_base: Path,
    hints: SolveHints | None = None,
    database: str | None = None,
    timeout_s: float = 120.0,
) -> AstapResult:
    """Run ASTAP and parse the result.

    Deletes any stale ``.ini`` first: ASTAP always writes one, so a leftover
    file would be indistinguishable from a fresh result.
    """
    ini_path = out_base.with_suffix(".ini")
    ini_path.unlink(missing_ok=True)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    args = build_args(image, out_base, hints=hints, database=database)

    try:
        completed = subprocess.run(
            [str(astap), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AstapResult(solved=False, error=f"timed out after {timeout_s:g}s")

    if not ini_path.is_file():
        return AstapResult(
            solved=False,
            error="astap produced no .ini",
            exit_code=completed.returncode,
        )

    result = parse_ini(ini_path)
    return AstapResult(
        solved=result.solved,
        solution=result.solution,
        error=result.error,
        warnings=result.warnings,
        cmdline=result.cmdline,
        exit_code=completed.returncode,
    )
