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

#: How much of the frame a solve has to cover to be worth having: a quarter of it.
#:
#: A centred crop keeps the frame centre (CRVAL) and the plate scale and roll (CD matrix)
#: exact, so a cropped solve is genuinely usable -- the only thing that shrinks is how much
#: of the *edges* the fit saw. Hence the bar is low: if ASTAP solved the middle quarter, the
#: numbers we take from it are trustworthy. Below that the field is so wide relative to the
#: star database that a wider database is the real answer (against the observed 6.33 deg
#: ceiling, a quarter of the frame is a field up to ~25 deg tall -- so 35 mm and longer are
#: fine, and an ultra-wide wants W08).
MIN_USEFUL_FRACTION = 0.25


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
    """What to show after a solve: the facts, what they do not cover, and what to do about it.

    ``notes`` are informational -- the solve is fine, it just did not see everything.
    ``warnings`` mean act: change a setting, or a left/right instruction will be wrong.
    """

    facts: tuple[str, ...]
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Fraction of the frame width the solution actually covered, when that is known.
    solved_fraction: float | None = None

    @property
    def cropped(self) -> bool:
        return self.solved_fraction is not None and self.solved_fraction < 1.0 - 1e-6

    @property
    def usable(self) -> bool:
        """True when enough of the frame was solved to be worth acting on."""
        return self.solved_fraction is None or self.solved_fraction >= MIN_USEFUL_FRACTION


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
    notes: list[str] = []
    warnings: list[str] = []
    factor = crop_factor(solution, width_px=width_px, expected_width_deg=expected_width_deg)
    if factor is not None and factor < 1.0 - 1e-6:
        covered = (
            f"solved the centre {factor * 100:.0f}% of the frame "
            f"({solved_width_deg(solution, width_px=width_px):.2f} deg of "
            f"{expected_width_deg:.2f} deg)"
        )
        if factor < MIN_USEFUL_FRACTION:
            warnings.append(
                f"{covered} -- less than a quarter of the frame in the fit; "
                "install a wider star database (G05 covers 9.5 deg, W08 the whole sky)"
            )
        else:
            # A centred crop keeps CRVAL (the centre) and the CD matrix (scale, roll) exact,
            # so pointing and reframing still work from it. Only the edges are unverified.
            notes.append(f"{covered} -- centre and roll are good, frame edges unverified")
    if solution.is_mirrored:
        warnings.append("mirrored solution: every left/right instruction must be flipped (07 §2.1)")
    return SolveReport(
        facts=tuple(facts), notes=tuple(notes), warnings=tuple(warnings), solved_fraction=factor
    )


__all__ = [
    "MIN_USEFUL_FRACTION",
    "SolveReport",
    "crop_factor",
    "describe",
    "solved_width_deg",
]
