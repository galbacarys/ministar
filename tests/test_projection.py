"""Sky to pixels, so a synthetic frame can be solved.

The fake telescope's whole value is that ASTAP can recover what we rendered, so the
geometry has to be a real tangent plane with the conventions ``Solution`` uses: PA of the
image +y axis, north up and east left, pixels 0-based with y downward.
"""

from __future__ import annotations

import math
from datetime import UTC

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


def test_the_fake_mount_is_an_alt_az_head() -> None:
    """A tripod head is adjusted in altitude and azimuth; RA/Dec is a consequence."""
    from datetime import datetime

    from ministar.rig import RigProfile
    from ministar.site import Site
    from ministar.ui.station import FakeCamera

    site = Site(latitude_deg=51.5, longitude_deg=-0.1)
    camera = FakeCamera(
        optics=RigProfile.fake().optics(),
        site=site,
        when=datetime.now(UTC),
        pointing=(160.27, 41.27),
    )
    alt0, az0 = camera.altaz

    assert camera.nudge_altaz(d_az_deg=1.0)
    assert camera.altaz[1] == pytest.approx((az0 + 1.0) % 360.0)
    assert camera.altaz[0] == pytest.approx(alt0), "azimuth must not change altitude"

    camera.nudge_altaz(d_alt_deg=100.0)  # the zenith is a stop, not a suggestion
    assert camera.altaz[0] == pytest.approx(89.9)
    camera.nudge_altaz(d_alt_deg=-200.0)
    assert camera.altaz[0] >= -5.0


def test_a_degree_of_azimuth_is_not_a_degree_of_sky() -> None:
    """The realism the alt-az controls exist for: azimuth lines converge at the zenith, so
    a degree of azimuth buys less sky the higher you point -- the same 5' click is coarse
    near the horizon and fine overhead."""
    from datetime import datetime

    from ministar import sky
    from ministar.site import Site

    site = Site(latitude_deg=51.5, longitude_deg=-0.1)
    lst = sky.lst_deg(datetime.now(UTC), site.longitude_deg)

    def sky_step(alt_deg: float) -> float:
        ra1, dec1 = sky.radec_from_altaz(alt_deg, 180.0, lst, site.latitude_deg)
        ra2, dec2 = sky.radec_from_altaz(alt_deg, 181.0, lst, site.latitude_deg)
        # RA is cyclic: a naive ra2 - ra1 reports ~269 degrees when the pair straddles 0.
        # That made this test pass or fail depending on the time of day it was run.
        gap_ra = abs((float(ra2) - float(ra1) + 180.0) % 360.0 - 180.0)
        return float(abs(dec2 - dec1) + gap_ra * 0.75)

    assert sky_step(20.0) > 4.0 * sky_step(80.0), "overhead, 1 deg of az is not 1 deg of sky"


def test_the_fake_cannot_be_nudged_without_a_site() -> None:
    """No latitude and longitude means an altitude is a number without a sky."""
    from ministar.ui.station import FakeCamera

    assert FakeCamera().nudge_altaz(d_az_deg=1.0) is False


def test_moving_the_fake_changes_the_field_it_shows() -> None:
    """Otherwise the controls would be decoration: the scene must follow the mount."""
    from datetime import datetime

    from ministar.rig import RigProfile
    from ministar.site import Site
    from ministar.starcatalog import Stars
    from ministar.ui.station import FakeCamera

    class StubCatalogue:
        """Two stars near the centre: the test must not walk the 1.3 GB database."""

        def stars_near(self, ra_deg, dec_deg, radius_deg, *, mag_limit=12.0):
            return Stars(np.array([10.70, 10.80]), np.array([41.27, 41.20]), np.array([6.0, 7.0]))

    camera = FakeCamera(
        optics=RigProfile.fake().optics(),
        catalog=StubCatalogue(),
        site=Site(latitude_deg=51.5, longitude_deg=-0.1),
        when=datetime.now(UTC),
        pointing=(10.6847, 41.269),
    )
    before = camera.scene().stars[0].x

    camera.nudge_altaz(d_az_deg=1.0)
    after = camera.scene().stars[0].x

    assert before != pytest.approx(after, abs=1e-6), "the field must shift with the mount"


def test_the_preview_follows_the_mount() -> None:
    """The bug that made the mount buttons look dead: the live view must show the pointing.

    render_frame() rendered the *un-pointed* random field, so moving the telescope changed no
    pixel. Roll did change it, which is why the slider worked and the arrows did not.
    """
    from ministar.optics import Optics
    from ministar.starcatalog import Stars
    from ministar.ui.station import FakeCamera

    class StubCatalogue:
        def stars_near(self, ra_deg, dec_deg, radius_deg, *, mag_limit=12.0):
            return Stars(np.array([10.70, 10.80]), np.array([41.27, 41.20]), np.array([6.0, 7.0]))

    from datetime import datetime

    from ministar.site import Site

    optics = Optics.from_plate_scale(width_px=400, height_px=300, plate_scale_arcsec=60.0)
    camera = FakeCamera(
        optics=optics,
        catalog=StubCatalogue(),
        site=Site(latitude_deg=51.5, longitude_deg=-0.1),
        when=datetime.now(UTC),
        pointing=(10.6847, 41.269),
    )
    before = camera.render_frame().copy()

    camera.nudge_altaz(d_az_deg=1.0)
    after = camera.render_frame()

    assert not np.array_equal(before, after), "the preview must move with the mount"


# -- the sky moves under a fixed mount --------------------------------------
def _drifting_camera(start_ra=160.27, start_dec=41.27, catalog=None):
    from datetime import datetime

    from ministar.rig import RigProfile
    from ministar.site import Site
    from ministar.ui.station import FakeCamera

    now = datetime(2026, 10, 27, 22, 0, tzinfo=UTC)
    clock = {"now": now}
    camera = FakeCamera(
        optics=RigProfile.fake().optics(),
        catalog=catalog,
        site=Site(latitude_deg=51.5, longitude_deg=-0.1),
        pointing=(start_ra, start_dec),
        clock=lambda: clock["now"],
    )
    return camera, clock, now


def test_the_sky_drifts_under_a_fixed_mount() -> None:
    """Ten minutes on: RA has advanced by ten minutes of sidereal time, exactly."""
    from datetime import timedelta

    from ministar import sky

    camera, clock, now = _drifting_camera()
    ra0, dec0 = camera.pointing
    lst0 = sky.lst_deg(now, -0.1)

    clock["now"] = now + timedelta(minutes=10)
    ra1, dec1 = camera.pointing

    delta_ra = ((ra1 - ra0 + 180.0) % 360.0) - 180.0
    lst1 = sky.lst_deg(now + timedelta(minutes=10), -0.1)
    assert delta_ra == pytest.approx(((lst1 - lst0 + 180.0) % 360.0) - 180.0), "= 10 min of LST"
    assert abs(dec1 - dec0) < 0.01, (
        "a fixed altitude is not a fixed declination -- the mount holds alt/az, so the sky "
        "position it reaches traces a curve, a few arcmin over ten minutes"
    )

    # The *angle swept* carries the cos(dec): that is the rate the sensor sees.
    swept_arcsec = delta_ra * math.cos(math.radians(dec0)) * 3600.0
    assert swept_arcsec == pytest.approx(camera.drift_arcsec_per_s() * 600.0, rel=0.01)


def test_holding_the_sky_still_freezes_it_where_it_is() -> None:
    from datetime import timedelta

    camera, clock, now = _drifting_camera()
    clock["now"] = now + timedelta(minutes=5)
    frozen = camera.pointing

    camera.set_drift(False)
    clock["now"] = now + timedelta(minutes=90)

    assert camera.pointing == pytest.approx(frozen), "holding must not rewind the sky"


def test_the_rendered_field_moves_at_the_drift_rate() -> None:
    """Not just the number: the picture has to crawl at the rate we quote."""
    from datetime import timedelta

    from ministar.starcatalog import Stars

    class StubCatalogue:
        def stars_near(self, ra_deg, dec_deg, radius_deg, *, mag_limit=12.0):
            return Stars(
                np.array([160.60, 160.62, 160.64]),
                np.array([41.27, 41.30, 41.24]),
                np.ones(3) * 6.0,
            )

    camera, clock, now = _drifting_camera(160.62, 41.27, catalog=StubCatalogue())
    x0 = min(star.x for star in camera.scene().stars)

    seconds = 600.0
    clock["now"] = now + timedelta(seconds=seconds)
    x1 = min(star.x for star in camera.scene().stars)

    expected_px = camera.drift_px_per_s() * seconds
    assert abs(x1 - x0) == pytest.approx(expected_px, rel=0.05), "within a few percent"


def test_local_time_zone_does_not_change_where_it_points() -> None:
    """A clock is a moment, not a wall display."""
    from datetime import datetime, timedelta, timezone

    from ministar.site import Site
    from ministar.ui.station import FakeCamera

    site = Site(latitude_deg=51.5, longitude_deg=-0.1)
    moment = datetime(2026, 10, 27, 22, 0, tzinfo=UTC)
    elsewhere = moment.astimezone(timezone(timedelta(hours=9)))

    a = FakeCamera(site=site, pointing=(160.27, 41.27), clock=lambda: moment)
    b = FakeCamera(site=site, pointing=(160.27, 41.27), clock=lambda: elsewhere)

    assert a.pointing == pytest.approx(b.pointing)
