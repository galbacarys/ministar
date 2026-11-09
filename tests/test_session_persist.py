"""Session state survives a process restart.

The wizard lives in a server now, so a restart is not an abstract event: it happens
on every service upgrade, every reboot, and every time the operator stops the console
to run a camera test. Before this, all of that reset the wizard to PREFLIGHT.

Two rules matter here:

* the file is JSON, so the datetime in a solved session must be encoded explicitly;
* a RUN cannot be resumed by loading it -- the process that owned the capture loop is
  gone. The honest resume point is POINT.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ministar.session import Phase, SessionState


def test_a_solved_session_round_trips_through_json(tmp_path: Path) -> None:
    when = datetime(2026, 11, 9, 22, 15, tzinfo=UTC)
    state = SessionState(
        phase=Phase.POINT,
        site_ready=True,
        target_name="Andromeda Galaxy",
        target_code="M31",
        target_ra_deg=10.6847,
        target_dec_deg=41.269,
        exposure_s=2.0,
        iso=800,
        stack_bin=4,
        solved=True,
        pointing_ra_deg=10.7,
        pointing_dec_deg=41.3,
        pointing_pa_deg=-12.5,
        pointing_scale_arcsec=9.0,
        pointing_solved_at=when,
        advice=["rotate the camera 32 degrees"],
    )
    path = tmp_path / "session.json"
    state.save(path)

    # The raw file must be valid JSON, not a repr with a datetime object in it.
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pointing_solved_at"] == when.isoformat()

    loaded = SessionState.load(path)
    assert loaded.phase is Phase.POINT
    assert loaded.target_code == "M31"
    assert loaded.exposure_s == 2.0
    assert loaded.stack_bin == 4
    assert loaded.pointing_solved_at == when
    assert loaded.advice == ["rotate the camera 32 degrees"]


def test_a_run_resumes_at_point_with_a_note() -> None:
    state = SessionState(phase=Phase.RUN, solved=True)
    note = state.prepare_resume()

    assert state.phase is Phase.POINT
    assert "re-point" in note
    assert note in state.advice

    # Calling it again after a page refresh must not duplicate the note.
    assert state.prepare_resume() == ""
    assert state.advice.count(note) == 1


def test_a_non_run_phase_is_left_alone() -> None:
    for phase in (Phase.PREFLIGHT, Phase.SETUP, Phase.FOCUS, Phase.POINT, Phase.DONE):
        state = SessionState(phase=phase)
        assert state.prepare_resume() == ""
        assert state.phase is phase


def test_an_unknown_phase_falls_back_to_preflight() -> None:
    state = SessionState.from_dict({"phase": "not-a-phase", "site_ready": True})
    assert state.phase is Phase.PREFLIGHT
    # Other known fields still survive.
    assert state.site_ready is True


def test_a_missing_or_corrupt_file_is_a_fresh_session(tmp_path: Path) -> None:
    assert SessionState.load(tmp_path / "missing.json").phase is Phase.PREFLIGHT

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert SessionState.load(corrupt).phase is Phase.PREFLIGHT

    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2, 3]", encoding="utf-8")
    assert SessionState.load(wrong_shape).phase is Phase.PREFLIGHT


def test_an_unparseable_solve_time_is_dropped_not_fatal() -> None:
    state = SessionState.from_dict({"phase": "point", "pointing_solved_at": "yesterday"})
    assert state.phase is Phase.POINT
    assert state.pointing_solved_at is None
