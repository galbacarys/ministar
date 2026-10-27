"""Planning astronomy: checked against closed-form values, not against itself."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from ministar.sky import (
    altitude_deg,
    gmst_deg,
    hours_above,
    julian_date,
    lst_deg,
    max_altitude_deg,
    visible_minutes,
)

LAT = 39.16  # recorded site latitude in the briefs


def test_julian_date_at_j2000_epoch() -> None:
    assert julian_date(datetime(2000, 1, 1, 12, tzinfo=UTC)) == pytest.approx(2451545.0)


def test_gmst_at_the_j2000_epoch() -> None:
    """Meeus: GMST at 2000-01-01 12:00 UT is 280.46061837 deg."""
    assert gmst_deg(datetime(2000, 1, 1, 12, tzinfo=UTC)) == pytest.approx(280.46061837, abs=1e-6)


def test_lst_advances_with_longitude() -> None:
    when = datetime(2026, 10, 27, 0, tzinfo=UTC)
    east = lst_deg(when, 10.0)
    west = lst_deg(when, -10.0)
    assert (east - west) % 360 == pytest.approx(20.0)


def test_altitude_at_culmination_equals_max_altitude() -> None:
    """At hour angle zero the two must agree; this is the sign-error test."""
    for dec in (-40.0, 0.0, 41.27, 80.0):
        ra, lst = 123.4, 123.4  # H = 0
        assert altitude_deg(ra, dec, lst, LAT) == pytest.approx(max_altitude_deg(dec, LAT))


def test_circumpolar_target_never_sets() -> None:
    assert float(hours_above(89.9, LAT, 25.0)) == pytest.approx(24.0)


def test_target_that_never_rises() -> None:
    assert float(hours_above(-80.0, LAT, 25.0)) == pytest.approx(0.0)


def test_hours_above_matches_the_closed_form_for_an_equatorial_target() -> None:
    # cos H0 = sin(25) / cos(39.16) -> H0 = 56.96 deg -> 2*H0/15 = 7.595 h
    expected = (
        2.0
        * math.degrees(math.acos(math.sin(math.radians(25.0)) / math.cos(math.radians(LAT))))
        / 15.0
    )
    assert float(hours_above(0.0, LAT, 25.0)) == pytest.approx(expected, rel=1e-9)


def test_hours_above_is_vectorised() -> None:
    import numpy as np

    result = hours_above(np.array([0.0, 89.9, -80.0]), LAT, 25.0)
    assert isinstance(result, np.ndarray)
    assert result.shape == (3,)
    assert result[1] == pytest.approx(24.0)
    assert result[2] == pytest.approx(0.0)


def test_visible_minutes_is_bounded_by_the_window() -> None:
    start = datetime(2026, 10, 27, 0, tzinfo=UTC)
    end = datetime(2026, 10, 27, 8, tzinfo=UTC)
    # M31, autumn, from a mid-northern site: up for much of an 8 h night.
    minutes = float(
        visible_minutes(10.68, 41.27, latitude_deg=LAT, longitude_deg=-77.5, start=start, end=end)
    )
    assert 0.0 < minutes <= 480.0
    assert minutes > 120.0


def test_visible_minutes_broadcasts_over_targets() -> None:
    import numpy as np

    start = datetime(2026, 10, 27, 0, tzinfo=UTC)
    end = datetime(2026, 10, 27, 8, tzinfo=UTC)
    out = visible_minutes(
        np.array([10.68, 5.58]),  # M31 and M42-ish
        np.array([41.27, -5.39]),
        latitude_deg=LAT,
        longitude_deg=-77.5,
        start=start,
        end=end,
    )
    assert isinstance(out, np.ndarray)
    assert out.shape == (2,)


def test_a_window_that_ends_before_it_starts_is_rejected() -> None:
    start = datetime(2026, 10, 27, 4, tzinfo=UTC)
    end = datetime(2026, 10, 27, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        visible_minutes(0.0, 0.0, latitude_deg=LAT, longitude_deg=0.0, start=start, end=end)
