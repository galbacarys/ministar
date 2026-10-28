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

import numpy as np

from ministar.projection import TangentPlane, gnomonic
from ministar.solver.astap import BLIND_RADIUS_DEG, Solution, SolveHints

#: Past this radius a hinted search is no longer buying anything: it costs what a blind solve
#: costs and still only tells us the pointing is somewhere in a huge circle. Go blind instead.
HINT_MAX_USEFUL_RADIUS_DEG = 20.0

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


#: The smallest hint worth giving: cheaper than blind, wide enough for a mount bumped by a
#: hand rather than carried off the target.
HINT_FLOOR_RADIUS_DEG = 1.0


def drift_since_deg(elapsed_s: float, dec_deg: float) -> float:
    """How far the sky has moved during ``elapsed_s``, as an angle on the sky."""
    from ministar.optics import SIDEREAL_RATE_ARCSEC_PER_S

    return (
        SIDEREAL_RATE_ARCSEC_PER_S
        / 3600.0
        * max(0.0, float(elapsed_s))
        * math.cos(math.radians(dec_deg))
    )


def solve_hints(
    *,
    fov_height_deg: float,
    previous: tuple[float, float] | None = None,
    elapsed_s: float = 0.0,
    dec_deg: float | None = None,
) -> SolveHints:
    """Hints for one solve: always the field, and a position only if we have measured one.

    ``previous`` is a *solution's* RA/Dec, never the target's: before the first solve we do
    not know where the camera is looking, and a blind search with an FOV hint is both correct
    and affordable.

    Given a previous solution *and* how long ago it was, the search moves with the sky. Two
    things can have happened since: an untracked mount has left the frame showing sky further
    east, or a tracking mount has held it still. We cannot tell which from one solve, so the
    circle is centred halfway between the two and is wide enough to contain both -- half the
    drift either side, plus a margin for a mount nudged by hand.

    East, not west, and the distinction is worth being careful about: the *stars* march west
    across the frame, while the frame's own sky position advances east at the same rate --
    ``RA = LST - HA``, and a fixed mount holds ``HA``. Aiming the hint the wrong way is a
    silent failure of exactly the kind this function exists to prevent.
    """
    if previous is None:
        return SolveHints(fov_height_deg=fov_height_deg, radius_deg=BLIND_RADIUS_DEG)

    dec = previous[1] if dec_deg is None else float(dec_deg)
    drift = drift_since_deg(elapsed_s, dec)
    radius = HINT_FLOOR_RADIUS_DEG + drift / 2.0
    if radius > HINT_MAX_USEFUL_RADIUS_DEG:
        # Gone far enough that the hint is the whole sky: say so plainly.
        return SolveHints(fov_height_deg=fov_height_deg, radius_deg=BLIND_RADIUS_DEG)

    # Halfway east of where it was: RA compresses by cos(dec), which is why the shift is
    # expressed as an angle first and divided back out here.
    shift_ra_deg = drift / 2.0 / max(0.05, math.cos(math.radians(dec)))
    return SolveHints(
        fov_height_deg=fov_height_deg,
        ra_hours=((previous[0] + shift_ra_deg) % 360.0) / 15.0,
        south_pole_distance_deg=dec + 90.0,
        radius_deg=radius,
    )


def keep_capture(*, solved: bool, converted_is_raw: bool) -> bool:
    """Should the pointing frame survive the solve?

    A POINT capture is a check, not an imaging frame, so the camera file never stays: it is
    20 MB of RAW that nothing will read again, and the RUN loop keeps the frames that matter.
    The *converted* file is kept only when the solve failed, because that frame is then the
    only evidence of why -- and a failed solve is rare. The ``.ini`` sidecar always stays; it
    is 254 bytes and quotes the solver's own verdict.
    """
    if converted_is_raw:
        return False  # the fake: regenerable from the catalogue, never evidence
    return not solved


#: How close to an edge the target may start: a fraction of the short side, so a mount that
#: settles a little still begins with the object fully in frame.
EDGE_MARGIN_FRACTION = 0.02


def _frame(optics, *, ra_deg: float, dec_deg: float, pa_y_deg: float) -> TangentPlane:
    return TangentPlane(
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        pa_y_deg=pa_y_deg,
        plate_scale_arcsec=optics.plate_scale_arcsec,
        width_px=optics.width_px,
        height_px=optics.height_px,
    )


def drift_direction_px(plane: TangentPlane, ra_deg: float, dec_deg: float) -> tuple[float, float]:
    """Unit vector of the target's motion *in the frame*: untracked stars move west.

    Measured numerically rather than derived, so it inherits the projection's own conventions
    (roll, parity) instead of duplicating them -- and cannot disagree with the preview.
    """
    step_deg = 0.05 / max(0.05, math.cos(math.radians(dec_deg)))
    x0, y0 = plane.project(np.array([ra_deg]), np.array([dec_deg]))
    x1, y1 = plane.project(np.array([ra_deg - step_deg]), np.array([dec_deg]))
    dx, dy = float(x1[0] - x0[0]), float(y1[0] - y0[0])
    norm = math.hypot(dx, dy) or 1.0
    return dx / norm, dy / norm


def _chord_px(ux: float, uy: float, width_px: int, height_px: int) -> float:
    """Path length of the drift across the frame: the run a target can actually have.

    The chord, not the projection: a ray crosses whichever edge it reaches first, so a
    diagonal drift is limited by the short side and gives a *shorter* run than an aligned
    one (5183 px -> 3455 px between the long and short axis, the 1.5x of 10 §2.1c). Getting
    this wrong is invisible at roll 0 and 90 deg, where both formulas agree -- and wrong by
    several hundred pixels at every angle in between.
    """
    lengths = []
    if abs(ux) > 1e-12:
        lengths.append((width_px - 1) / abs(ux))
    if abs(uy) > 1e-12:
        lengths.append((height_px - 1) / abs(uy))
    return min(lengths) if lengths else 0.0


def _edge_travel_px(
    px: float, py: float, ux: float, uy: float, width_px: int, height_px: int
) -> float:
    """Distance from a point inside the frame to the edge the drift leads to."""
    hits = []
    if ux > 1e-12:
        hits.append((width_px - 1 - px) / ux)
    elif ux < -1e-12:
        hits.append(-px / ux)
    if uy > 1e-12:
        hits.append((height_px - 1 - py) / uy)
    elif uy < -1e-12:
        hits.append(-py / uy)
    return max(0.0, min(hits)) if hits else 0.0


def _west_of(ra_deg: float, dec_deg: float, arcmin: float) -> tuple[float, float]:
    """A point ``arcmin`` west (decreasing RA) along the parallel."""
    cosd = max(0.05, math.cos(math.radians(dec_deg)))
    return ((ra_deg - arcmin / 60.0 / cosd) % 360.0, dec_deg)


def _project_px(
    optics, *, ra_deg: float, dec_deg: float, pa_y_deg: float, ra: float, dec: float
) -> tuple[float, float]:
    plane = _frame(optics, ra_deg=ra_deg, dec_deg=dec_deg, pa_y_deg=pa_y_deg)
    x, y = plane.project(np.array([ra]), np.array([dec]))
    return float(x[0]), float(y[0])


def best_start(
    *,
    target_ra_deg: float,
    target_dec_deg: float,
    optics,
    pa_y_deg: float,
    target_radius_arcmin: float = 0.0,
    margin_fraction: float = EDGE_MARGIN_FRACTION,
) -> tuple[float, float]:
    """Where to point so the target begins the session at the edge it entered from.

    Aiming *at* the target throws away half the frame: it then has only the half-width ahead
    of it before it leaves. Starting it against the entry edge gives it the whole run -- 2x
    for the same pointing, which is what the roll advice is protecting.

    Solved in *pixels*, by Newton, rather than as a fixed RA offset: a great circle is not a
    parallel, and at 5 deg the two differ by 10 arcmin, which is a whole mount nudge.
    """
    plane = _frame(optics, ra_deg=target_ra_deg, dec_deg=target_dec_deg, pa_y_deg=pa_y_deg)
    ux, uy = drift_direction_px(plane, target_ra_deg, target_dec_deg)
    margin_px = margin_fraction * min(optics.width_px, optics.height_px)
    radius_px = target_radius_arcmin * 60.0 / optics.plate_scale_arcsec
    # 2 px of slack: the entry point is defined as "just inside the margin", and an exact
    # boundary is a coin toss in floating point -- the page would then say "NOT in frame".
    # The margin is a *box* inset, so the distance it allows along a diagonal is not
    # (margin subtracted from the chord): the target must clear both axes, which at 20 deg
    # of roll is 2 px tighter than the chord formula predicts -- enough to be outside.
    inset = margin_px + radius_px + 2.0
    inner_w = max(1, round(optics.width_px - 2.0 * inset))
    inner_h = max(1, round(optics.height_px - 2.0 * inset))
    behind_px = _chord_px(ux, uy, inner_w, inner_h) / 2.0

    centre_x, centre_y = plane.centre_px
    want_x = centre_x - behind_px * ux
    want_y = centre_y - behind_px * uy

    # First guess: straight west along the parallel. Then Newton to put the target on the
    # requested pixel, using the projection itself for the Jacobian so no convention is
    # restated here (roll, parity and curvature all come out of the same code).
    ra, dec = _west_of(target_ra_deg, target_dec_deg, behind_px * optics.plate_scale_arcsec / 60.0)
    # Damped: with the drift on the diagonal the first guess is degrees out, and a full
    # Newton step from there overshoots into a worse linearisation. Half a degree per step
    # converges in a couple of dozen cheap single-point projections.
    h = 1e-4
    for _ in range(60):
        ax, ay = _project_px(
            optics,
            ra_deg=ra,
            dec_deg=dec,
            pa_y_deg=pa_y_deg,
            ra=target_ra_deg,
            dec=target_dec_deg,
        )
        err_x, err_y = want_x - ax, want_y - ay
        if math.hypot(err_x, err_y) < 0.5:
            break
        bx, by = _project_px(
            optics,
            ra_deg=ra + h,
            dec_deg=dec,
            pa_y_deg=pa_y_deg,
            ra=target_ra_deg,
            dec=target_dec_deg,
        )
        cx, cy = _project_px(
            optics,
            ra_deg=ra,
            dec_deg=dec + h,
            pa_y_deg=pa_y_deg,
            ra=target_ra_deg,
            dec=target_dec_deg,
        )
        j11, j21 = (bx - ax) / h, (by - ay) / h
        j12, j22 = (cx - ax) / h, (cy - ay) / h
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-12:
            break
        step_ra = (err_x * j22 - err_y * j12) / det
        step_dec = (j11 * err_y - j21 * err_x) / det
        ra = (ra + max(-0.5, min(0.5, step_ra))) % 360.0
        dec = max(-89.9, min(89.9, dec + max(-0.5, min(0.5, step_dec))))
    return ra, dec


def _amount(arcmin: float) -> str:
    return f"{arcmin / 60.0:.2f} deg" if arcmin >= 60.0 else f"{arcmin:.0f}'"


@dataclass(frozen=True)
class FrameAdvice:
    """Where the target is in the frame, and what to do about it."""

    separation_arcmin: float
    east_arcmin: float
    north_arcmin: float
    inside: bool
    travel_arcmin: float
    best_travel_arcmin: float
    move_east_arcmin: float
    move_north_arcmin: float
    minutes_now: float
    minutes_best: float
    #: Half the object's long axis. Zero for a point source. When it is non-zero, "in frame"
    #: means the whole object, which is why aiming and reporting must both know it.
    radius_arcmin: float = 0.0

    @property
    def at_best_start(self) -> bool:
        return math.hypot(self.move_east_arcmin, self.move_north_arcmin) < 1.0

    def describe(self) -> tuple[str, ...]:
        lines = []
        where = (
            f"{_amount(abs(self.east_arcmin))} {'east' if self.east_arcmin >= 0 else 'west'}, "
            f"{_amount(abs(self.north_arcmin))} {'north' if self.north_arcmin >= 0 else 'south'}"
        )
        if self.inside:
            lines.append(f"target {_amount(self.separation_arcmin)} from the centre ({where})")
        else:
            extent = (
                f" -- it is {2.0 * self.radius_arcmin / 60:.1f} deg across, so all of it needs "
                f"{_amount(self.radius_arcmin)} of clearance"
                if self.radius_arcmin > 0
                else ""
            )
            lines.append(
                f"target {'NOT fully' if self.radius_arcmin > 0 else 'NOT'} in the frame: "
                f"{where} of centre{extent}"
            )
        lines.append(
            f"drift ahead {self.minutes_now:.1f} min "
            f"-- the best start gives {self.minutes_best:.1f} min"
        )
        move = math.hypot(self.move_east_arcmin, self.move_north_arcmin)
        if self.at_best_start:
            lines.append("pointing is at the best start")
        else:
            parts = []
            east = self.move_east_arcmin
            north = self.move_north_arcmin
            if abs(east) >= 1.0:
                parts.append(f"{_amount(abs(east))} {'east' if east > 0 else 'west'}")
            if abs(north) >= 1.0:
                parts.append(f"{_amount(abs(north))} {'north' if north > 0 else 'south'}")
            lines.append(
                f"move the mount {' and '.join(parts)}"
                if parts
                else f"move the mount {_amount(move)}"
            )
        return tuple(lines)


def frame_advice(
    *,
    ra_deg: float,
    dec_deg: float,
    target_ra_deg: float,
    target_dec_deg: float,
    optics,
    pa_y_deg: float,
    target_radius_arcmin: float = 0.0,
    margin_fraction: float = EDGE_MARGIN_FRACTION,
) -> FrameAdvice:
    """Everything the POINT page can say once it knows where the camera actually is."""
    plane = _frame(optics, ra_deg=ra_deg, dec_deg=dec_deg, pa_y_deg=pa_y_deg)
    xi, eta = gnomonic(ra_deg, dec_deg, target_ra_deg, target_dec_deg)
    east = float(np.degrees(xi)) * 60.0
    north = float(np.degrees(eta)) * 60.0

    x, y = plane.project(np.array([target_ra_deg]), np.array([target_dec_deg]))
    px, py = float(x[0]), float(y[0])
    margin_px = margin_fraction * min(optics.width_px, optics.height_px)
    radius_px = target_radius_arcmin * 60.0 / optics.plate_scale_arcsec
    inside = bool(plane.inside(np.array([px]), np.array([py]), margin_px + radius_px)[0])

    ux, uy = drift_direction_px(plane, target_ra_deg, target_dec_deg)
    arcmin_per_px = optics.plate_scale_arcsec / 60.0
    travel_px = max(
        0.0,
        (_edge_travel_px(px, py, ux, uy, optics.width_px, optics.height_px) if inside else 0.0)
        - margin_px
        - radius_px,
    )

    entry_ra, entry_dec = best_start(
        target_ra_deg=target_ra_deg,
        target_dec_deg=target_dec_deg,
        optics=optics,
        pa_y_deg=pa_y_deg,
        target_radius_arcmin=target_radius_arcmin,
        margin_fraction=margin_fraction,
    )
    # What a run looks like from the best start is measured there, not assumed: the entry
    # point puts the target behind the centre, so its travel is the half-extent ahead plus
    # the offset it starts with.
    entry_x, entry_y = _project_px(
        optics,
        ra_deg=entry_ra,
        dec_deg=entry_dec,
        pa_y_deg=pa_y_deg,
        ra=target_ra_deg,
        dec=target_dec_deg,
    )
    best_px = max(
        0.0,
        _edge_travel_px(entry_x, entry_y, ux, uy, optics.width_px, optics.height_px)
        - margin_px
        - radius_px,
    )

    delta_ra = (entry_ra - ra_deg + 180.0) % 360.0 - 180.0
    move_east = delta_ra * math.cos(math.radians(dec_deg)) * 60.0
    move_north = (entry_dec - dec_deg) * 60.0

    per_min = optics.drift_arcsec_per_s(dec_deg) / 60.0 * 60.0
    return FrameAdvice(
        separation_arcmin=math.hypot(east, north),
        east_arcmin=east,
        north_arcmin=north,
        inside=inside,
        travel_arcmin=travel_px * arcmin_per_px,
        best_travel_arcmin=best_px * arcmin_per_px,
        move_east_arcmin=move_east,
        move_north_arcmin=move_north,
        minutes_now=travel_px * arcmin_per_px / per_min if per_min else 0.0,
        minutes_best=best_px * arcmin_per_px / per_min if per_min else 0.0,
        radius_arcmin=float(target_radius_arcmin),
    )


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
        f"position angle {solution.position_angle_deg % 360.0:.2f} deg   "
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
    "BLIND_RADIUS_DEG",
    "EDGE_MARGIN_FRACTION",
    "HINT_FLOOR_RADIUS_DEG",
    "HINT_MAX_USEFUL_RADIUS_DEG",
    "MIN_USEFUL_FRACTION",
    "FrameAdvice",
    "SolveReport",
    "best_start",
    "crop_factor",
    "describe",
    "drift_direction_px",
    "drift_since_deg",
    "frame_advice",
    "keep_capture",
    "solve_hints",
    "solved_width_deg",
]
