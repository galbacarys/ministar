"""How long a single sub may be, before the sky streaks it.

The ceiling is trailing, not skyglow (03 §1.3): an untracked mount moves the sky at
``15.0411 * cos(dec)`` arcsec per second, so the only question is how many pixels of
trail are acceptable. Two pixels is the working budget -- past that stars stop being
rounds and start being dashes, and every stacker's centroid code starts rejecting them.

The answer is then floored to a legal Canon Tv value, because the camera cannot be asked
for "3.1 seconds". Flooring matters: the ceiling came from trailing, so rounding *up* to
the nearest Tv would put streaks in the frame to save a third of a stop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ministar.camera.base import label_for_seconds, snap_shutter_down
from ministar.optics import SIDEREAL_RATE_ARCSEC_PER_S, Optics

#: Pixels of intra-exposure trail we are willing to accept (03 §1.3).
TRAIL_BUDGET_PX = 2.0

#: Canon APS-C crop factor, which the 500-rule divides the focal length by.
APS_C_CROP = 1.6

#: NPF coefficients, as published by Frédéric Michaud (Société Astronomique du Havre):
#:
#:     t = (16.856 N + 0.0997 f + 13.713 p) / (f cos dec)
#:
#: with N the f-number, f the focal length in mm, p the pixel pitch in micrometres and dec
#: the declination. It is a heuristic for "the longest exposure before stars stop looking
#: like points", calibrated at a viewing size; the constants encode an acceptable trail of
#: roughly two pixels at a typical pixel pitch, which is why it lands on our budget.
NPF_APERTURE = 16.856
NPF_FOCAL = 0.0997
NPF_PIXEL = 13.713


@dataclass(frozen=True)
class Exposure:
    """A chosen sub length and the reasoning behind it."""

    #: Longest exposure the trailing budget allows, before snapping.
    ceiling_s: float
    #: What the camera will actually be asked for.
    seconds: float
    #: Trail at the snapped value, so the user can see the trade.
    trail_px: float

    @property
    def label(self) -> str:
        return label_for_seconds(self.seconds)


def max_sub_seconds(optics: Optics, dec_deg: float, *, trail_px: float = TRAIL_BUDGET_PX) -> float:
    """Longest exposure with no more than ``trail_px`` of trail. May be fractional."""
    rate = SIDEREAL_RATE_ARCSEC_PER_S * math.cos(math.radians(dec_deg))
    if rate <= 1e-9:
        # At the pole the sky does not move; the camera's own limit applies instead.
        return float("inf")
    return trail_px * optics.plate_scale_arcsec / rate


def choose_exposure(
    optics: Optics, dec_deg: float, *, trail_px: float = TRAIL_BUDGET_PX
) -> Exposure:
    """The longest legal Tv that stays inside the trailing budget."""
    ceiling = max_sub_seconds(optics, dec_deg, trail_px=trail_px)
    seconds = snap_shutter_down(ceiling) if ceiling != float("inf") else 30.0
    actual_trail = optics.drift_px(dec_deg=dec_deg, seconds=seconds)
    return Exposure(ceiling_s=ceiling, seconds=seconds, trail_px=actual_trail)


def pixel_pitch_um(optics: Optics) -> float:
    """Pixel pitch implied by the plate scale, ``p = scale * f / 206.265``.

    The rig keeps plate scale (measured) rather than pitch (often absent from EXIF), and the
    NPF rule is written in pitch, so the conversion has to happen somewhere explicit.
    """
    if not optics.focal_length_mm:
        raise ValueError("NPF needs the focal length")
    return optics.plate_scale_arcsec * optics.focal_length_mm / 206.265


def npf_seconds(optics: Optics, dec_deg: float, *, f_number: float) -> float:
    """The NPF rule's longest exposure, for comparison with the trail budget.

    Not used to choose the exposure -- the trail budget in pixels is the measurement, and
    NPF is a heuristic with a viewing-size constant baked in. It is here so the choice can
    be *shown*: for this rig at f/2.8 the two agree to a few percent, which is the answer to
    "the 500-rule lets me go longer".
    """
    focal = optics.focal_length_mm
    if not focal:
        raise ValueError("NPF needs the focal length")
    pitch = pixel_pitch_um(optics)
    denominator = focal * math.cos(math.radians(dec_deg))
    if abs(denominator) < 1e-9:
        return float("inf")
    return (NPF_APERTURE * f_number + NPF_FOCAL * focal + NPF_PIXEL * pitch) / abs(denominator)


def rule_of_500_seconds(optics: Optics, *, crop_factor: float = APS_C_CROP) -> float:
    """The 500-rule's longest exposure: ``500 / (focal length * crop factor)``.

    A print-size heuristic from the film era. It ignores pixel pitch, aperture and
    declination, which is exactly why it is generous on a modern sensor: for this rig it
    implies about 4.7 px of trail, more than twice the budget, and stars roughly 2.7x their
    at-focus size.
    """
    focal = optics.focal_length_mm
    if not focal:
        raise ValueError("the 500-rule needs the focal length")
    return 500.0 / (focal * crop_factor)


__all__ = [
    "APS_C_CROP",
    "TRAIL_BUDGET_PX",
    "Exposure",
    "choose_exposure",
    "max_sub_seconds",
    "npf_seconds",
    "pixel_pitch_um",
    "rule_of_500_seconds",
]
