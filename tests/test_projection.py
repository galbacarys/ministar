"""Sky to pixels, so a synthetic frame can be solved.

The fake telescope's whole value is that ASTAP can recover what we rendered, so the
geometry has to be a real tangent plane with the conventions ``Solution`` uses: PA of the
image +y axis, north up and east left, pixels 0-based with y downward.
"""

from __future__ import annotations

import numpy as np
import pytest

from ministar.projection import TangentPlane, stars_in_frame

SCALE = 6.77  # arcsec/px, the 135 mm rig


def plane(**overrides: object) -> TangentPlane:
    values: dict[str, object] = {
        "ra_deg": 10.6847,
        "dec_deg": 41.269,
        "pa_y_deg": 0.0,
        "plate_scale_arcsec": SCALE,
        "width_px": 5184,
        "height_px": 3456,
    }
    values.update(overrides)
    return TangentPlane(**values)  # type: ignore[arg-type]


def test_the_pointing_lands_on_the_frame_centre() -> None:
    p = plane()
    x, y = p.project(np.array([10.6847]), np.array([41.269]))

    assert x[0] == pytest.approx(p.centre_px[0])
    assert y[0] == pytest.approx(p.centre_px[1])


def test_one_degree_north_moves_up_the_frame() -> None:
    """North up: a star north of centre has a smaller row index, at 1 deg / scale."""
    p = plane()
    x, y = p.project(np.array([10.6847]), np.array([42.269]))
    expected_px = 3600.0 / SCALE

    assert x[0] == pytest.approx(p.centre_px[0], abs=0.5)
    assert y[0] == pytest.approx(p.centre_px[1] - expected_px, rel=0.02)


def test_one_degree_east_moves_left_the_frame() -> None:
    """East left is the astronomical convention, and the opposite of a screen mockup."""
    p = plane()
    ra_east = 10.6847 + 1.0 / np.cos(np.radians(41.269))
    x, _ = p.project(np.array([ra_east]), np.array([41.269]))
    expected_px = 3600.0 / SCALE

    assert x[0] == pytest.approx(p.centre_px[0] - expected_px, rel=0.02)


def test_pa_rotates_the_frame() -> None:
    """At PA_y = 90 the +y axis points east, so north is now to the *right*."""
    p = plane(pa_y_deg=90.0)
    x, y = p.project(np.array([10.6847]), np.array([42.269]))

    assert x[0] > p.centre_px[0] + 100, "north should land right of centre"
    assert y[0] == pytest.approx(p.centre_px[1], abs=5.0)


def test_mirrored_geometry_flips_the_horizontal_axis() -> None:
    upright = plane(east_left=True)
    mirrored = plane(east_left=False)
    ra_east = 10.6847 + 1.0 / np.cos(np.radians(41.269))

    x_upright, _ = upright.project(np.array([ra_east]), np.array([41.269]))
    x_mirrored, _ = mirrored.project(np.array([ra_east]), np.array([41.269]))

    assert (x_upright[0] - upright.centre_px[0]) == pytest.approx(
        -(x_mirrored[0] - mirrored.centre_px[0])
    )


def test_stars_in_frame_keeps_only_what_is_inside() -> None:
    p = plane()
    ra = np.array([10.6847, 10.6847, 100.0])
    dec = np.array([41.269, 42.269, 41.269])
    mag = np.array([5.0, 6.0, 7.0])

    x, _, kept = stars_in_frame(ra, dec, mag, p)

    assert len(kept) == 2
    assert kept.tolist() == [5.0, 6.0]
    assert np.all(x >= 0) and np.all(x <= p.width_px)


def test_offsets_map_to_distinct_pixels() -> None:
    """Sanity: the projection is not silently collapsing everything onto the centre."""
    p = plane()
    offsets = np.array([0.0, 0.5, 1.0, 2.0, 3.0])
    _, y = p.project(np.full_like(offsets, 41.269), 41.269 + offsets)

    steps = np.diff(y)
    assert np.all(steps < 0), "north of centre must march up the frame"
    assert len(set(np.round(y, 3))) == len(offsets)
    # The projection is not exactly linear, but over 3 deg it should not be far off.
    assert abs(y[1] - y[0]) == pytest.approx(0.5 * 3600 / SCALE, rel=0.02)


def test_the_fake_mount_can_be_nudged() -> None:
    """Aim roughly, then let the solver say how far off we are -- that is the loop."""
    from ministar.rig import RigProfile
    from ministar.ui.station import FakeCamera

    camera = FakeCamera(optics=RigProfile.fake().optics(), pointing=(10.0, 41.0))

    camera.move_by(d_ra_deg=0.5, d_dec_deg=-0.25)
    assert camera.pointing == pytest.approx((10.5, 40.75))

    camera.move_by(d_ra_deg=-20.0)  # RA wraps
    assert camera.pointing[0] == pytest.approx(350.5)

    camera.move_by(d_dec_deg=100.0)  # Dec clamps at the pole
    assert camera.pointing[1] == pytest.approx(90.0)


def test_moving_the_fake_changes_the_field_it_shows() -> None:
    """Otherwise the controls would be decoration: the scene must follow the pointing."""
    from ministar.rig import RigProfile
    from ministar.starcatalog import StarCatalog
    from ministar.ui.station import FakeCamera

    catalog = StarCatalog.load("/mnt/c/Program Files/astap")
    camera = FakeCamera(
        optics=RigProfile.fake().optics(), catalog=catalog, pointing=(10.6847, 41.269)
    )
    before = camera.scene().stars[0].x

    camera.move_by(d_ra_deg=1.0)
    after = camera.scene().stars[0].x

    assert before != pytest.approx(after, abs=1e-6), "the field must shift with the mount"
