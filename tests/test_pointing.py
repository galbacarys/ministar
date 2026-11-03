"""Reading a solve back: the facts, and the crop that changes their meaning.

The fixture is brief 07's real solved frame (``CD`` matrix, 1958 px tall, 2.297"/px, PA_y
178.05 deg), so these numbers are not invented.
"""

from __future__ import annotations

import math
from datetime import UTC

import pytest

from ministar.pointing import MIN_USEFUL_FRACTION, crop_factor, describe, solved_width_deg
from ministar.solver.astap import Solution

#: Brief 07 §5, a real solve.
FRAME_A = Solution(
    crpix=(1469.0, 979.5),
    crval=(80.0, 69.23),
    cdelt=(0.0, 0.0),
    crota=(-178.051, -178.036),
    cd=(
        6.3766968388110333e-4,
        2.1696682773295344e-5,
        2.1874558883785833e-5,
        -6.3784928262788855e-4,
    ),
)


def test_the_solved_width_comes_from_the_cd_matrix() -> None:
    """Brief 07 records 1958 px at 2.297"/px = 1.249 deg; the same number from CD."""
    assert solved_width_deg(FRAME_A, width_px=1958) * 60 == pytest.approx(74.95, abs=0.2)


def test_a_full_frame_solve_produces_no_warnings() -> None:
    report = describe(FRAME_A, width_px=1958, expected_width_deg=1.249)

    assert not report.cropped
    assert report.warnings == ()
    assert any("position angle" in fact for fact in report.facts)


def test_a_mild_crop_is_a_note_not_a_warning() -> None:
    """A centred crop keeps the centre and the roll exact; only the edges go unverified."""
    report = describe(FRAME_A, width_px=1958, expected_width_deg=1.249 / 0.95)

    assert report.cropped
    assert report.usable
    assert report.warnings == ()
    assert "centre and roll are good" in report.notes[0]


def test_the_middle_quarter_is_enough_to_work_from() -> None:
    """A 100 mm frame solved at 34% is still worth having: the centre and roll are exact."""
    report = describe(FRAME_A, width_px=5184, expected_width_deg=9.76)

    assert report.cropped
    assert report.usable, "a quarter of the frame is the bar, and 34% clears it"
    assert report.warnings == ()
    assert "edges unverified" in report.notes[0]


def test_below_a_quarter_solved_the_warning_names_the_remedy() -> None:
    """The 50 mm case: an ultra-wide whose field the database can barely sample."""
    report = describe(FRAME_A, width_px=5184, expected_width_deg=26.34)

    assert report.cropped
    assert not report.usable
    warning = report.warnings[0]
    assert "less than a quarter" in warning
    assert "wider star database" in warning
    assert "W08" in warning


def test_the_usable_limit_is_a_quarter_of_the_frame() -> None:
    """25% solved is the bar: 26% is accepted, 24% is not."""
    accepted = 1.249 / (MIN_USEFUL_FRACTION + 0.01)
    rejected = 1.249 / (MIN_USEFUL_FRACTION - 0.01)
    inside = describe(FRAME_A, width_px=1958, expected_width_deg=accepted)
    outside = describe(FRAME_A, width_px=1958, expected_width_deg=rejected)

    assert MIN_USEFUL_FRACTION == 0.25
    assert inside.usable and not inside.warnings
    assert not outside.usable and outside.warnings


def test_without_an_expected_field_there_is_nothing_to_compare() -> None:
    assert crop_factor(FRAME_A, width_px=1958, expected_width_deg=None) is None
    report = describe(FRAME_A, width_px=1958)
    assert not report.cropped
    assert report.usable
    assert report.solved_fraction is None


def test_a_mirrored_solution_warns_that_instructions_flip() -> None:
    mirrored = Solution(
        crpix=FRAME_A.crpix,
        crval=FRAME_A.crval,
        cdelt=FRAME_A.cdelt,
        crota=FRAME_A.crota,
        cd=(FRAME_A.cd[0], FRAME_A.cd[1], FRAME_A.cd[2], -FRAME_A.cd[3]),
    )

    report = describe(mirrored, width_px=1958, expected_width_deg=1.249)

    assert any("mirrored" in warning for warning in report.warnings)


def test_the_position_angle_uses_cd_not_crota() -> None:
    """CROTA2 = -178.036 and PA_y = 178.051 on this frame; both are 'close', one is right."""
    report = describe(FRAME_A, width_px=1958)

    assert "178.05" in report.facts[1]


# -- what to tell the solver ------------------------------------------------
def test_the_first_solve_is_blind_but_for_the_field() -> None:
    """We aimed by hand at a target; that is not where the camera is. Do not fake a position."""
    from ministar.pointing import BLIND_RADIUS_DEG, solve_hints

    hints = solve_hints(fov_height_deg=2.19)

    assert hints.fov_height_deg == 2.19, "the lens is known"
    assert hints.ra_hours is None and hints.south_pole_distance_deg is None
    assert hints.radius_deg == BLIND_RADIUS_DEG == 180.0


def test_a_resolve_hints_around_the_previous_solution() -> None:
    from ministar.pointing import HINT_FLOOR_RADIUS_DEG, solve_hints

    hints = solve_hints(fov_height_deg=2.19, previous=(160.27, 41.27))

    assert hints.ra_hours == pytest.approx(160.27 / 15.0), "no time has passed, so no shift"
    assert hints.south_pole_distance_deg == pytest.approx(131.27)
    assert hints.radius_deg == pytest.approx(HINT_FLOOR_RADIUS_DEG)


def test_a_pointing_capture_never_keeps_the_camera_file() -> None:
    """A 20 MB RAW nobody will read again; the RUN loop keeps the frames that matter."""
    from ministar.pointing import keep_capture

    assert keep_capture(solved=True, converted_is_raw=False) is False
    assert keep_capture(solved=False, converted_is_raw=False) is True, (
        "a failed solve keeps the converted FITS: it is the only evidence of why"
    )


def test_the_fake_frame_is_never_kept() -> None:
    """It is regenerable from the catalogue, so it cannot be evidence of anything."""
    from ministar.pointing import keep_capture

    assert keep_capture(solved=True, converted_is_raw=True) is False
    assert keep_capture(solved=False, converted_is_raw=True) is False


# -- where to aim -----------------------------------------------------------
def _fake_optics():
    from ministar.rig import RigProfile

    return RigProfile.fake().optics()


def _advice(ra, dec, *, pa_y_deg=0.0, target=(160.27, 41.27)):
    from ministar.pointing import frame_advice

    return frame_advice(
        ra_deg=ra,
        dec_deg=dec,
        target_ra_deg=target[0],
        target_dec_deg=target[1],
        optics=_fake_optics(),
        pa_y_deg=pa_y_deg,
    )


def test_aiming_at_the_target_throws_away_half_the_frame() -> None:
    """The point of the entry point: the same pointing buys twice the run."""
    from ministar.pointing import best_start

    optics = _fake_optics()
    start = best_start(target_ra_deg=160.27, target_dec_deg=41.27, optics=optics, pa_y_deg=0.0)
    centred = _advice(160.27, 41.27)
    entry = _advice(*start)

    assert entry.inside and centred.inside
    assert entry.minutes_now == pytest.approx(2.0 * centred.minutes_now, rel=0.10)
    assert entry.at_best_start
    assert not centred.at_best_start


def test_the_best_start_is_west_of_the_target() -> None:
    """Untracked stars move west, so the frame centre starts west of the target."""
    from ministar.pointing import best_start

    start = best_start(
        target_ra_deg=160.27, target_dec_deg=41.27, optics=_fake_optics(), pa_y_deg=0.0
    )
    assert start[0] < 160.27, "centre west of target = decreasing RA"
    assert start[1] > 41.27, "and north: a parallel sags below the tangent plane"


def test_the_advice_says_which_way_to_move_the_mount() -> None:
    lines = " ".join(_advice(160.27, 41.27).describe())
    assert "move the mount" in lines
    assert "west" in lines, "the entry point is west of a target-centred pointing"


@pytest.mark.parametrize("roll", [0.0, 20.0, 35.0, 60.0, 90.0, 135.0])
def test_the_entry_point_is_inside_the_frame_at_any_roll(roll: float) -> None:
    """A diagonal drift is limited by the short side, and the margin is a box: both bit."""
    from ministar.pointing import best_start

    start = best_start(
        target_ra_deg=160.27, target_dec_deg=41.27, optics=_fake_optics(), pa_y_deg=roll
    )
    advice = _advice(*start, pa_y_deg=roll)

    assert advice.inside
    assert advice.minutes_now > 1.0, "a run needs to be worth starting"
    assert advice.minutes_now == pytest.approx(advice.minutes_best, rel=0.05)


def test_a_target_outside_the_frame_is_reported_not_hidden() -> None:
    """Inform, never block: a pointing 10 deg off is said plainly."""
    lines = " ".join(_advice(170.0, 41.27).describe())
    assert "NOT in the frame" in lines
    assert "min" in lines


# -- the hint walks with the sky --------------------------------------------
def test_the_hint_direction_matches_what_the_fake_sky_actually_does() -> None:
    """The test that catches a sign error: measure the drift, then check the hint agrees.

    Stars march west across the frame, but the frame's own sky position moves EAST (RA
    increases), because a fixed mount holds the hour angle and RA = LST - HA. A hint aimed at
    the target's motion instead of the pointing's is wrong by twice the drift.
    """
    from datetime import datetime, timedelta

    from ministar.pointing import solve_hints
    from ministar.rig import RigProfile
    from ministar.site import Site
    from ministar.ui.station import FakeCamera

    now = datetime(2026, 10, 27, 22, 0, tzinfo=UTC)
    clock = {"now": now}
    camera = FakeCamera(
        optics=RigProfile.fake().optics(),
        site=Site(latitude_deg=51.5, longitude_deg=-0.1),
        pointing=(160.27, 41.27),
        clock=lambda: clock["now"],
    )
    before = camera.pointing
    clock["now"] = now + timedelta(minutes=10)
    after = camera.pointing

    hints = solve_hints(fov_height_deg=6.5, previous=before, elapsed_s=600.0, dec_deg=before[1])
    guess = hints.ra_hours * 15.0
    moved = ((after[0] - before[0] + 180.0) % 360.0) - 180.0

    assert moved > 0, "the pointing advances east, whatever the stars do"
    assert guess == pytest.approx(before[0] + moved / 2.0, abs=0.01), "halfway there"
    # ...and the radius really does contain both ends of the interval.
    assert hints.radius_deg >= moved * math.cos(math.radians(before[1])) / 2.0


def test_a_tight_hint_when_the_last_solve_was_seconds_ago() -> None:
    from ministar.pointing import HINT_FLOOR_RADIUS_DEG, solve_hints

    hints = solve_hints(fov_height_deg=6.5, previous=(160.27, 41.27), elapsed_s=0.0)

    assert hints.ra_hours == pytest.approx(160.27 / 15.0)
    assert hints.radius_deg == pytest.approx(HINT_FLOOR_RADIUS_DEG)


def test_the_hint_widens_with_the_gap() -> None:
    from ministar.pointing import BLIND_RADIUS_DEG, solve_hints

    radii = [
        solve_hints(
            fov_height_deg=6.5, previous=(160.27, 41.27), elapsed_s=seconds, dec_deg=41.27
        ).radius_deg
        for seconds in (60, 600, 1800, 3600)
    ]
    assert radii == sorted(radii), "a longer gap cannot need a smaller circle"
    assert all(radius < BLIND_RADIUS_DEG for radius in radii), "still a hint, not a gamble"


def test_a_gap_of_a_day_is_just_a_blind_solve() -> None:
    from ministar.pointing import BLIND_RADIUS_DEG, solve_hints

    hints = solve_hints(fov_height_deg=6.5, previous=(160.27, 41.27), elapsed_s=86400.0)

    assert hints.radius_deg == BLIND_RADIUS_DEG
    assert hints.ra_hours is None, "no point pretending to know"


def test_a_short_gap_near_the_pole_needs_almost_no_radius() -> None:
    """Rotation outruns drift there, but the pointing barely moves: 15.04 cos(dec)."""
    from ministar.pointing import solve_hints

    near_pole = solve_hints(
        fov_height_deg=6.5, previous=(160.27, 85.0), elapsed_s=1800.0, dec_deg=85.0
    )
    near_equator = solve_hints(
        fov_height_deg=6.5, previous=(160.27, 0.0), elapsed_s=1800.0, dec_deg=0.0
    )

    assert near_pole.radius_deg < near_equator.radius_deg


# -- aiming must know how big the target is -------------------------------
def test_an_extended_target_is_aimed_whole_and_the_advice_agrees() -> None:
    """The bug this replaced: a 1.7 deg object aimed by its centre is half off the sensor.

    "aim for the run" placed the target's *centre* on the frame edge, while the advice required
    the whole object to fit -- so the page said "NOT in the frame" and then told the user to
    move 50', which was the size of the object, not the error.
    """
    from ministar.pointing import best_start, frame_advice

    optics = _fake_optics()
    ra, dec, radius_arcmin = 56.87, 24.12, 50.0  # the Pleiades

    start = best_start(
        target_ra_deg=ra,
        target_dec_deg=dec,
        optics=optics,
        pa_y_deg=0.0,
        target_radius_arcmin=radius_arcmin,
    )
    advice = frame_advice(
        ra_deg=start[0],
        dec_deg=start[1],
        target_ra_deg=ra,
        target_dec_deg=dec,
        optics=optics,
        pa_y_deg=0.0,
        target_radius_arcmin=radius_arcmin,
    )

    assert advice.inside, "the whole object has to fit, not just its centre"
    assert advice.at_best_start
    assert optics.width_arcmin / 2 - advice.east_arcmin >= radius_arcmin, "clearance for its edge"


def test_an_extended_target_gets_a_shorter_run_than_a_point_source() -> None:
    """Its own width is part of the budget: 34 min against 51 for a star at the same place."""
    from ministar.pointing import best_start, frame_advice

    optics = _fake_optics()
    ra, dec = 56.87, 24.12
    runs = {}
    for radius_arcmin in (0.0, 50.0):
        start = best_start(
            target_ra_deg=ra,
            target_dec_deg=dec,
            optics=optics,
            pa_y_deg=0.0,
            target_radius_arcmin=radius_arcmin,
        )
        runs[radius_arcmin] = frame_advice(
            ra_deg=start[0],
            dec_deg=start[1],
            target_ra_deg=ra,
            target_dec_deg=dec,
            optics=optics,
            pa_y_deg=0.0,
            target_radius_arcmin=radius_arcmin,
        ).minutes_now

    # The reduction is not arbitrary: it is exactly the time the sky needs to cross the
    # object's own width (2 x 50' here). 34.2 against 41.5 min measured, 7.3 min predicted.
    arcmin_per_min = optics.drift_arcsec_per_s(dec) / 60.0 * 60.0
    expected_reduction = 2.0 * 50.0 / arcmin_per_min
    assert runs[0.0] - runs[50.0] == pytest.approx(expected_reduction, rel=0.10)


def test_the_contradiction_is_what_the_shared_radius_prevents() -> None:
    """Aim without the size and report with it: the page argues with itself."""
    from ministar.pointing import best_start, frame_advice

    optics = _fake_optics()
    ra, dec = 56.87, 24.12
    start = best_start(target_ra_deg=ra, target_dec_deg=dec, optics=optics, pa_y_deg=0.0)
    advice = frame_advice(
        ra_deg=start[0],
        dec_deg=start[1],
        target_ra_deg=ra,
        target_dec_deg=dec,
        optics=optics,
        pa_y_deg=0.0,
        target_radius_arcmin=50.0,
    )

    assert not advice.inside
    assert "NOT fully in the frame" in " ".join(advice.describe())
    assert "across" in " ".join(advice.describe()), "say why, not just that"


def test_the_entry_tolerance_is_five_arcmin_or_five_percent() -> None:
    """10 §5: a fixed 5' is a third of a pixel at 400 mm and most of a frame at 14 mm."""
    from ministar.optics import Optics
    from ministar.pointing import entry_tolerance_arcmin

    wide = Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=4.434, focal_length_mm=14.0)
    mid = Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=4.434, focal_length_mm=400.0)
    long = Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=4.434, focal_length_mm=1000.0)
    assert entry_tolerance_arcmin(wide) == wide.height_arcmin * 0.05
    # 400 mm: 5% of a 2.2-degree-high frame is 6.6', so the frame term wins ...
    assert entry_tolerance_arcmin(mid) == mid.height_arcmin * 0.05
    # ... and at 1 m the frame is small enough that the 5' floor takes over.
    assert entry_tolerance_arcmin(long) == 5.0


def test_at_entry_accepts_the_best_start_and_rejects_a_nudge() -> None:
    from ministar.optics import Optics
    from ministar.pointing import at_entry, best_start

    optics = Optics.from_pitch(width_px=480, height_px=320, pixel_um=4.434, focal_length_mm=135.0)
    entry_ra, entry_dec = best_start(
        target_ra_deg=100.0, target_dec_deg=45.0, optics=optics, pa_y_deg=20.0
    )
    kwargs = dict(
        target_ra_deg=100.0,
        target_dec_deg=45.0,
        optics=optics,
        pa_y_deg=20.0,
    )
    assert at_entry(ra_deg=entry_ra, dec_deg=entry_dec, **kwargs) is True
    # A degree off is a whole frame at this focal length, not a nudge.
    assert at_entry(ra_deg=entry_ra + 1.0, dec_deg=entry_dec, **kwargs) is False


def test_the_entry_corner_is_named_in_image_terms() -> None:
    """Brief 10 §5: "top-left", never "east" -- the user is reading a screen."""
    from ministar.optics import Optics
    from ministar.pointing import entry_corner_name

    optics = Optics.from_pitch(width_px=480, height_px=320, pixel_um=4.434, focal_length_mm=135.0)
    name = entry_corner_name(target_ra_deg=100.0, target_dec_deg=45.0, optics=optics, pa_y_deg=20.0)
    assert name in {
        "top-left",
        "top-right",
        "bottom-left",
        "bottom-right",
        "top",
        "bottom",
        "left",
        "right",
    }


def test_corner_names_divide_the_frame_into_thirds() -> None:
    from ministar.pointing import corner_name

    assert corner_name(0, 0, 300, 300) == "top-left"
    assert corner_name(299, 299, 300, 300) == "bottom-right"
    assert corner_name(150, 150, 300, 300) == "centre"
    assert corner_name(150, 10, 300, 300) == "top"
    assert corner_name(10, 150, 300, 300) == "left"


def test_the_interval_start_centres_the_target_in_the_retained_frame() -> None:
    """The point of the offset: same retained area, subject in the middle not the corner."""
    import pytest as _pytest

    from ministar.optics import Optics
    from ministar.pointing import best_start
    from ministar.projection import TangentPlane

    optics = Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=4.434, focal_length_mm=135.0)
    target = (56.87, 24.1)  # M45
    interval = 300.0
    start_ra, start_dec = best_start(
        target_ra_deg=target[0],
        target_dec_deg=target[1],
        optics=optics,
        pa_y_deg=0.0,
        interval_s=interval,
    )
    plane = TangentPlane(
        ra_deg=start_ra,
        dec_deg=start_dec,
        pa_y_deg=0.0,
        plate_scale_arcsec=optics.plate_scale_arcsec,
        width_px=optics.width_px,
        height_px=optics.height_px,
    )
    x, _y = plane.project([target[0]], [target[1]])
    drift = optics.drift_px(dec_deg=target[1], seconds=interval)

    # North-up drift is horizontal, so the retained rectangle is [0, W-1-d] x [0, H-1].
    retained_centre = (optics.width_px - 1 - drift) / 2.0
    assert float(x[0]) == _pytest.approx(retained_centre, abs=3.0)
    # ... and the end is symmetric about the frame centre: equal margins left and right.
    end = float(x[0]) + drift
    assert abs(float(x[0]) - ((optics.width_px - 1) - end)) < 6.0


def test_without_an_interval_the_target_starts_at_the_entry_edge() -> None:
    from ministar.optics import Optics
    from ministar.pointing import best_start
    from ministar.projection import TangentPlane

    optics = Optics.from_pitch(width_px=5184, height_px=3456, pixel_um=4.434, focal_length_mm=135.0)
    start_ra, start_dec = best_start(
        target_ra_deg=56.87, target_dec_deg=24.1, optics=optics, pa_y_deg=0.0
    )
    plane = TangentPlane(
        ra_deg=start_ra,
        dec_deg=start_dec,
        pa_y_deg=0.0,
        plate_scale_arcsec=optics.plate_scale_arcsec,
        width_px=optics.width_px,
        height_px=optics.height_px,
    )
    x, _y = plane.project([56.87], [24.1])
    assert 0.0 <= float(x[0]) < 0.05 * optics.width_px  # the margin, not the middle
