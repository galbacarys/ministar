"""Sub length: the trailing ceiling, floored to a shutter speed the camera has.

Cross-checked against brief 03 §1.3, which published the ceiling for this rig as
"50 mm -> 2-3 s, 135 mm -> ~0.8-1.6 s, 300 mm -> 0.4-0.7 s" for a 2 px trail budget.
"""

from __future__ import annotations

import math

import pytest

from ministar.camera.base import TV_SECONDS, snap_shutter_down
from ministar.exposure import TRAIL_BUDGET_PX, choose_exposure, max_sub_seconds
from ministar.optics import Optics


def optics_at(focal_mm: float, pixel_um: float = 4.0) -> Optics:
    """Brief 03's own assumption was 4.0 um pixels; the 60D is 4.3, so the default here is
    4.0 to make the cross-check an apples-to-apples one. The extra 7% of pitch genuinely
    buys a slightly longer sub, which is why the check would fail at 4.3."""
    return Optics.from_pitch(
        width_px=5202, height_px=3464, pixel_um=pixel_um, focal_length_mm=focal_mm
    )


# -- flooring to a legal Tv -------------------------------------------------
def test_snapping_down_never_exceeds_the_request() -> None:
    for seconds in (0.31, 0.5, 0.78, 1.24, 2.9, 3.34):
        snapped = snap_shutter_down(seconds)
        assert snapped <= seconds
        assert snapped in TV_SECONDS


def test_snapping_down_is_not_nearest() -> None:
    """The distinction matters: a ceiling that rounds up puts streaks in the frame."""
    assert snap_shutter_down(3.1) == 2.5
    assert snap_shutter_down(1.05) == 1.0


def test_a_request_below_the_fastest_shutter_lands_on_the_fastest() -> None:
    assert snap_shutter_down(1e-6) == TV_SECONDS[0]


def test_a_non_positive_request_is_an_error() -> None:
    with pytest.raises(ValueError):
        snap_shutter_down(0.0)


# -- the ceiling ------------------------------------------------------------
@pytest.mark.parametrize(
    ("focal", "dec", "low", "high"),
    [
        # Brief 03 §1.3, at a 2 px trail budget.
        (50, 45, 2.0, 3.0),
        (135, 45, 0.8, 1.6),
        (300, 45, 0.4, 0.7),
    ],
)
def test_the_ceiling_matches_the_published_ranges(
    focal: float, dec: float, low: float, high: float
) -> None:
    chosen = choose_exposure(optics_at(focal), dec)

    assert low <= chosen.seconds <= high
    assert chosen.trail_px <= TRAIL_BUDGET_PX + 0.05


def test_the_trail_at_the_chosen_shutter_is_within_budget() -> None:
    for focal in (50, 100, 200, 400):
        for dec in (0.0, 30.0, 60.0):
            chosen = choose_exposure(optics_at(focal), dec)
            assert chosen.trail_px <= TRAIL_BUDGET_PX


def test_near_the_pole_the_sky_stops_moving() -> None:
    assert math.isinf(max_sub_seconds(optics_at(200), 90.0))
    assert choose_exposure(optics_at(200), 89.99).seconds == pytest.approx(30.0)


def test_a_lower_trail_tolerance_shortens_the_sub() -> None:
    loose = choose_exposure(optics_at(200), 0.0, trail_px=2.0)
    strict = choose_exposure(optics_at(200), 0.0, trail_px=1.0)

    assert strict.ceiling_s == pytest.approx(loose.ceiling_s / 2)


def test_the_label_reads_like_a_shutter_speed() -> None:
    chosen = choose_exposure(optics_at(200), 41.3)

    assert chosen.label.endswith('"') or chosen.label.startswith("1/")
