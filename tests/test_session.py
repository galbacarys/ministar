"""The session wizard state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from ministar.session import Phase, SessionState


def test_fresh_session_starts_at_preflight() -> None:
    assert SessionState().phase is Phase.PREFLIGHT


def test_preflight_leads_to_setup() -> None:
    """Software readiness first, then rig readiness."""
    state = SessionState()
    state.advance()  # PREFLIGHT -> SETUP needs nothing
    assert state.phase is Phase.SETUP


def test_setup_needs_a_site_before_target() -> None:
    """Without a location every visibility column is unknown, so it is a blocker."""
    state = SessionState(phase=Phase.SETUP)
    assert state.blockers() == ["observing site not set (SETUP)"]
    with pytest.raises(ValueError, match="observing site"):
        state.advance()

    state.site_ready = True
    state.advance()
    assert state.phase is Phase.TARGET


def test_cannot_advance_without_a_target() -> None:
    state = SessionState(phase=Phase.SETUP, site_ready=True)
    state.advance()  # SETUP -> TARGET needs a site, which is now set
    assert state.phase is Phase.TARGET
    assert state.blockers() == ["no target selected"]
    with pytest.raises(ValueError, match="no target"):
        state.advance()


def test_full_path_to_running() -> None:
    state = SessionState()
    state.advance()  # -> SETUP
    state.site_ready = True
    state.advance()  # -> TARGET
    state.target_name = "M81"
    state.target_ra_deg, state.target_dec_deg = 148.888, 69.065
    state.advance()  # -> PARAMETERS
    state.reframe_interval_s = 600.0
    state.usable_mp = 13.0
    state.advance()  # -> FOCUS
    assert state.phase is Phase.FOCUS

    # Focus must happen before solving: a defocused frame will not plate solve.
    assert state.blockers() == ["camera not focused yet"]
    state.focus_position = 5030
    state.advance()  # -> POINT
    assert state.blockers() == ["not plate solved yet"]
    state.solved = True
    state.entry_corner = "top-left"
    state.advance()  # -> RUN
    assert state.phase is Phase.RUN
    assert state.can_advance()


def test_focus_is_required_before_pointing() -> None:
    """The ordering is a real dependency, not a preference."""
    state = SessionState(phase=Phase.FOCUS)
    assert not state.can_advance()
    state.focus_position = 0
    assert state.can_advance()


def test_restart_keeps_equipment_settings() -> None:
    state = SessionState(
        phase=Phase.RUN,
        target_name="M81",
        reframe_interval_s=600.0,
        focal_length_mm=400.0,
        solved=True,
        entry_corner="top-left",
    )
    state.restart()
    assert state.phase is Phase.TARGET
    assert not state.solved
    assert state.entry_corner is None
    assert state.focal_length_mm == 400.0  # not the user's problem to re-enter
    assert state.target_name == "M81"  # still selected, just re-enterable


def test_persistence_roundtrip(tmp_path: Path) -> None:
    state = SessionState(
        phase=Phase.POINT,
        target_name="Jellyfish",
        reframe_interval_s=480.0,
        focus_position=5030,
        focus_hfd=2.1,
        solved=True,
        advice=["rotate the camera 32 degrees"],
    )
    path = tmp_path / "session.json"
    state.save(path)

    restored = SessionState.load(path)
    assert restored.phase is Phase.POINT
    assert restored.target_name == "Jellyfish"
    assert restored.focus_position == 5030
    assert restored.advice == ["rotate the camera 32 degrees"]


def test_missing_and_corrupt_state_fall_back_cleanly(tmp_path: Path) -> None:
    assert SessionState.load(tmp_path / "nope.json").phase is Phase.PREFLIGHT
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert SessionState.load(bad).phase is Phase.PREFLIGHT


def test_unknown_phase_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"phase": "teleporting"}', encoding="utf-8")
    assert SessionState.load(path).phase is Phase.PREFLIGHT


def test_summary_is_readable() -> None:
    state = SessionState(
        phase=Phase.RUN, target_name="M81", reframe_interval_s=600.0, usable_mp=13.0
    )
    assert state.summary() == "run  |  M81  |  reframe 10 min  |  13.0 MP"
