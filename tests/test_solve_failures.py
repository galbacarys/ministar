"""Two failed solves stop the page offering a third: it offers a retry instead."""

from __future__ import annotations

import types
from pathlib import Path

from ministar import quality
from ministar.session import SessionState
from ministar.ui import screens


class _FailingCamera:
    """A camera whose capture always fails -- the cheapest way to spend a solve attempt."""

    disposable_frames = True

    def capture_to(self, path):
        raise RuntimeError("no frame for you")


def _context(tmp_path: Path):
    paths = types.SimpleNamespace(frames=tmp_path, scratch=tmp_path)
    settings = types.SimpleNamespace(astap=tmp_path / "astap_cli.exe", dcraw=None, paths=paths)
    station = types.SimpleNamespace(camera=_FailingCamera())
    state = SessionState()
    return types.SimpleNamespace(
        state=state,
        settings=settings,
        station=station,
        refresh=lambda: None,
        rig=None,
    )


def test_a_failed_solve_counts_and_two_of_them_hold_the_page_off(tmp_path: Path) -> None:
    ctx = _context(tmp_path)

    report, note = screens._solve(ctx)
    assert report is None and "failed" in note.lower()
    assert ctx.state.solve_failures == 1
    assert not quality.should_hold_off(ctx.state.solve_failures)

    screens._solve(ctx)
    assert ctx.state.solve_failures == 2
    assert quality.should_hold_off(ctx.state.solve_failures), "now the retry would show"


def test_refusing_to_capture_is_not_a_solve_failure(tmp_path: Path) -> None:
    """A moving mount is not the solver's fault, and must not count towards giving up."""
    ctx = _context(tmp_path)

    class _Moving:
        moving = True

        def describe(self) -> str:
            return "mount moving (9.0 levels/s)"

    report, note = screens._solve(ctx, _Moving())

    assert report is None and "settle" in note
    assert ctx.state.solve_failures == 0


def test_the_counter_survives_a_round_trip_through_the_session_file(tmp_path: Path) -> None:
    """A resumed session should not forget that the solver has already given up twice."""
    state = SessionState(solve_failures=2)
    again = SessionState.from_dict(state.to_dict())

    assert again.solve_failures == 2
    assert quality.should_hold_off(again.solve_failures)
