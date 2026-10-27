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


def test_type_dropdown_choices_map_to_filters() -> None:
    """'(all)' must genuinely mean all, including the stars the default hides."""
    from ministar.catalog import DEFAULT_TARGET_TYPES
    from ministar.ui.screens import ALL_TYPES, DEEP_SKY_PRESET, types_for

    assert types_for(None) is None
    assert types_for("") is None
    assert types_for(ALL_TYPES) is None
    assert types_for(DEEP_SKY_PRESET) == frozenset(DEFAULT_TARGET_TYPES)
    assert types_for("PN") == frozenset({"PN"})
