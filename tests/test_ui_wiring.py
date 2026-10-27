"""Wiring checks for the UI shell.

These catch the failures that are invisible until someone clicks: a phase with no
screen, a screen reading an attribute the session does not have, a preflight status
with no glyph. None of them need a browser, and all of them are cheap.
"""

from __future__ import annotations

import dataclasses

from ministar import preflight as preflight_checks
from ministar.config import Settings
from ministar.session import ORDER, Phase, SessionState
from ministar.ui import screens

#: Session fields the screens write through _field(); a typo here would only
#: surface on the page that used it.
USED_FIELDS = (
    "target_name",
    "target_ra_deg",
    "target_dec_deg",
    "focal_length_mm",
    "exposure_s",
    "iso",
    "reframe_interval_s",
    "focus_position",
    "focus_hfd",
)


def test_every_phase_has_a_screen() -> None:
    missing = [phase.name for phase in ORDER if phase not in screens.RENDERERS]
    assert not missing, f"phases with no screen: {missing}"


def test_no_screen_for_a_phase_outside_the_order() -> None:
    extra = [phase.name for phase in screens.RENDERERS if phase not in ORDER]
    assert not extra, f"screens registered for unlisted phases: {extra}"


def test_screens_only_touch_real_session_fields() -> None:
    available = {field.name for field in dataclasses.fields(SessionState)}
    unknown = [name for name in USED_FIELDS if name not in available]
    assert not unknown, f"screens reference missing fields: {unknown}"


def test_status_glyphs_cover_every_preflight_status() -> None:
    emitted = {check.status for check in preflight_checks.run(Settings.load())}
    assert emitted <= set(screens.STATUS_GLYPH), (
        f"no glyph for {emitted - set(screens.STATUS_GLYPH)}"
    )


def test_a_new_session_starts_at_the_first_phase() -> None:
    state = SessionState()
    assert state.phase is ORDER[0]
    assert state.phase is Phase.PREFLIGHT
    assert isinstance(state.can_advance(), bool)


def test_the_magnitude_column_folds_the_band_in() -> None:
    """One approximate scale, so "V" and "B" never reach the table as their own field."""
    columns = {column["name"]: column for column in screens.TARGET_COLUMNS}

    assert "band" not in columns, "the band belongs inside the number, not beside it"
    assert columns["mag"]["label"] == "approx. mag"
    assert columns["mag"]["sortable"] is True, "a renamed column must still sort"


def test_both_misreadable_headings_carry_their_explanation() -> None:
    known = {column["name"] for column in screens.TARGET_COLUMNS}
    unknown = set(screens.COLUMN_HELP) - known
    assert not unknown, f"help for columns that do not exist: {unknown}"
    assert {"mag", "sb"} <= set(screens.COLUMN_HELP)


def test_a_helpful_header_cell_stays_sortable() -> None:
    """A hand-rolled <th> silently drops sorting, the arrow and aria-sort with it."""
    html = screens._header_slot("approx. mag", "brightness")

    assert ':props="props"' in html, "q-th must receive the table's slot props"
    assert "approx. mag" in html
    assert "<q-tooltip" in html and "brightness" in html


def test_type_dropdown_choices_map_to_filters() -> None:
    """'(all)' must genuinely mean all, including the stars the default hides."""
    from ministar.catalog import DEFAULT_TARGET_TYPES
    from ministar.ui.screens import ALL_TYPES, DEEP_SKY_PRESET, types_for

    assert types_for(None) is None
    assert types_for("") is None
    assert types_for(ALL_TYPES) is None
    assert types_for(DEEP_SKY_PRESET) == frozenset(DEFAULT_TARGET_TYPES)
    assert types_for("PN") == frozenset({"PN"})


# -- the frame-fit UI -------------------------------------------------------
def _row(verdict: str, *, window=None) -> object:
    from ministar.catalog import Target
    from ministar.targets import Row

    return Row(
        target=Target(name="X", type="G", ra_deg=0.0, dec_deg=45.0, major_arcmin=60.0),
        magnitude=8.0,
        size_arcmin=60.0,
        max_altitude_deg=60.0,
        visible_minutes=120.0,
        verdict=verdict,
        rank=0,
        window_seconds=window,
    )


def test_the_window_column_is_present_and_in_seconds() -> None:
    columns = {column["name"]: column for column in screens.TARGET_COLUMNS}

    assert columns["window"]["label"] == "window (s)"
    assert columns["window"]["sortable"] is True


def test_rows_that_will_not_work_are_dimmed_rather_than_hidden() -> None:
    """ "inform, never block": still selectable, visibly de-emphasised."""
    dimmed = screens._table_row(0, _row("short reframe window", window=54.0))
    fine = screens._table_row(1, _row("good candidate", window=900.0))

    assert dimmed["dim"] is True
    assert fine["dim"] is False
    assert dimmed["verdict"] == "short reframe window", "the row still exists"


def test_an_endless_window_is_a_sentinel_not_infinity() -> None:
    """JSON has no inf, and a pole target legitimately never leaves the frame."""
    endless = screens._table_row(0, _row("good candidate", window=float("inf")))
    unknown = screens._table_row(1, _row("good candidate", window=None))

    assert endless["window"] == screens._NO_LIMIT_WINDOW
    assert unknown["window"] == screens._NO_LIMIT_WINDOW


def test_the_window_hint_states_the_declination_it_assumed() -> None:
    from ministar.optics import Optics

    optics = Optics.from_pitch(width_px=5202, height_px=3464, pixel_um=4.3, focal_length_mm=200)
    text = screens.window_hint_for(optics, 300.0)

    assert "dec 45" in text, "a global control must say which declination it used"
    assert "'" in text, "it quotes a size, which is what a person recognises"
    assert "5 min" not in text, "the slider handle already reads in minutes"


def test_without_a_profile_the_hint_admits_it() -> None:
    assert "no rig profile" in screens.window_hint_for(None, 300.0)


def test_the_slider_is_in_minutes_and_stops_where_the_sky_does() -> None:
    """A slider labelled "60" for one minute is a unit bug, not a preference."""
    from ministar.optics import Optics

    wide = Optics.from_pitch(width_px=5202, height_px=3464, pixel_um=4.3, focal_length_mm=24)
    long = Optics.from_pitch(width_px=5202, height_px=3464, pixel_um=4.3, focal_length_mm=400)

    assert screens.window_slider_max_minutes(None) == 60, "no rig: the full hour stands"
    assert screens.window_slider_max_minutes(wide) == 60, "a 24 mm frame spans 51 min"
    # At 400 mm a point source crosses the frame in 12.8 min, so 60 is a lie.
    assert screens.window_slider_max_minutes(long) == 12


def test_the_parameters_limit_is_in_minutes_and_never_zero() -> None:
    assert screens.interval_slider_max_minutes(None) == 60
    assert screens.interval_slider_max_minutes(276 * 60) == 60, "capped at an hour"
    assert screens.interval_slider_max_minutes(71.0) == 1, "M31 at 400 mm: 1 min, not 0"
    assert screens.interval_slider_max_minutes(16531.0) == 60


def test_a_selected_target_carries_its_extent_for_the_parameters_screen() -> None:
    """PARAMETERS clamps its slider from state, without reloading the catalogue."""
    from ministar.session import SessionState

    fields = {field.name for field in dataclasses.fields(SessionState)}
    assert {"target_major_arcmin", "target_minor_arcmin"} <= fields
