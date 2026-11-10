"""The RUN loop, driven by the fake camera.

The loop is deliberately thin, so what these tests defend is mostly *lifecycle*: frames
paced rather than free-running, a pause that actually stops the shutter, an error that is
reported rather than raised in a thread nobody sees, and -- the user's explicit
requirement -- fake frames going to a temporary directory that does not survive the run.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from ministar.run import RunConfig, Runner


def wait_until(predicate, *, timeout: float = 15.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition never became true")


def make_runner(
    sky, tmp_path: Path, *, keep_frames: bool = False, capture=None, resolve=None
) -> Runner:
    return Runner(
        capture=capture or sky.camera.capture_to,
        camera=sky.camera,
        model=sky.model,
        resolve=resolve,
        clock=lambda: sky.when,
        config=RunConfig(
            exposure_s=0.02,
            iso=800,
            bin_factor=2,
            keep_frames=keep_frames,
            target_name="test",
        ),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
    )


def test_fake_frames_are_temporary_and_the_stack_survives_them(sky, tmp_path) -> None:
    runner = make_runner(sky, tmp_path, keep_frames=False)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 3)
        snapshot = runner.snapshot()
        assert snapshot.state == "running"
        assert snapshot.stats is not None
        assert snapshot.stats.accepted >= 1
        assert snapshot.data_uri.startswith("data:image/jpeg;base64,")
        assert list((tmp_path / "scratch").glob("ministar-run-*"))
        assert snapshot.frames_dir.startswith(str(tmp_path / "scratch"))
    finally:
        runner.stop()

    snapshot = runner.snapshot()
    assert snapshot.state == "stopped"
    assert snapshot.stats is not None and snapshot.stats.accepted >= 1
    assert not list((tmp_path / "scratch").glob("ministar-run-*"))
    assert not list((tmp_path / "frames").glob("*"))


def test_a_real_run_keeps_its_originals(sky, tmp_path) -> None:
    runner = make_runner(sky, tmp_path, keep_frames=True)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 2)
    finally:
        runner.stop()
    kept = sorted((tmp_path / "frames").glob("*.fit"))
    assert len(kept) >= 2
    snapshot = runner.snapshot()
    assert snapshot.kept_frames >= 2
    assert str(tmp_path / "frames") in snapshot.frames_dir


def test_pause_stops_the_shutter_and_resume_restarts_it(sky, tmp_path) -> None:
    runner = make_runner(sky, tmp_path)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 2)
        runner.pause()
        assert runner.snapshot().state == "paused"
        paused_at = runner.snapshot().frame
        time.sleep(0.8)
        assert runner.snapshot().frame == paused_at

        runner.resume()
        wait_until(lambda: runner.snapshot().frame > paused_at)
        assert runner.snapshot().state == "running"
    finally:
        runner.stop()


def test_an_error_is_reported_in_the_snapshot(sky, tmp_path) -> None:
    def broken(path):
        raise RuntimeError("the shutter jammed")

    runner = make_runner(sky, tmp_path, capture=broken)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().state == "error")
        snapshot = runner.snapshot()
        assert "the shutter jammed" in snapshot.error
        assert "RuntimeError" in snapshot.error
    finally:
        runner.stop()


def test_a_camera_that_cannot_take_settings_says_so(sky, tmp_path) -> None:
    """The 60D path raises NotImplementedError; the run must continue and admit it."""

    def refuse(settings):
        raise NotImplementedError("not yet")

    camera = SimpleNamespace(disposable_frames=True, apply=refuse)
    runner = Runner(
        capture=sky.camera.capture_to,
        camera=camera,
        model=sky.model,
        config=RunConfig(exposure_s=0.02, bin_factor=2, keep_frames=False),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
    )
    try:
        runner.start()
        snapshot = runner.snapshot()
        assert "cannot be told an exposure" in snapshot.warning
    finally:
        runner.stop()


def test_the_reframe_countdown_is_reported_when_one_was_chosen(sky, tmp_path) -> None:
    runner = Runner(
        capture=sky.camera.capture_to,
        camera=sky.camera,
        model=sky.model,
        config=RunConfig(exposure_s=0.02, bin_factor=2, keep_frames=False, reframe_interval_s=1.0),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
    )
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 1)
        due = runner.snapshot().reframe_due_in_s
        assert due is not None and due <= 1.0
    finally:
        runner.stop()


def test_the_countdown_is_the_object_edge_when_it_comes_first(sky, tmp_path) -> None:
    """The chosen interval is a cadence, not the truth: the target's position decides.

    At 135 mm the fake frame gives the target ~2.6 min from the centre to the edge. Asking
    for a one-hour interval must still count down that edge time, not 60:00.
    """
    ra, dec = sky.camera.pointing
    runner = Runner(
        capture=sky.camera.capture_to,
        camera=sky.camera,
        model=sky.model,
        config=RunConfig(
            exposure_s=0.02,
            bin_factor=2,
            keep_frames=False,
            reframe_interval_s=3600.0,
            target_ra_deg=ra,
            target_dec_deg=dec,
        ),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
        clock=lambda: sky.when,
    )
    try:
        runner.start()
        snapshot = runner.snapshot()
        assert snapshot.reframe_edge_in_s is not None
        assert 120.0 < snapshot.reframe_edge_in_s < 200.0
        assert snapshot.reframe_due_in_s is not None
        assert abs(snapshot.reframe_due_in_s - snapshot.reframe_edge_in_s) < 2.0
    finally:
        runner.stop()


def test_the_loop_reframes_when_the_object_reaches_the_edge(sky, tmp_path) -> None:
    """A target already outside the frame makes the physical window zero, so no frame runs."""
    ra, dec = sky.camera.pointing
    runner = Runner(
        capture=sky.camera.capture_to,
        camera=sky.camera,
        model=sky.model,
        config=RunConfig(
            exposure_s=0.02,
            bin_factor=2,
            keep_frames=False,
            reframe_interval_s=3600.0,
            target_ra_deg=ra + 1.0,
            target_dec_deg=dec,
        ),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
        clock=lambda: sky.when,
    )
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().state == "reframe", timeout=5)
        assert runner.snapshot().frame == 0
    finally:
        runner.stop()


def test_without_an_interval_there_is_no_countdown(sky, tmp_path) -> None:
    runner = make_runner(sky, tmp_path)
    runner.start()
    try:
        assert runner.snapshot().reframe_due_in_s is None
    finally:
        runner.stop()


def test_the_idle_snapshot_is_honest_about_having_nothing(sky, tmp_path) -> None:
    snapshot = make_runner(sky, tmp_path).snapshot()
    assert snapshot.state == "idle"
    assert snapshot.frame == 0
    assert snapshot.data_uri == ""
    assert snapshot.stats is not None and snapshot.stats.accepted == 0
    assert snapshot.reframe_due_in_s is None


def solved_now(sky):
    """What ASTAP would report: the fake camera's actual pointing, roll and scale."""
    ra, dec = sky.camera.pointing
    return SimpleNamespace(
        crval=(ra, dec),
        position_angle_deg=sky.camera.roll_deg,
        pixel_scale_arcsec=sky.optics.plate_scale_arcsec,
    )


def test_a_bumped_mount_is_re_solved_and_stacking_continues(sky, tmp_path) -> None:
    calls = []

    def resolve(path, when, model):
        calls.append(path)
        return solved_now(sky)

    runner = make_runner(sky, tmp_path, resolve=resolve)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 2)
        before = runner.snapshot().stats.accepted

        ra, dec = sky.camera.pointing
        sky.camera.set_pointing(ra + 0.1, dec)  # a hand on the tripod

        wait_until(lambda: runner.snapshot().recoveries >= 1)
        wait_until(lambda: runner.snapshot().stats.accepted > before)
        snapshot = runner.snapshot()
        assert calls, "the resolver was never asked"
        assert "would not solve" not in snapshot.warning
        assert snapshot.recoveries == 1
    finally:
        runner.stop()


def test_a_bump_that_will_not_solve_is_reported(sky, tmp_path) -> None:
    runner = make_runner(sky, tmp_path, resolve=lambda *_: None)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 2)
        ra, dec = sky.camera.pointing
        sky.camera.set_pointing(ra + 0.1, dec)
        wait_until(lambda: "mount moved" in runner.snapshot().warning)
        assert runner.snapshot().recoveries == 0
        assert "would not solve" in runner.snapshot().warning
    finally:
        runner.stop()


def test_a_solver_that_raises_is_a_message_not_a_crash(sky, tmp_path) -> None:
    def explode(path, when, model):
        raise RuntimeError("astap_cli.exe exited 1")

    runner = make_runner(sky, tmp_path, resolve=explode)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().frame >= 2)
        ra, dec = sky.camera.pointing
        sky.camera.set_pointing(ra + 0.1, dec)
        wait_until(lambda: "solver failed" in runner.snapshot().warning)
        assert runner.snapshot().state == "running"
    finally:
        runner.stop()


def make_interval_runner(sky, tmp_path, interval: float) -> Runner:
    return Runner(
        capture=sky.camera.capture_to,
        camera=sky.camera,
        model=sky.model,
        config=RunConfig(
            exposure_s=0.02, bin_factor=2, keep_frames=False, reframe_interval_s=interval
        ),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
        clock=lambda: sky.when,
    )


def test_the_loop_stops_at_the_reframe_interval(sky, tmp_path) -> None:
    runner = make_interval_runner(sky, tmp_path, 1.5)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().state == "reframe", timeout=10)
        stopped_at = runner.snapshot().frame
        assert stopped_at >= 1
        assert runner.snapshot().reframe_progress >= 0.99

        time.sleep(0.8)
        assert runner.snapshot().frame == stopped_at, "the shutter kept going during a reframe"

        runner.resume_reframe()
        wait_until(lambda: runner.snapshot().state == "running")
        assert runner.snapshot().reframe_progress < 0.5  # the countdown started over
        wait_until(lambda: runner.snapshot().frame > stopped_at)
    finally:
        runner.stop()


def test_resuming_a_reframe_rebases_onto_the_new_pointing(sky, tmp_path) -> None:
    """The mount moved by most of a frame: the next frames must land on the old canvas."""
    runner = make_interval_runner(sky, tmp_path, 0.6)
    try:
        runner.start()
        wait_until(lambda: runner.snapshot().state == "reframe", timeout=10)
        assert runner.snapshot().stats.accepted >= 1

        # The user re-points while the loop is stopped, so no frame is taken mid-move.
        ra, dec = sky.camera.pointing
        sky.camera.set_pointing(ra + 0.1, dec)
        assert runner.snapshot().stats.rejected == 0

        rebased = runner.resume_reframe(
            ra_deg=sky.camera.pointing[0],
            dec_deg=sky.camera.pointing[1],
            pa_y_deg=sky.camera.roll_deg,
            plate_scale_arcsec=sky.optics.plate_scale_arcsec,
        )
        assert rebased is True
        accepted_before = runner.snapshot().stats.accepted
        wait_until(lambda: runner.snapshot().stats.accepted > accepted_before)
        reasons = dict(runner.snapshot().stats.reasons)
        assert not any("mount moved" in reason for reason in reasons)
        assert runner.snapshot().recoveries == 0
    finally:
        runner.stop()


def test_the_runner_tells_the_stack_where_the_target_is(sky, tmp_path) -> None:
    runner = Runner(
        capture=sky.camera.capture_to,
        camera=sky.camera,
        model=sky.model,
        config=RunConfig(
            exposure_s=0.02,
            bin_factor=2,
            keep_frames=False,
            target_ra_deg=sky.camera.pointing[0],
            target_dec_deg=sky.camera.pointing[1],
        ),
        frames_dir=tmp_path / "frames",
        scratch_dir=tmp_path / "scratch",
        clock=lambda: sky.when,
    )
    try:
        runner.start()
        # ``frame`` counts captures started; the crop only exists once one is accumulated.
        wait_until(lambda: runner.snapshot().stats.accepted >= 1)
        assert runner.snapshot().stats.crop_rule == "target"
    finally:
        runner.stop()
