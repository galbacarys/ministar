"""The measured intersection: what the frame keeps when the sky turns as well as slides."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ministar import envelope
from ministar.optics import Optics
from ministar.projection import deproject, gnomonic


def _optics(scale_arcsec: float = 6.7747) -> Optics:
    """A 135 mm frame on the 60D: 9.76 x 6.50 deg."""
    return Optics.from_plate_scale(width_px=5184, height_px=3456, plate_scale_arcsec=scale_arcsec)


def _straight_line(optics: Optics, *, dec_deg: float, seconds: float) -> float:
    dx = min(optics.width_px, optics.drift_px(dec_deg=dec_deg, seconds=seconds))
    return max(0.0, (optics.width_px - dx) / optics.width_px)


def test_deproject_inverts_gnomonic() -> None:
    """The whole envelope rests on this round trip, so it is tested on its own."""
    ra, dec = deproject(160.27, 41.27, np.radians(0.5), np.radians(-0.25))
    xi, eta = gnomonic(160.27, 41.27, ra, dec)

    assert float(np.degrees(xi)) == pytest.approx(0.5, abs=1e-9)
    assert float(np.degrees(eta)) == pytest.approx(-0.25, abs=1e-9)


def test_a_short_run_needs_no_rotation_model() -> None:
    """Four minutes turns 1 deg: the closed form is exact for translation, so use it."""
    optics = _optics()
    assert not envelope.rotation_matters(240)
    assert optics.retained_fraction(dec_deg=41.27, seconds=240) == pytest.approx(
        _straight_line(optics, dec_deg=41.27, seconds=240)
    )


def test_near_the_pole_the_measured_intersection_takes_over() -> None:
    """Where drift approaches zero and rotation does not: 97% naive, 88% real."""
    optics = _optics()
    straight = _straight_line(optics, dec_deg=89.0, seconds=3600)
    measured = optics.retained_fraction(dec_deg=89.0, seconds=3600)

    assert straight > 0.95, "the drift really has gone"
    assert measured == pytest.approx(0.88, abs=0.03)
    assert measured < straight - 0.05


def test_the_intersection_shrinks_as_the_interval_grows() -> None:
    optics = _optics()
    fractions = [
        envelope.kept_fraction(
            width_px=optics.width_px,
            height_px=optics.height_px,
            plate_scale_arcsec=optics.plate_scale_arcsec,
            dec_deg=70.0,
            seconds=seconds,
        )
        for seconds in (600, 1200, 1800, 2400)
    ]
    assert fractions == sorted(fractions, reverse=True)


def test_a_zero_interval_keeps_everything() -> None:
    optics = _optics()
    assert optics.retained_fraction(dec_deg=89.0, seconds=0.0) == 1.0


def test_usable_megapixels_falls_when_the_frame_turns() -> None:
    """The number the user reads, not just the fraction behind it."""
    optics = _optics()
    naive = optics.full_megapixels() * _straight_line(optics, dec_deg=89.0, seconds=3600)
    measured = optics.usable_megapixels(dec_deg=89.0, seconds=3600)

    assert measured < naive - 1.5, "megapixels, not points of a fraction"


@pytest.mark.parametrize(
    ("dec_deg", "seconds", "expected"),
    [
        (89.0, 3600.0, True),  # pole, an hour: the frame turns 15 deg
        (41.0, 3600.0, False),  # the drift dominates long before the turn matters
        (89.0, 300.0, False),  # pole, but five minutes barely turns
    ],
)
def test_the_pole_caveat_is_gated_on_both_pole_and_interval(
    dec_deg: float, seconds: float, expected: bool
) -> None:
    assert envelope.rotation_dominates(dec_deg=dec_deg, seconds=seconds) is expected


def test_rotation_is_independent_of_declination_but_drift_is_not() -> None:
    """The distinction the whole module exists for."""
    optics = _optics()
    assert envelope.rotation_deg(3600) == pytest.approx(15.0411, abs=1e-6)
    assert optics.drift_arcsec_per_s(0.0) == pytest.approx(15.0411, abs=1e-4)
    assert optics.drift_arcsec_per_s(89.0) < 0.3


def test_rotation_never_invents_pixels() -> None:
    """Sanity fence: the measured fraction must stay a fraction."""
    optics = _optics()
    for dec in (-89.0, -45.0, 0.0, 41.27, 75.0, 89.0):
        for seconds in (0.0, 60.0, 1200.0, 7200.0):
            fraction = envelope.kept_fraction(
                width_px=optics.width_px,
                height_px=optics.height_px,
                plate_scale_arcsec=optics.plate_scale_arcsec,
                dec_deg=dec,
                seconds=seconds,
            )
            assert 0.0 <= fraction <= 1.0
            assert not math.isnan(fraction)
