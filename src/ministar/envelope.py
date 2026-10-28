"""What survives a run: the sky every frame of the interval has in common.

The camera is bolted to the ground, so as the sky turns its footprint rotates **about the
celestial pole**. Over a run the content therefore drifts *and* turns, and the region common
to every frame is the intersection of the footprint with its own displaced and rotated copies:
a rectangle while rotation is negligible, an octagon once it is not.

Why this module exists. ``optics.retained_fraction`` models the run as a straight translation.
That is exact -- the intersection of two rectangles offset along the drift is a rectangle --
but it has no term for rotation, and near the pole the drift rate ``15.04 cos(dec)`` correctly
approaches zero while the frame keeps turning. The closed form then reports an intact frame
for a field that is rotating out of itself: measured at dec 89 over an hour, 97.3% against a
real 88.2% (brief 12).

The frame is taken to be north-up, i.e. the drift along the long side, which is the planning
assumption everywhere else (``optics.window_seconds``, brief 12 POINT).
"""

from __future__ import annotations

import math

import numpy as np

from ministar.projection import deproject, gnomonic

#: The sky turns this much per second: 15.0411 deg/hour, the same at every declination. The
#: *drift* rate is this times cos(dec); the *rotation* rate is not, which is the whole point.
SIDEREAL_DEG_PER_S = 15.0411 / 3600.0

#: Below this much rotation the closed form is within a percentage point -- at dec 41 a
#: 20-minute run turns 5 deg and the two agree to 0.8 of a point -- so it is preferred: exact
#: for translation, cheap, and it says what it means.
NEGLIGIBLE_ROTATION_DEG = 2.0

#: Samples of the run intersected. Two would do for translation; rotation is a smooth
#: monotone turn, so a handful is plenty and the cost stays in microseconds.
SAMPLES = 13

#: Where the pole's rotation is worth mentioning: at dec 75 the drift rate is down to a
#: quarter of sidereal, so a frame turned 22 deg during an hour.
POLE_DEC_DEG = 75.0

#: ...and an interval short enough barely turns at all needs no caveat.
POLE_NOTE_MIN_SECONDS = 900.0


def rotation_deg(seconds: float) -> float:
    """How far the field turns during ``seconds`` -- independent of declination."""
    return SIDEREAL_DEG_PER_S * max(0.0, float(seconds))


def rotation_matters(seconds: float) -> bool:
    """Is the closed form good enough for this interval?"""
    return rotation_deg(seconds) >= NEGLIGIBLE_ROTATION_DEG


def rotation_dominates(*, dec_deg: float, seconds: float) -> bool:
    """Does field rotation need a word next to the megapixel count?

    Not "is it non-zero" but "is it the thing happening": near the pole the frame turns faster
    than the field slides, and a number that drops unexpectedly deserves an explanation.
    """
    return abs(dec_deg) >= POLE_DEC_DEG and seconds >= POLE_NOTE_MIN_SECONDS


def _footprint(
    width_px: int, height_px: int, plate_scale_arcsec: float, dec_deg: float
) -> list[tuple[float, float]]:
    """Frame corners in sky coordinates: north-up, centred on the target."""
    hw = (width_px - 1) / 2.0 * plate_scale_arcsec / 3600.0
    hh = (height_px - 1) / 2.0 * plate_scale_arcsec / 3600.0
    corners = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ra, dec = deproject(
        0.0,
        dec_deg,
        [math.radians(x) for x, _ in corners],
        [math.radians(y) for _, y in corners],
    )
    return [(float(a), float(b)) for a, b in zip(ra, dec, strict=True)]


def _plane(corners, dec_deg: float) -> list[tuple[float, float]]:
    """Sky corners into the target-centred tangent plane, in arcsec (east, north)."""
    xi, eta = gnomonic(0.0, dec_deg, [c[0] for c in corners], [c[1] for c in corners])
    arcsec_per_rad = math.degrees(1.0) * 3600.0
    return [
        (float(a) * arcsec_per_rad, float(b) * arcsec_per_rad) for a, b in zip(xi, eta, strict=True)
    ]


def _area(poly) -> float:
    # Shoelace: the polygons here are convex and small, so nothing cleverer is needed.
    if len(poly) < 3:
        return 0.0
    x = np.array([p[0] for p in poly])
    y = np.array([p[1] for p in poly])
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _clip(subject, clipper) -> list[tuple[float, float]]:
    """Sutherland-Hodgman for convex polygons wound counter-clockwise."""
    if len(subject) < 3:
        return []

    def side(p, a, b):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])

    def meet(p1, p2, a, b):
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = a
        x4, y4 = b
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) < 1e-15:
            return p2
        d1 = x1 * y2 - y1 * x2
        d2 = x3 * y4 - y3 * x4
        return (
            (d1 * (x3 - x4) - (x1 - x2) * d2) / den,
            (d1 * (y3 - y4) - (y1 - y2) * d2) / den,
        )

    out = list(subject)
    for i in range(len(clipper)):
        a, b = clipper[i], clipper[(i + 1) % len(clipper)]
        inp, out = out, []
        if not inp:
            return []
        prev = inp[-1]
        for cur in inp:
            cur_side, prev_side = side(cur, a, b), side(prev, a, b)
            if cur_side >= 0:
                if prev_side < 0:
                    out.append(meet(prev, cur, a, b))
                out.append(cur)
            elif prev_side >= 0:
                out.append(meet(prev, cur, a, b))
            prev = cur
    return out


def kept_fraction(
    *,
    width_px: int,
    height_px: int,
    plate_scale_arcsec: float,
    dec_deg: float,
    seconds: float,
    samples: int = SAMPLES,
) -> float:
    """Fraction of the frame that every frame of a run of ``seconds`` has in common."""
    if seconds <= 0:
        return 1.0
    dec = max(-89.9, min(89.9, float(dec_deg)))
    base = _footprint(width_px, height_px, plate_scale_arcsec, dec)
    first = _plane(base, dec)
    whole = _area(first)
    if whole <= 0.0:
        return 0.0

    common = first
    turn = rotation_deg(seconds)
    for step in range(1, samples + 1):
        turned = [(ra + turn * step / samples, d) for ra, d in base]
        common = _clip(common, _plane(turned, dec))
        if len(common) < 3:
            return 0.0
    return max(0.0, min(1.0, _area(common) / whole))


__all__ = [
    "NEGLIGIBLE_ROTATION_DEG",
    "POLE_DEC_DEG",
    "POLE_NOTE_MIN_SECONDS",
    "SAMPLES",
    "SIDEREAL_DEG_PER_S",
    "kept_fraction",
    "rotation_deg",
    "rotation_dominates",
    "rotation_matters",
]
