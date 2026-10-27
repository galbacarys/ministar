"""The frame geometry, checked against the numbers already verified in the briefs.

The point of these tests is that the arithmetic is not new: brief 10 §2.1 published a
table of "time to 80% retention" measured against a real solve, and brief 01 verified
an 8.53° field on a real 100 mm frame. If this module disagrees with either, this
module is wrong.
"""

from __future__ import annotations

import math

import pytest

from ministar.io.tiff import RESOLUTION_UNIT_INCH, FrameExif
from ministar.optics import (
    DEFAULT_MIN_WINDOW_S,
    HINT_DEC_DEG,
    TINY_PX,
    Optics,
)

#: The 5202x3464 / 4.3 um frame the briefs were written against.
WIDTH, HEIGHT, PITCH = 5202, 3464, 4.3


def rig(focal_mm: float) -> Optics:
    return Optics.from_pitch(
        width_px=WIDTH, height_px=HEIGHT, pixel_um=PITCH, focal_length_mm=focal_mm
    )


# -- the frame --------------------------------------------------------------
def test_plate_scale_matches_the_published_values() -> None:
    assert rig(200).plate_scale_arcsec == pytest.approx(4.43, abs=0.01)
    assert rig(50).plate_scale_arcsec == pytest.approx(17.74, abs=0.01)
    assert rig(400).plate_scale_arcsec == pytest.approx(2.22, abs=0.01)


def test_field_height_matches_a_real_solved_frame() -> None:
    """Brief 01 verified 8.53 deg on a real 100 mm APS-C light frame."""
    assert rig(100).height_arcmin / 60 == pytest.approx(8.53, abs=0.01)


def test_field_of_view_shrinks_with_focal_length() -> None:
    heights = [rig(f).height_arcmin for f in (50, 100, 200, 400)]
    assert heights == sorted(heights, reverse=True)


# -- the cross-check against brief 10 §2.1 ----------------------------------
@pytest.mark.parametrize(
    ("focal", "dec", "published_min"),
    [
        # 02 §2.1's table: time to f = 0.80 with drift along the long axis.
        (50, 0, 20.4),
        (50, 45, 28.9),
        (50, 80, 117.5),
        (135, 0, 7.6),
        (135, 45, 10.7),
        (200, 0, 5.1),
        (200, 45, 7.2),
        (200, 80, 29.4),
        (400, 0, 2.6),
        (400, 45, 3.6),
        (400, 80, 14.7),
    ],
)
def test_window_reproduces_the_published_envelope_table(
    focal: float, dec: float, published_min: float
) -> None:
    window = rig(focal).window_seconds(fill=0.8, dec_deg=dec) / 60.0

    assert window == pytest.approx(published_min, rel=0.02)


def test_window_falls_to_zero_once_the_object_fills_the_frame() -> None:
    assert rig(200).window_seconds(fill=1.0, dec_deg=0.0) == 0.0
    assert rig(200).window_seconds(fill=1.4, dec_deg=0.0) == 0.0


def test_at_the_pole_there_is_no_drift() -> None:
    assert math.isinf(rig(400).window_seconds(fill=0.5, dec_deg=90.0))


# -- objects against the frame ---------------------------------------------
def test_fill_uses_the_better_orientation() -> None:
    """M31 at 400 mm is 93% of the 192' long axis, and that is what makes it useless."""
    optics = rig(400)

    assert optics.width_arcmin == pytest.approx(192, abs=1)
    assert optics.fill(178.0, 70.0) == pytest.approx(0.93, abs=0.01)


def test_a_long_thin_object_rotates_rather_than_filling_the_short_axis() -> None:
    """M16 is 120'x25': 62% of the long axis, but only 20% of the short one."""
    optics = rig(400)

    assert optics.fill(120.0, 25.0) == pytest.approx(0.62, abs=0.01)
    assert optics.fill(25.0, 120.0) == pytest.approx(0.62, abs=0.01), "orientation-free"


def test_a_missing_minor_axis_is_assumed_circular() -> None:
    optics = rig(400)

    assert optics.fill(60.0, None) > optics.fill(60.0, 15.0)


def test_object_size_in_pixels_is_concrete() -> None:
    assert rig(400).object_size_px(178.0) == pytest.approx(4810, rel=0.02)
    assert rig(50).object_size_px(178.0) == pytest.approx(602, rel=0.02)


def test_the_dot_threshold_is_a_pixel_count_not_an_angle() -> None:
    assert TINY_PX == 20.0
    # 20 px is 5.9' at 50 mm and 0.7' at 400 mm: no single angle could serve both.
    assert rig(50).object_size_px(TINY_PX * rig(50).plate_scale_arcsec / 60) == pytest.approx(
        TINY_PX
    )


# -- the window as a control -----------------------------------------------
def test_max_fill_inverts_the_window_formula() -> None:
    optics = rig(400)

    fill = optics.max_fill(dec_deg=0.0, min_window_s=300.0)
    assert fill == pytest.approx(0.61, abs=0.01)
    assert optics.window_seconds(fill=fill, dec_deg=0.0) == pytest.approx(300.0, rel=0.01)


def test_near_the_pole_even_a_huge_object_gets_a_long_window() -> None:
    optics = rig(400)

    assert optics.max_fill(dec_deg=80.0, min_window_s=300.0) == pytest.approx(0.93, abs=0.01)
    assert optics.window_seconds(fill=0.9, dec_deg=80.0) > 300.0


def test_the_slider_hint_is_a_size_at_a_stated_declination() -> None:
    optics = rig(200)

    hint = optics.hint_size_arcmin(min_window_s=DEFAULT_MIN_WINDOW_S)

    assert HINT_DEC_DEG == 45.0
    # At 45 deg the same 300 s allows a graciously larger object than at the equator.
    assert hint > optics.max_size_arcmin(dec_deg=0.0, min_window_s=DEFAULT_MIN_WINDOW_S)
    assert hint == pytest.approx(331, rel=0.02)


# -- refusals ---------------------------------------------------------------
def test_an_impossible_pitch_is_refused_with_a_way_out() -> None:
    """352.8 um is the EOS 60D's 72 dpi placeholder, and 82x wrong."""
    with pytest.raises(ValueError, match="not physical"):
        Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=352.78, focal_length_mm=24.0)


def test_exif_with_the_60d_placeholder_names_the_real_problem() -> None:
    exif = FrameExif(
        make="Canon",
        model="Canon EOS 60D",
        lens_model="EF-S24mm f/2.8 STM",
        focal_length_mm=24.0,
        focal_plane_x_resolution=72.0,
        focal_plane_y_resolution=72.0,
        resolution_unit=RESOLUTION_UNIT_INCH,
        pixel_x_dimension=5184,
        pixel_y_dimension=3456,
        orientation=1,
    )

    with pytest.raises(ValueError, match="plate solve"):
        Optics.from_exif(exif)


def test_a_frame_with_a_sane_pitch_becomes_optics() -> None:
    exif = FrameExif(
        focal_length_mm=200.0,
        focal_plane_x_resolution=25400 / PITCH,
        focal_plane_y_resolution=25400 / PITCH,
        resolution_unit=RESOLUTION_UNIT_INCH,
        pixel_x_dimension=WIDTH,
        pixel_y_dimension=HEIGHT,
        orientation=1,
    )

    optics = Optics.from_exif(exif)

    assert optics.plate_scale_arcsec == pytest.approx(4.43, abs=0.01)
    assert optics.focal_length_mm == 200.0
    assert "200 mm" in optics.description


def test_zero_sized_frames_are_refused() -> None:
    with pytest.raises(ValueError):
        Optics.from_plate_scale(width_px=0, height_px=10, plate_scale_arcsec=1.0)
    with pytest.raises(ValueError):
        Optics.from_plate_scale(width_px=10, height_px=10, plate_scale_arcsec=0.0)
