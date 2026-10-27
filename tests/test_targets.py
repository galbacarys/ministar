"""Filtering and ranking the catalogue, without a UI or a real night."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ministar.catalog import Target
from ministar.site import Site
from ministar.targets import Filters, Row, build_rows, top, verdict_for

SITE = Site(latitude_deg=45.0, longitude_deg=0.0)
START = datetime(2026, 10, 27, 0, tzinfo=UTC)
END = datetime(2026, 10, 27, 8, tzinfo=UTC)


def catalogue() -> list[Target]:
    return [
        # Circumpolar from 45 N: up all night.
        Target(
            name="NGC0001",
            type="G",
            ra_deg=0.0,
            dec_deg=80.0,
            v_mag=8.0,
            major_arcmin=20.0,
            surface_brightness=21.0,
        ),
        # Never rises from 45 N.
        Target(
            name="NGC0002",
            type="G",
            ra_deg=0.0,
            dec_deg=-80.0,
            v_mag=9.0,
            major_arcmin=10.0,
            surface_brightness=21.0,
        ),
        # Up, but very faint surface brightness.
        Target(
            name="NGC0003",
            type="G",
            ra_deg=6.0,
            dec_deg=60.0,
            b_mag=12.0,
            major_arcmin=3.0,
            surface_brightness=24.0,
        ),
    ]


# -- verdict wording --------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"visible_minutes": 0.0, "max_altitude_deg": 60.0}, "not up tonight"),
        ({"visible_minutes": 100.0, "max_altitude_deg": 10.0}, "grazes the horizon"),
        (
            # "Huge" is now frame-relative: without optics there is no frame to be
            # bigger than, so an oversized arcminute size says nothing by itself.
            {"visible_minutes": 100.0, "max_altitude_deg": 60.0, "size_arcmin": 500.0},
            "brightness unknown",
        ),
        (
            {"visible_minutes": 100.0, "max_altitude_deg": 60.0, "size_arcmin": 0.5},
            "tiny, will be a dot",
        ),
        (
            {"visible_minutes": 100.0, "max_altitude_deg": 60.0, "fill": 1.3},
            "bigger than your frame",
        ),
        (
            {
                "visible_minutes": 100.0,
                "max_altitude_deg": 60.0,
                "size_px": 8.0,
                "fill": 0.05,
            },
            "tiny, will be a dot",
        ),
        (
            # 54 s is M31 at 400 mm on the equator: real, and useless to automate.
            {
                "visible_minutes": 100.0,
                "max_altitude_deg": 60.0,
                "fill": 0.93,
                "window_seconds": 54.0,
            },
            "short reframe window",
        ),
        (
            {
                "visible_minutes": 100.0,
                "max_altitude_deg": 60.0,
                "fill": 0.93,
                "window_seconds": 54.0,
                "min_window_seconds": 30.0,
            },
            "brightness unknown",
        ),
        (
            {
                "visible_minutes": 100.0,
                "max_altitude_deg": 60.0,
                "size_arcmin": 20.0,
                "surface_brightness": 25.0,
            },
            "faint surface brightness",
        ),
        ({"visible_minutes": 100.0, "max_altitude_deg": 60.0}, "brightness unknown"),
        (
            {"visible_minutes": 100.0, "max_altitude_deg": 60.0, "magnitude": 8.0},
            "good candidate",
        ),
    ],
)
def test_verdict_wording(kwargs: dict, expected: str) -> None:
    assert verdict_for(**kwargs) == expected


# -- building rows ----------------------------------------------------------
def test_an_incomplete_site_is_refused() -> None:
    with pytest.raises(ValueError, match="complete site"):
        build_rows(catalogue(), site=Site(), start=START, end=END)


def test_visibility_is_computed_per_target() -> None:
    rows = {
        row.target.name: row for row in build_rows(catalogue(), site=SITE, start=START, end=END)
    }

    circumpolar = rows["NGC0001"]
    never = rows["NGC0002"]

    assert circumpolar.visible_minutes == pytest.approx(480.0)  # the whole window
    assert never.visible_minutes == pytest.approx(0.0)
    assert never.verdict == "not up tonight"
    assert circumpolar.max_altitude_deg > never.max_altitude_deg


def test_minimum_visible_minutes_filters() -> None:
    rows = build_rows(
        catalogue(), site=SITE, start=START, end=END, filters=Filters(min_visible_minutes=60)
    )
    assert {row.target.name for row in rows} == {"NGC0001", "NGC0003"}


def test_magnitude_filter_cuts_on_the_approximate_visual_scale() -> None:
    rows = build_rows(
        catalogue(), site=SITE, start=START, end=END, filters=Filters(max_magnitude=10.0)
    )
    names = {row.target.name for row in rows}
    # NGC0003 is B=12, i.e. about 11.3 once shifted to the visual scale: too faint
    # either way, which is the point -- the shift must not rescue a hopeless object.
    assert names == {"NGC0001", "NGC0002"}


def test_a_blue_only_row_is_not_penalised_for_its_band() -> None:
    """B reads ~0.7 mag fainter for the same object, so a raw cut would drop this."""
    blue_only = Target(
        name="B-ONLY",
        type="G",
        ra_deg=0.0,
        dec_deg=80.0,
        b_mag=10.4,
        surface_brightness=20.0,
    )
    assert blue_only.magnitude == 10.4, "the raw catalogue value stays raw"

    rows = build_rows(
        [blue_only],
        site=SITE,
        start=START,
        end=END,
        filters=Filters(max_magnitude=10.0, min_visible_minutes=0.0),
    )
    assert [row.target.name for row in rows] == ["B-ONLY"]
    # The row carries the same converted number the filter used, so the table cannot
    # show 10.4 for a row that was admitted as 9.7.
    assert rows[0].magnitude == pytest.approx(9.69, abs=0.005)


def test_an_object_without_any_magnitude_is_excluded_by_a_magnitude_filter() -> None:
    unknown = [Target(name="X", type="G", ra_deg=0.0, dec_deg=80.0)]
    assert (
        build_rows(unknown, site=SITE, start=START, end=END, filters=Filters(max_magnitude=12.0))
        == []
    )


def test_surface_brightness_filter() -> None:
    rows = build_rows(
        catalogue(),
        site=SITE,
        start=START,
        end=END,
        filters=Filters(max_surface_brightness=22.0),
    )
    assert "NGC0003" not in {row.target.name for row in rows}


def test_size_filter() -> None:
    rows = build_rows(
        catalogue(), site=SITE, start=START, end=END, filters=Filters(min_size_arcmin=15.0)
    )
    assert {row.target.name for row in rows} == {"NGC0001"}


def test_type_filter() -> None:
    rows = build_rows(
        catalogue(), site=SITE, start=START, end=END, filters=Filters(types=frozenset({"PN"}))
    )
    assert rows == []


def test_search_matches_the_name() -> None:
    rows = build_rows(
        catalogue(), site=SITE, start=START, end=END, filters=Filters(search="ngc0002")
    )
    assert {row.target.name for row in rows} == {"NGC0002"}


# -- ranking ----------------------------------------------------------------
def test_top_prefers_good_candidates_and_respects_the_limit() -> None:
    def make(name: str, verdict_rank: int, magnitude: float | None) -> Row:
        return Row(
            target=Target(name=name, type="G", ra_deg=0.0, dec_deg=0.0),
            magnitude=magnitude,
            size_arcmin=None,
            max_altitude_deg=60.0,
            visible_minutes=100.0,
            verdict="x",
            rank=verdict_rank,
        )

    rows = [make("poor", 6, 5.0), make("good-dim", 0, 9.0), make("good-bright", 0, 6.0)]
    ordered = [row.target.name for row in top(rows, 2)]
    assert ordered == ["good-bright", "good-dim"]


# -- frame-relative rows ----------------------------------------------------
def optics_at(focal_mm: float):
    from ministar.optics import Optics

    return Optics.from_pitch(width_px=5202, height_px=3464, pixel_um=4.3, focal_length_mm=focal_mm)


def test_rows_carry_the_frame_arithmetic_when_optics_are_known() -> None:
    optics = optics_at(200)
    rows = build_rows(catalogue(), site=SITE, start=START, end=END, optics=optics)
    ngc1 = next(row for row in rows if row.target.name == "NGC0001")  # 20' at dec 80

    # The fixture has no minor axis, so Optics assumes circular and the SHORT axis
    # binds. That is the conservative reading, and it is the one the row reports.
    assert ngc1.fill == pytest.approx(20 * 60 / optics.height_arcsec, rel=0.01)
    assert ngc1.size_px == pytest.approx(20 * 60 / optics.plate_scale_arcsec, rel=0.01)
    assert ngc1.window_seconds is not None and ngc1.window_seconds > 0


def test_without_optics_the_frame_columns_stay_empty() -> None:
    """No rig profile means no measurement -- absent, not an invented default."""
    rows = build_rows(catalogue(), site=SITE, start=START, end=END)

    assert rows, "the rows themselves must still be produced"
    assert all(
        row.fill is None and row.window_seconds is None and row.size_px is None for row in rows
    )


def test_the_window_filter_drops_a_target_it_can_judge() -> None:
    """A 180' object at 400 mm fills 94% of the frame: ~4 minutes at dec 80, seconds at 0."""
    huge = Target(
        name="WIDE",
        type="G",
        ra_deg=0.0,
        dec_deg=80.0,
        v_mag=6.0,
        major_arcmin=180.0,
        minor_arcmin=60.0,
        surface_brightness=20.0,
    )
    filters = Filters(min_window_seconds=300.0, min_visible_minutes=0.0)

    kept = build_rows(
        [huge], site=SITE, start=START, end=END, filters=filters, optics=optics_at(400)
    )
    assert kept == []

    generous = build_rows(
        [huge], site=SITE, start=START, end=END, filters=filters, optics=optics_at(100)
    )
    assert [row.target.name for row in generous] == ["WIDE"]


def test_the_window_filter_cannot_drop_what_it_cannot_measure() -> None:
    """Without optics every row passes: silence is not evidence of discomfort."""
    huge = Target(name="WIDE", type="G", ra_deg=0.0, dec_deg=80.0, v_mag=6.0, major_arcmin=180.0)

    rows = build_rows(
        [huge],
        site=SITE,
        start=START,
        end=END,
        filters=Filters(min_window_seconds=300.0, min_visible_minutes=0.0),
    )

    assert [row.target.name for row in rows] == ["WIDE"]
