"""Two failed solves stop the page offering a third: it offers a retry instead."""

from __future__ import annotations

import types
from pathlib import Path

from ministar import quality
from ministar.camera.base import CameraSettings
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

        def steady_for(self, seconds: float, now=None) -> bool:
            return False

    report, note = screens._solve(ctx, _Moving())

    assert report is None and "held still" in note
    assert ctx.state.solve_failures == 0


def test_a_solve_sets_its_own_exposure_and_restores_it(tmp_path: Path) -> None:
    """The dials are not a solve exposure: a short preview setting starves ASTAP."""

    class _RecordingCamera(_FailingCamera):
        def __init__(self) -> None:
            self.applied: list[CameraSettings] = []
            self.captured = 0

        def get_settings(self) -> CameraSettings:
            return CameraSettings(iso=400, shutter_seconds=0.25)

        def apply(self, settings: CameraSettings) -> None:
            self.applied.append(settings)

        def capture_to(self, path):
            self.captured += 1
            raise RuntimeError("stop after the shutter")

    camera = _RecordingCamera()
    ctx = _context(tmp_path)
    ctx.station = types.SimpleNamespace(
        camera=camera,
        apply=camera.apply,
        get_settings=camera.get_settings,
        capture_to=camera.capture_to,
    )

    screens._solve(ctx)

    assert camera.captured == 1
    assert camera.applied[0] == CameraSettings(
        iso=screens.SOLVE_ISO_DEFAULT, shutter_seconds=screens.SOLVE_EXPOSURE_S
    )
    # Even a failed capture must hand the operator's setting back.
    assert camera.applied[-1] == CameraSettings(iso=400, shutter_seconds=0.25)


def test_auto_solve_waits_for_whichever_is_longer() -> None:
    """The instruction grace and the settle overlap, not queue."""
    # Stopped early: still waits for the operator's recheck interval.
    assert (
        screens.auto_solve_ready_at(
            instruction_at=0.0, last_motion_at=1.0, recheck_s=5.0, settle_s=2.0
        )
        == 5.0
    )
    # Still adjusting past the grace: waits for the settle instead.
    assert (
        screens.auto_solve_ready_at(
            instruction_at=0.0, last_motion_at=6.0, recheck_s=5.0, settle_s=2.0
        )
        == 8.0
    )


def test_the_counter_survives_a_round_trip_through_the_session_file(tmp_path: Path) -> None:
    """A resumed session should not forget that the solver has already given up twice."""
    state = SessionState(solve_failures=2)
    again = SessionState.from_dict(state.to_dict())

    assert again.solve_failures == 2
    assert quality.should_hold_off(again.solve_failures)
