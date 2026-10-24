"""Tests for the shutter-table snapping and optical helpers.

These encode the two arithmetic traps the research found: Canon's Tv values are
a discrete table, and ASTAP's ``-fov`` is the image height rather than the
diameter.
"""

from __future__ import annotations

import pytest

from ministar.camera.base import (
    MAX_TV_SECONDS,
    fov_height_deg,
    label_for_seconds,
    pixel_scale_arcsec,
    snap_shutter,
)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (0.5, 0.5),
        (1.0, 1.0),
        (2.0, 2.0),
        (3.2, 3.2),
        (2.2, 2.0),  # nearest in log space; 2.7 is not a legal value at all
        (2.45, 2.5),
        (0.002, 1 / 500),
        (1e9, MAX_TV_SECONDS),
        (0.000001, 1 / 4000),
    ],
)
def test_snap_shutter(requested: float, expected: float) -> None:
    assert snap_shutter(requested) == pytest.approx(expected)


def test_snap_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        snap_shutter(0)
    with pytest.raises(ValueError):
        snap_shutter(-1)


def test_labels() -> None:
    assert label_for_seconds(2.0) == '2"'
    assert label_for_seconds(30.0) == '30"'
    assert label_for_seconds(1 / 500) == "1/500"


def test_fov_is_height_not_diameter() -> None:
    # Canon EOS 60D sensor is 22.3 x 14.9 mm; 100 mm lens gives an 8.53 deg tall
    # field, which is what the real frame's CD matrix reported.
    assert fov_height_deg(14.9, 100.0) == pytest.approx(8.53, abs=0.01)
    assert fov_height_deg(14.9, 400.0) == pytest.approx(2.13, abs=0.01)


def test_pixel_scale() -> None:
    # 60D pixels are 4.29 um; measured 2.297 arcsec/px at 384.7 mm.
    assert pixel_scale_arcsec(4.29, 384.718) == pytest.approx(2.30, abs=0.01)
