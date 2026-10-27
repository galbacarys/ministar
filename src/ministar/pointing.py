"""Reading a plate solve back, and noticing when it describes a crop.

A solve is only evidence if it describes the frame we shot. ASTAP silently crops to fit the
star database's field limit (brief 01 §6: an 8.53 deg frame solved against D80 came back as a
6.33 deg sub-image), and then `CRVAL` describes the crop -- so a solution can look perfect
while hiding two thirds of the frame. Brief 07 §1 states the guard; this is that guard.

What survives a crop: plate scale and position angle (same pixels, same lens), and the frame
*centre*, because the crop is centred. What does not: the field of view, and therefore how
much sky the envelope was verified against. So a crop is a warning, never a failure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ministar.solver.astap import Solution

#: Above this fractional shortfall, the solution describes a different frame than the one
#: that was shot (brief 07 §1: "treat a mismatch > 10% as solution describes a crop").
CROP_TOLERANCE = 0.10


def solved_width_deg(solution: Solution, *, width_px: int) -> float:
    """Width of the solved frame, from the CD matrix: ``|CD column| * width``.

    Uses CD1_1/CD2_1 -- the first column -- which is the image +x axis. Never ``CROTA``:
    it is present, it is close, and it is a trap (brief 07 §1).
    """
    cd1_1, _, cd2_1, _ = solution.cd
    return math.hypot(cd1_1, cd2_1) * width_px


def crop_factor(
    solution: Solution, *, width_px: int, expected_width_deg: float | None
) -> float | None:
    """Solved width over expected width. ~1.0 is the whole frame; 0.36 is a 50 mm crop."""
    if not expected_width_deg:
        return None
    return solved_width_deg(solution, width_px=width_px) / expected_width_deg


@dataclass(frozen=True)
class SolveReport:
    """What to show after a solve: the facts, and the caveats that change their meaning."""

    facts: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def cropped(self) -> bool:
        return any("crop" in warning for warning in self.warnings)


def describe(
    solution: Solution,
    *,
    width_px: int,
    expected_width_deg: float | None = None,
) -> SolveReport:
    """One block of facts plus any warnings a user needs to interpret them."""
    ra_deg, dec_deg = solution.crval
    facts = [
        f"RA {ra_deg / 15.0:.4f} h   Dec {dec_deg:+.4f} deg",
        f"position angle {solution.position_angle_deg:.2f} deg   "
        f'scale {solution.pixel_scale_arcsec:.2f}"/px',
        f"parity {'mirrored' if solution.is_mirrored else 'normal'}",
    ]
    warnings: list[str] = []
    factor = crop_factor(solution, width_px=width_px, expected_width_deg=expected_width_deg)
    if factor is not None and factor < 1.0 - CROP_TOLERANCE:
        warnings.append(
            f"solved a crop: {factor * 100:.0f}% of the frame width "
            f"({solved_width_deg(solution, width_px=width_px):.2f} deg of "
            f"{expected_width_deg:.2f} deg) -- the star database is too narrow for this field"
        )
    if solution.is_mirrored:
        warnings.append("mirrored solution: every left/right instruction must be flipped (07 §2.1)")
    return SolveReport(facts=tuple(facts), warnings=tuple(warnings))


__all__ = [
    "CROP_TOLERANCE",
    "SolveReport",
    "crop_factor",
    "describe",
    "solved_width_deg",
]
