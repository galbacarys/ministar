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

FIXTURE_LAT = 45.0
# An ARBITRARY mid-latitude that exists only to exercise the maths. It is NOT the
# observing site, which is a user input and is not known to this program -- see the
# module docstring in sky.py. Do not read it as a real location.


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
        at_culmination = altitude_deg(ra, dec, lst, FIXTURE_LAT)
        assert at_culmination == pytest.approx(max_altitude_deg(dec, FIXTURE_LAT))


def test_circumpolar_target_never_sets() -> None:
    assert float(hours_above(89.9, FIXTURE_LAT, 25.0)) == pytest.approx(24.0)


def test_target_that_never_rises() -> None:
    assert float(hours_above(-80.0, FIXTURE_LAT, 25.0)) == pytest.approx(0.0)


def test_hours_above_matches_the_closed_form_for_an_equatorial_target() -> None:
    # Closed form at whichever latitude the fixture uses: cos H0 = sin(25)/cos(lat).
    cos_h0 = math.sin(math.radians(25.0)) / math.cos(math.radians(FIXTURE_LAT))
    expected = 2.0 * math.degrees(math.acos(cos_h0)) / 15.0
    assert float(hours_above(0.0, FIXTURE_LAT, 25.0)) == pytest.approx(expected, rel=1e-9)


def test_hours_above_is_vectorised() -> None:
    import numpy as np

    result = hours_above(np.array([0.0, 89.9, -80.0]), FIXTURE_LAT, 25.0)
    assert isinstance(result, np.ndarray)
    assert result.shape == (3,)
    assert result[1] == pytest.approx(24.0)
    assert result[2] == pytest.approx(0.0)


def test_visible_minutes_is_bounded_by_the_window() -> None:
    start = datetime(2026, 10, 27, 0, tzinfo=UTC)
    end = datetime(2026, 10, 27, 8, tzinfo=UTC)
    # M31, autumn, from a mid-northern site: up for much of an 8 h night.
    minutes = float(
        visible_minutes(
            10.68,
            41.27,
            latitude_deg=FIXTURE_LAT,
            longitude_deg=-77.5,  # arbitrary test longitude, not a real site
            start=start,
            end=end,
        )
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
        latitude_deg=FIXTURE_LAT,
        longitude_deg=-77.5,  # arbitrary test longitude, not a real site
        start=start,
        end=end,
    )
    assert isinstance(out, np.ndarray)
    assert out.shape == (2,)


def test_a_window_that_ends_before_it_starts_is_rejected() -> None:
    start = datetime(2026, 10, 27, 4, tzinfo=UTC)
    end = datetime(2026, 10, 27, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        visible_minutes(0.0, 0.0, latitude_deg=FIXTURE_LAT, longitude_deg=0.0, start=start, end=end)


@pytest.mark.parametrize(
    ("func", "params"),
    [
        (altitude_deg, ("latitude_deg",)),
        (max_altitude_deg, ("latitude_deg",)),
        (hours_above, ("latitude_deg",)),
        (visible_minutes, ("latitude_deg", "longitude_deg")),
    ],
)
def test_site_coordinates_are_never_defaulted(func, params) -> None:
    """No built-in observing site, ever.

    A default latitude or longitude would be indistinguishable from a correct answer:
    a wrong latitude silently changes hours-above, and a wrong longitude shifts the
    night window by hours. Making these mandatory was a deliberate decision after a
    fixture value got mistaken for a measured site.
    """
    import inspect

    signature = inspect.signature(func)
    for name in params:
        assert signature.parameters[name].default is inspect.Parameter.empty, (
            f"{func.__name__}({name}) must not have a default"
        )
