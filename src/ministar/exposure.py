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

from dataclasses import dataclass

from ministar.camera.base import label_for_seconds, snap_shutter_down
from ministar.optics import SIDEREAL_RATE_ARCSEC_PER_S, Optics

#: Pixels of intra-exposure trail we are willing to accept (03 §1.3).
TRAIL_BUDGET_PX = 2.0


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
    rate = SIDEREAL_RATE_ARCSEC_PER_S * max(0.0, abs(dec_deg))
    import math

    rate = SIDEREAL_RATE_ARCSEC_PER_S * math.cos(math.radians(dec_deg))
    if rate <= 1e-9:
        # At the pole the sky does not move; the camera's own limit applies instead.
        return float("inf")
    return trail_px * optics.plate_scale_arcsec / abs(rate)


def choose_exposure(
    optics: Optics, dec_deg: float, *, trail_px: float = TRAIL_BUDGET_PX
) -> Exposure:
    """The longest legal Tv that stays inside the trailing budget."""
    ceiling = max_sub_seconds(optics, dec_deg, trail_px=trail_px)
    seconds = snap_shutter_down(ceiling) if ceiling != float("inf") else 30.0
    actual_trail = optics.drift_px(dec_deg=dec_deg, seconds=seconds)
    return Exposure(ceiling_s=ceiling, seconds=seconds, trail_px=actual_trail)


__all__ = ["TRAIL_BUDGET_PX", "Exposure", "choose_exposure", "max_sub_seconds"]
