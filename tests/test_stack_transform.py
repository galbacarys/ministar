"""The transform that makes a bolted-down camera's stacking predictable.

The claims under test are the ones brief 15 §1 rests on: the map is the identity at the
solve's epoch, its translation is the drift the pointing advice draws, its rotation is
``Ω·sin δ``, and the projective fit is exact over a real field. A sign
error in any of them produces a stack that smears rather than fails, so each is pinned
against an independently-derived number rather than a golden value.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from ministar.optics import Optics
from ministar.pointing import drift_direction_px
from ministar.stack.transform import (
    SIDEREAL_RATE_DEG_PER_S,
    FieldModel,
    apply,
    compose,
    invert,
    rotation_deg,
)

EPOCH = datetime(2026, 1, 1, 22, 0, tzinfo=UTC)
OPTICS = Optics.from_pitch(width_px=480, height_px=320, pixel_um=4.434, focal_length_mm=135.0)
FULL = Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=4.434, focal_length_mm=135.0)


def make_model(**overrides) -> FieldModel:
    fields = {
        "ra_deg": 100.0,
        "dec_deg": 45.0,
        "pa_y_deg": 20.0,
        "plate_scale_arcsec": OPTICS.plate_scale_arcsec,
        "width_px": OPTICS.width_px,
        "height_px": OPTICS.height_px,
        "epoch": EPOCH,
    }
    fields.update(overrides)
    return FieldModel(**fields)


def test_the_map_is_the_identity_at_the_solve() -> None:
    matrix = make_model().homography(EPOCH)
    assert np.allclose(matrix / matrix[2, 2], np.eye(3), atol=1e-6)


def test_the_translation_is_the_sidereal_rate_times_cos_dec() -> None:
    model = make_model()
    later = EPOCH + timedelta(seconds=120)
    x, y = apply(model.homography(later), 239.5, 159.5)
    measured = math.hypot(x - 239.5, y - 159.5)
    expected = OPTICS.drift_px(dec_deg=45.0, seconds=120)
    assert measured == pytest.approx(expected, rel=0.01)


def test_the_translation_runs_where_the_pointing_advice_says_it_will() -> None:
    """The stack and the aiming arrows must not disagree about which way is west."""
    model = make_model()
    later = EPOCH + timedelta(seconds=60)
    x, y = apply(model.homography(later), 239.5, 159.5)
    direction = np.array([x - 239.5, y - 159.5])
    direction /= np.linalg.norm(direction)
    expected = np.array(drift_direction_px(model.plane, model.ra_deg, model.dec_deg))
    assert float(direction @ expected) > 0.999


def test_the_rotation_is_omega_sin_dec() -> None:
    model = make_model()
    later = EPOCH + timedelta(seconds=600)
    measured = rotation_deg(
        model.relative_homography(EPOCH, later), (model.width_px / 2, model.height_px / 2)
    )
    expected = SIDEREAL_RATE_DEG_PER_S * 600 * math.sin(math.radians(45.0))
    assert measured == pytest.approx(-expected, rel=0.01)


def test_rotation_is_maximal_near_the_pole_and_vanishes_at_the_equator() -> None:
    """10 §2.2: near the pole translation collapses but rotation stays at its maximum."""
    span = timedelta(seconds=600)
    centre = (OPTICS.width_px / 2, OPTICS.height_px / 2)
    at_equator = abs(
        rotation_deg(make_model(dec_deg=0.0).relative_homography(EPOCH, EPOCH + span), centre)
    )
    near_pole = abs(
        rotation_deg(make_model(dec_deg=85.0).relative_homography(EPOCH, EPOCH + span), centre)
    )
    assert at_equator < 0.05
    assert near_pole == pytest.approx(
        SIDEREAL_RATE_DEG_PER_S * 600 * math.sin(math.radians(85.0)), rel=0.02
    )


def test_the_relative_map_is_the_identity_at_its_own_reference() -> None:
    matrix = make_model().relative_homography(
        EPOCH + timedelta(seconds=77), EPOCH + timedelta(seconds=77)
    )
    assert np.allclose(matrix / matrix[2, 2], np.eye(3), atol=1e-6)


def test_the_projective_fit_is_exact_on_a_real_frame() -> None:
    model = make_model(
        plate_scale_arcsec=FULL.plate_scale_arcsec,
        width_px=FULL.width_px,
        height_px=FULL.height_px,
    )
    # An affine is 6.5 px out at the corners here; a homography is exact, and the residual
    # is the evidence rather than a golden number.
    assert model.residual_px(EPOCH + timedelta(seconds=900)) < 0.01


def test_compose_and_invert_undo_each_other() -> None:
    model = make_model()
    first = model.homography(EPOCH + timedelta(seconds=30))
    x, y = apply(compose(invert(first), first), 123.0, 45.0)
    assert x == pytest.approx(123.0, abs=1e-6)
    assert y == pytest.approx(45.0, abs=1e-6)


def test_composing_two_maps_equals_composing_them_in_sequence() -> None:
    model = make_model()
    a = model.homography(EPOCH + timedelta(seconds=40))
    b = model.homography(EPOCH + timedelta(seconds=100))
    through_b = apply(compose(b, invert(a)), 100.0, 50.0)
    via_a_then_b = apply(b, *apply(invert(a), 100.0, 50.0))
    assert through_b[0] == pytest.approx(via_a_then_b[0], abs=1e-6)
    assert through_b[1] == pytest.approx(via_a_then_b[1], abs=1e-6)


def test_binning_scales_the_plate_and_halves_the_grid() -> None:
    binned = FieldModel.for_binning(make_model(), 2)
    assert binned.plate_scale_arcsec == pytest.approx(OPTICS.plate_scale_arcsec * 2)
    assert binned.width_px == OPTICS.width_px // 2
    assert binned.height_px == OPTICS.height_px // 2
    # The same sky on a coarser grid: each binned pixel spans twice the angle, so the
    # drift is *halved* in pixels. Getting this backwards would double every warp.
    later = EPOCH + timedelta(seconds=60)
    x0, y0 = apply(make_model().homography(later), OPTICS.width_px / 2, OPTICS.height_px / 2)
    x1, y1 = apply(binned.homography(later), binned.width_px / 2, binned.height_px / 2)
    assert (x1 - binned.width_px / 2) == pytest.approx(0.5 * (x0 - OPTICS.width_px / 2), rel=1e-3)
    assert (y1 - binned.height_px / 2) == pytest.approx(0.5 * (y0 - OPTICS.height_px / 2), rel=1e-3)


def test_a_model_needs_a_positive_grid_and_scale() -> None:
    with pytest.raises(ValueError):
        make_model(width_px=0)
    with pytest.raises(ValueError):
        make_model(plate_scale_arcsec=0.0)


def test_a_bin_factor_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        FieldModel.for_binning(make_model(), 0)


def test_sample_points_cover_the_frame() -> None:
    samples = make_model().sample_pixels()
    assert samples.shape == (9, 2)
    assert samples[:, 0].min() == 0.0
    assert samples[:, 0].max() == OPTICS.width_px - 1
    assert samples[:, 1].max() == OPTICS.height_px - 1
