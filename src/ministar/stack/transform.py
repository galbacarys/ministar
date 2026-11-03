"""Where the camera looks, and how its pixels move as the sky turns.

Brief 15 §1 is the whole argument for this module existing and for it being short: a
camera bolted to a tripod has a transform that is *known*, not estimated. No star
detection per frame, no astroalign, no per-frame failure mode. The only inputs are

* the one plate solve taken at POINT (centre, roll, plate scale), and
* the time each frame was captured.

The sky rotates about the pole at a fixed sidereal rate, so a star at pixel ``p`` at the
reference epoch is a fixed direction, and at ``t`` it is that direction rotated about the
celestial pole by ``Omega.Δt``. In terms of the reference frame's own projection ``C`` (a
:class:`~ministar.projection.TangentPlane`):

    pixels at t  =  C( rotate_pole( C⁻¹(pixels at t_ref), -Omega.Δt ) )

The negative sign is not arbitrary. A fixed pixel sees the sky's RA *increase* with
time, so the star that was there at ``t_ref`` has moved to the pixel whose sky is at
RA - Δ — westwards, which is what stars do. ``tests/test_stack_transform.py`` pins the
sign against the same drift direction the pointing advice uses, because a sign error
here is invisible until the stack smears.

⚠️ **A homography, not an affine.** The composition above is two central projections
around a rotation, which is projective. An affine is a 6-parameter approximation of an
8-parameter map: measured on this rig's full field (9.7° x 6.5° at 135 mm) the affine
residual reaches **6.5 px at the corners after 15 minutes**, which is 44 arcseconds of
smear — visible, and growing for the rest of the run. The same correspondences fitted
with a homography are exact to machine precision at every elapsed time tested, so the
extra two parameters are not a refinement; they are the difference between "the
transform is known" and "the transform is roughly known".

⚠️ **No site, no alt/az, no mount model.** Rotating about the celestial pole is a purely
equatorial operation, which is why a real camera — which reports no encoders — can use
this. It is the same physics brief 10 §3.2 derives from the horizontal frame; that
formulation needs altitude and azimuth, which only the fake has.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from ministar.optics import SIDEREAL_RATE_ARCSEC_PER_S
from ministar.projection import TangentPlane

#: Degrees of sky rotation per second of time. The sidereal day is 86164.0905 s.
SIDEREAL_RATE_DEG_PER_S = SIDEREAL_RATE_ARCSEC_PER_S / 3600.0

#: Points used to fit the homography. Corners, edge midpoints and the centre: enough to
#: constrain eight numbers with margin, and placed where a wrong rotation shows up.
_SAMPLE_FRACTIONS = (
    (0.0, 0.0),
    (0.5, 0.0),
    (1.0, 0.0),
    (0.0, 0.5),
    (0.5, 0.5),
    (1.0, 0.5),
    (0.0, 1.0),
    (0.5, 1.0),
    (1.0, 1.0),
)


def compose(outer: np.ndarray, inner: np.ndarray) -> np.ndarray:
    """The projective map ``outer ∘ inner``: two maps chained like functions."""
    return np.asarray(outer, dtype=float) @ np.asarray(inner, dtype=float)


def invert(matrix: np.ndarray) -> np.ndarray:
    """The inverse of a non-singular 3x3 homography."""
    return np.linalg.inv(np.asarray(matrix, dtype=float))


def apply(matrix: np.ndarray, x, y) -> tuple[np.ndarray, np.ndarray]:
    """Map points through a homography, with one division for the whole array."""
    matrix = np.asarray(matrix, dtype=float)
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    w = matrix[2, 0] * x + matrix[2, 1] * y + matrix[2, 2]
    return (
        (matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]) / w,
        (matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]) / w,
    )


def rotation_deg(matrix: np.ndarray, at_xy: tuple[float, float] = (0.0, 0.0)) -> float:
    """Field rotation of the map *at a point*, in degrees.

    Read from the local Jacobian, not from the top-left 2x2: a homography's linear block
    is scaled and sheared by its perspective row, which biases a global reading by ~1%
    across this rig's field. The local angle is also the honest quantity — it is what a
    star at that spot actually does — and it varies across the frame (brief 10 §3.2
    measures the centre's value 0.5% off ``Omega.sin dec``, which is the approximation's own
    documented error, not a bug).

    Callers should pass the frame centre, which is the number the HUD means by "field
    rotated".
    """
    x, y = at_xy
    step = 1.0
    x_plus, y_plus = apply(matrix, x + step, y)
    x_minus, y_minus = apply(matrix, x - step, y)
    return math.degrees(math.atan2(y_plus - y_minus, x_plus - x_minus))


def fit_homography(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Least-squares homography from four or more correspondences (DLT).

    Written out rather than calling ``cv2.findHomography`` for two reasons: no RANSAC
    threshold to tune on data that has no outliers (the correspondences are computed, not
    detected), and the fit's residual stays measurable, which is how the affine-vs-
    homography claim in the module docstring was checked.
    """
    rows: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(source, target, strict=True):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        values.append(u)
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        values.append(v)
    solution, *_ = np.linalg.lstsq(np.asarray(rows, dtype=float), np.asarray(values), rcond=None)
    return np.append(solution, 1.0).reshape(3, 3)


@dataclass(frozen=True)
class FieldModel:
    """A fixed camera, described by the solve that measured where it points.

    ``plate_scale_arcsec`` and the pixel dimensions are those of the *stacked* frame,
    not the sensor: the accumulator bins before it warps, so the model has to speak the
    binned grid (``for_binning``).
    """

    ra_deg: float
    dec_deg: float
    pa_y_deg: float
    plate_scale_arcsec: float
    width_px: int
    height_px: int
    #: The solve's timestamp. All drift is measured from here.
    epoch: datetime
    east_left: bool = True

    def __post_init__(self) -> None:
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("frame dimensions must be positive")
        if not self.plate_scale_arcsec > 0:
            raise ValueError("plate scale must be positive")

    @classmethod
    def for_binning(cls, model: FieldModel, bin_factor: int) -> FieldModel:
        """The same camera on a coarser grid: ``bin_factor`` sensor pixels per pixel.

        Binning is a scale change about the origin. The half-pixel offset that averaging
        introduces is constant and is absorbed by the reference grid, so it is ignored.
        """
        if bin_factor < 1:
            raise ValueError("bin factor must be at least 1")
        return cls(
            ra_deg=model.ra_deg,
            dec_deg=model.dec_deg,
            pa_y_deg=model.pa_y_deg,
            plate_scale_arcsec=model.plate_scale_arcsec * bin_factor,
            width_px=model.width_px // bin_factor,
            height_px=model.height_px // bin_factor,
            epoch=model.epoch,
            east_left=model.east_left,
        )

    @property
    def plane(self) -> TangentPlane:
        return TangentPlane(
            ra_deg=self.ra_deg,
            dec_deg=self.dec_deg,
            pa_y_deg=self.pa_y_deg,
            plate_scale_arcsec=self.plate_scale_arcsec,
            width_px=self.width_px,
            height_px=self.height_px,
            east_left=self.east_left,
        )

    def rotation_deg(self, when: datetime) -> float:
        """Sky rotation since the epoch. Westwards (decreasing RA) is negative."""
        return -SIDEREAL_RATE_DEG_PER_S * (when - self.epoch).total_seconds()

    def sample_pixels(self) -> np.ndarray:
        """The correspondences used to fit the map: 9 points across the frame."""
        w, h = self.width_px - 1, self.height_px - 1
        return np.array([(w * fx, h * fy) for fx, fy in _SAMPLE_FRACTIONS], dtype=float)

    def map_points(self, x, y, when: datetime) -> tuple[np.ndarray, np.ndarray]:
        """Where epoch pixels land at ``when``: the exact composition, no fit."""
        ra, dec = self.plane.deproject_pixels(x, y)
        return self.plane.project((ra + self.rotation_deg(when)) % 360.0, dec)

    def homography(self, when: datetime) -> np.ndarray:
        """3x3 map from epoch pixels to ``when`` pixels."""
        source = self.sample_pixels()
        target_x, target_y = self.map_points(source[:, 0], source[:, 1], when)
        return fit_homography(source, np.column_stack([target_x, target_y]))

    def residual_px(self, when: datetime) -> float:
        """RMS distance between the fitted map and the exact one, in pixels.

        Should be at machine precision; it is reported rather than assumed so a broken
        fit fails a test instead of quietly smearing a night's frames.
        """
        source = self.sample_pixels()
        exact_x, exact_y = self.map_points(source[:, 0], source[:, 1], when)
        fit_x, fit_y = apply(self.homography(when), source[:, 0], source[:, 1])
        return float(np.sqrt(np.mean((exact_x - fit_x) ** 2 + (exact_y - fit_y) ** 2)))

    def relative_homography(self, reference: datetime, when: datetime) -> np.ndarray:
        """3x3 map from ``reference`` pixels to ``when`` pixels.

        Both maps are fitted against the *same* solve epoch, so the composition cancels
        it. The first run frame becomes the accumulator's canvas: the map at its own time
        is the identity, and every later frame is warped by this.
        """
        return compose(self.homography(when), invert(self.homography(reference)))


__all__ = [
    "SIDEREAL_RATE_DEG_PER_S",
    "FieldModel",
    "apply",
    "compose",
    "fit_homography",
    "invert",
    "rotation_deg",
]
