"""Sky coordinates to pixels, so a synthetic frame can be solvable.

The fake telescope has to hand the solver something real: random stars never solve, and a
frame that cannot be solved cannot exercise POINT, the envelope, or the reframe arrows. So
the fake renders stars from ASTAP's own catalogue through a tangent-plane (gnomonic)
projection -- the same model ASTAP fits -- and the solver recovers what we put in.

Conventions, stated because every one of them is a place to get the sign wrong:

* ``centre`` is the sky position at the **frame centre**, which is what ``CRVAL`` reports.
* ``pa_y`` is the position angle of the image's +y axis, measured north through east --
  i.e. the same quantity as ``Solution.position_angle_deg``.
* ``east_left`` is the astronomical convention (north up, east left, ``CD`` determinant
  negative). It flips the horizontal axis only; the vertical axis is unaffected.
* Pixels are 0-based ``(x, y)`` with ``y`` increasing **down**, as arrays index them, so the
  centre of a 4-pixel-wide frame is 1.5.

The projection is standard: with ``d = sin d sin d0 + cos d cos d0 cos(da)``,
``xi = cos d sin(da) / d`` (east) and ``eta = (sin d cos d0 - cos d sin d0 cos(da)) / d``
(north), both in radians, valid while the field is small enough that ``d > 0``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def gnomonic(ra0_deg: float, dec0_deg: float, ra_deg, dec_deg) -> tuple[np.ndarray, np.ndarray]:
    """Tangent-plane offsets ``(xi, eta)`` in RADIANS, east and north.

    Exact -- no small-angle approximation -- because at 50 mm the frame is 26 deg across and
    the same formula has to serve the whole range. Both the pixel projection and the
    pointing advice use it, so they cannot disagree about where a star is.
    """
    ra = np.radians(np.asarray(ra_deg, dtype=float))
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    ra0 = math.radians(ra0_deg)
    dec0 = math.radians(dec0_deg)
    delta_ra = ra - ra0
    d = math.sin(dec0) * np.sin(dec) + math.cos(dec0) * np.cos(dec) * math.cos(delta_ra)
    xi = np.cos(dec) * np.sin(delta_ra) / d
    eta = (np.sin(dec) * math.cos(dec0) - np.cos(dec) * math.sin(dec0) * np.cos(delta_ra)) / d
    return xi, eta


@dataclass(frozen=True)
class TangentPlane:
    """A frame's geometry: where it points, how it is rolled, and its scale."""

    ra_deg: float
    dec_deg: float
    pa_y_deg: float
    plate_scale_arcsec: float
    width_px: int
    height_px: int
    east_left: bool = True

    @property
    def centre_px(self) -> tuple[float, float]:
        return ((self.width_px - 1) / 2.0, (self.height_px - 1) / 2.0)

    def project(self, ra_deg: np.ndarray, dec_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Sky positions to 0-based pixel coordinates ``(x, y)``."""
        ra = np.radians(np.asarray(ra_deg, dtype=float))
        dec = np.radians(np.asarray(dec_deg, dtype=float))
        ra0 = math.radians(self.ra_deg)
        dec0 = math.radians(self.dec_deg)
        delta_ra = ra - ra0

        d = math.sin(dec0) * np.sin(dec) + math.cos(dec0) * np.cos(dec) * np.cos(delta_ra)
        xi = np.cos(dec) * np.sin(delta_ra) / d
        eta = (np.sin(dec) * math.cos(dec0) - np.cos(dec) * math.sin(dec0) * np.cos(delta_ra)) / d

        pa = math.radians(self.pa_y_deg)
        # Rotate the sky offset into the image axes: +y is at pa_y, +x is 90 deg from it.
        x_along = xi * math.cos(pa) - eta * math.sin(pa)
        y_along = xi * math.sin(pa) + eta * math.cos(pa)
        if self.east_left:
            x_along = -x_along

        scale = math.radians(self.plate_scale_arcsec / 3600.0)
        centre_x, centre_y = self.centre_px
        return centre_x + x_along / scale, centre_y - y_along / scale

    def inside(self, x: np.ndarray, y: np.ndarray, margin_px: float = 0.0) -> np.ndarray:
        return (
            (x >= margin_px)
            & (x <= self.width_px - 1 - margin_px)
            & (y >= margin_px)
            & (y <= self.height_px - 1 - margin_px)
        )


def stars_in_frame(
    ra_deg: np.ndarray,
    dec_deg: np.ndarray,
    mag: np.ndarray,
    plane: TangentPlane,
    *,
    margin_px: float = 0.0,
):
    """Positions and magnitudes of the catalogue stars that land in this frame."""
    x, y = plane.project(ra_deg, dec_deg)
    keep = plane.inside(x, y, margin_px)
    return x[keep], y[keep], np.asarray(mag, dtype=float)[keep]


__all__ = ["TangentPlane", "gnomonic", "stars_in_frame"]
