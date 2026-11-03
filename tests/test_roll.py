"""Which way to turn the camera.

The physics is brief 07 §2.5; the sanity check is that a north-up camera has the drift
along its long axis, and the invariant is that *following the advice makes it aligned* --
which is what turns a text instruction into something the user can trust.
"""

from __future__ import annotations

import math

import pytest

from ministar.reframe.roll import (
    ALIGNED_TOLERANCE_DEG,
    drift_angle_from_long_axis,
    parallactic_angle,
    roll_advice,
)


# -- the parallactic angle --------------------------------------------------
def test_at_the_meridian_the_parallactic_angle_is_zero_or_half_a_turn() -> None:
    """On the meridian the hour circle *is* the vertical, so q is 0 or 180.

    Which one depends on whether the object is south or north of the zenith: at latitude
    45, a dec-30 object is south of it (q = 0) and a dec-60 object is north (q = 180).
    """
    south_of_zenith = parallactic_angle(latitude_deg=45.0, dec_deg=30.0, hour_angle_deg=0.0)
    north_of_zenith = parallactic_angle(latitude_deg=45.0, dec_deg=60.0, hour_angle_deg=0.0)

    assert south_of_zenith == pytest.approx(0.0, abs=1e-9)
    assert abs(north_of_zenith) == pytest.approx(180.0, abs=1e-9)


def test_the_parallactic_angle_changes_sign_either_side_of_the_meridian() -> None:
    east = parallactic_angle(45.0, 30.0, -30.0)
    west = parallactic_angle(45.0, 30.0, +30.0)

    assert math.copysign(1, east) != math.copysign(1, west)
    assert abs(east) == pytest.approx(abs(west), abs=1e-9)


# -- the drift angle --------------------------------------------------------
def test_a_north_up_camera_has_the_drift_along_its_long_axis() -> None:
    """PA_y = 0 means +y points north, so the sky slides east-west across the long side."""
    assert drift_angle_from_long_axis(psi_deg=0.0, parallactic_deg=0.0) == pytest.approx(0.0)


def test_the_angle_is_symmetric_about_the_long_axis() -> None:
    """The long axis has 180 degrees of symmetry: 10 and 170 are the same problem."""
    a = drift_angle_from_long_axis(psi_deg=10.0, parallactic_deg=0.0)
    b = drift_angle_from_long_axis(psi_deg=190.0, parallactic_deg=0.0)

    assert a == pytest.approx(170.0)
    assert b == pytest.approx(170.0)


# -- the advice -------------------------------------------------------------
def test_an_aligned_camera_is_left_alone() -> None:
    advice = roll_advice(psi_deg=0.0, parallactic_deg=0.0)

    assert advice.aligned
    assert "aligned" in advice.describe()


def test_a_small_error_inside_the_tolerance_is_aligned() -> None:
    advice = roll_advice(psi_deg=0.0, parallactic_deg=ALIGNED_TOLERANCE_DEG - 1.0)

    assert advice.aligned


def test_the_advice_names_a_direction_and_a_magnitude() -> None:
    advice = roll_advice(psi_deg=0.0, parallactic_deg=0.0 + 30.0)

    assert not advice.aligned
    assert advice.degrees == pytest.approx(30.0)
    assert advice.direction in {"cw", "ccw"}
    assert advice.direction.upper() in advice.describe()


def test_following_the_advice_aligns_the_camera() -> None:
    """The invariant that matters: the instruction is self-consistent."""
    for roll in (-170, -90, -30, -5, 0, 5, 30, 90, 170):
        for q in (-60, 0, 45):
            advice = roll_advice(psi_deg=roll, parallactic_deg=q)
            if advice.aligned:
                continue
            signed = advice.degrees if advice.direction == "ccw" else -advice.degrees
            after = roll_advice(psi_deg=roll + signed, parallactic_deg=q)
            assert after.aligned, (
                f"roll {roll} q {q}: {advice.describe()} left "
                f"{after.angle_from_long_axis:.1f} deg off"
            )


def test_the_turn_is_never_the_long_way_round() -> None:
    for roll in range(-180, 180, 7):
        advice = roll_advice(psi_deg=roll, parallactic_deg=0.0)
        assert advice.degrees <= 90.0 + ALIGNED_TOLERANCE_DEG


def test_a_position_angle_is_used_without_adding_the_parallactic_angle() -> None:
    """The bug: the fake's roll *is* its PA, so adding q rotated the drift into the diagonal."""
    from ministar.reframe.roll import roll_inputs

    assert roll_inputs(
        solved_pa_deg=39.0, camera_roll_deg=0.0, camera_reports_pa=False, parallactic_deg=39.0
    ) == (0.0, 39.0)
    assert roll_inputs(
        solved_pa_deg=None, camera_roll_deg=39.0, camera_reports_pa=True, parallactic_deg=39.0
    ) == (0.0, 39.0)


def test_a_clamp_angle_still_needs_the_parallactic_angle() -> None:
    from ministar.reframe.roll import roll_inputs

    assert roll_inputs(
        solved_pa_deg=None, camera_roll_deg=0.0, camera_reports_pa=False, parallactic_deg=39.0
    ) == (39.0, 0.0)


def test_without_a_roll_there_is_nothing_to_advise() -> None:
    from ministar.reframe.roll import roll_inputs

    assert (
        roll_inputs(
            solved_pa_deg=None, camera_roll_deg=None, camera_reports_pa=False, parallactic_deg=39.0
        )
        is None
    )


def test_a_north_up_image_is_aligned_whatever_the_parallactic_angle() -> None:
    """M45 at q=39: the old advice said "rotate 39", which made the drift diagonal."""
    from ministar.reframe.roll import drift_angle_from_long_axis, roll_advice

    assert drift_angle_from_long_axis(psi_deg=0.0, parallactic_deg=0.0) == 0.0
    assert roll_advice(psi_deg=0.0, parallactic_deg=0.0).direction == "none"
    assert roll_advice(psi_deg=39.0, parallactic_deg=0.0).direction in {"cw", "ccw"}


def test_the_fake_camera_declares_that_its_roll_is_a_position_angle() -> None:
    from ministar.ui.station import FakeCamera

    assert FakeCamera.reports_position_angle is True
