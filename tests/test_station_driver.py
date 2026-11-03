"""The station adapter that lets the real autofocus routine drive the UI.

A fake station stands in for the threaded camera pump, so the ordering rules that can
be wrong -- "wait for a frame *after* the request", "queue full beats waiting", the
star-count gate -- are testable without a camera, a browser or cv2.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import pytest

from ministar.focus.driver import Direction
from ministar.focus.station_driver import StationDriver


@dataclass
class Snap:
    frames: int = 0
    pending: int = 0
    position: int = 0
    hfd: float = 1.8
    stars: int = 20
    error: str = ""


@dataclass
class FakeStation:
    """Just enough Station: a live stream that keeps producing frames.

    ``snapshot()`` advances the frame counter on every call, which is what a running preview
    looks like from the driver's side -- measurements average several *distinct* frames, so a
    fake that returned the same frame number forever would model a frozen camera (and did:
    that is how the averaging timeout was caught).
    """

    snap: Snap = field(default_factory=Snap)
    requests: list[tuple[Direction, int]] = field(default_factory=list)
    accept: bool = True
    #: Frames produced per request; 0 models a camera that never answers.
    frames_per_request: int = 1
    #: Whether preview frames keep arriving between requests.
    streaming: bool = True
    #: Per-frame HFD and star counts, in order, to model a noisy measurement.
    hfd_jitter: tuple[float, ...] = ()
    stars_jitter: tuple[int, ...] = ()
    _frame_index: int = 0

    def snapshot(self) -> Snap:
        if not self.streaming:
            return self.snap
        self.snap.frames += 1
        if self.hfd_jitter:
            self.snap.hfd = self.hfd_jitter[self._frame_index % len(self.hfd_jitter)]
        if self.stars_jitter:
            self.snap.stars = self.stars_jitter[self._frame_index % len(self.stars_jitter)]
        self._frame_index += 1
        return self.snap

    def request(self, direction: Direction, count: int = 1) -> int:
        if not self.accept:
            return 0
        self.requests.append((direction, count))
        self.snap.frames += self.frames_per_request
        self.snap.position += count if direction.toward_near else -count
        return count


def a_driver(station: FakeStation, **kwargs) -> StationDriver:
    values = {"timeout_s": 0.05, "poll_s": 0.001, "frames_per_measure": 5}
    values.update(kwargs)
    return StationDriver(station=station, **values)  # type: ignore[arg-type]


# -- pulsing ----------------------------------------------------------------
def test_a_pulse_reports_success_once_a_newer_frame_exists() -> None:
    station = FakeStation()

    result = a_driver(station).pulse(Direction.NEAR_1, 3)

    assert result.ok and result.pulses_sent == 3
    assert station.requests == [(Direction.NEAR_1, 3)]


def test_a_pulse_that_produces_no_frame_fails_rather_than_lying() -> None:
    """Silence is not success: the routine must not measure a stale picture."""
    # A camera that answers nothing at all: no frame from the pulse, and none from the stream.
    station = FakeStation(frames_per_request=0, streaming=False)

    result = a_driver(station).pulse(Direction.NEAR_1, 1)

    assert not result.ok
    assert "did not apply" in result.detail


def test_a_full_queue_is_reported_without_waiting() -> None:
    result = a_driver(FakeStation(accept=False)).pulse(Direction.FAR_2, 1)

    assert not result.ok
    assert "queue full" in result.detail


def test_the_driver_reports_the_station_position() -> None:
    station = FakeStation()
    driver = a_driver(station)

    driver.pulse(Direction.NEAR_2, 2)

    assert driver.position == 2


# -- measuring --------------------------------------------------------------
def test_a_good_frame_is_accepted() -> None:
    sample = a_driver(FakeStation()).measure()

    assert sample.accepted
    assert sample.star_count == 20
    assert sample.hfd == pytest.approx(1.8)


def test_a_measurement_averages_several_frames() -> None:
    """The reason this exists: single-frame HFD on a hard field jumped between 3 and 6 px."""
    station = FakeStation(hfd_jitter=(3.0, 6.0, 4.0, 6.0, 3.5))
    sample = a_driver(station, frames_per_measure=5).measure()

    assert sample.accepted
    # The middle of those five readings, not the first or the last frame's.
    assert sample.hfd == pytest.approx(4.0)


def test_one_bad_frame_cannot_drag_the_measurement() -> None:
    """A median rather than a mean: a gust, a cloud or a dropped JPEG is discarded."""
    station = FakeStation(hfd_jitter=(2.0, 2.0, 2.0, 40.0, 2.0))
    sample = a_driver(station, frames_per_measure=5).measure()

    assert sample.accepted
    assert sample.hfd == pytest.approx(2.0)


def test_a_star_poor_field_is_rejected_even_if_one_frame_looks_fine() -> None:
    """A majority of the frames must be usable, or it is luck rather than a measurement."""
    station = FakeStation(stars_jitter=(20, 0, 0, 0, 0))
    sample = a_driver(station, frames_per_measure=5).measure()

    assert not sample.accepted
    assert "usable" in sample.reason


def test_too_few_stars_is_rejected_with_a_reason() -> None:
    sample = a_driver(FakeStation(snap=Snap(stars=2, hfd=math.nan))).measure()

    assert not sample.accepted
    assert "2 star" in sample.reason


def test_a_camera_error_is_rejected_and_quoted() -> None:
    sample = a_driver(FakeStation(snap=Snap(stars=0, error="EDS_ERR_COMM_DISCONNECTED"))).measure()

    assert not sample.accepted
    assert "DISCONNECTED" in sample.reason


def test_an_infinite_hfd_is_never_accepted() -> None:
    """A frame the metric could not measure is not a focus point."""
    sample = a_driver(FakeStation(snap=Snap(stars=20, hfd=math.inf))).measure()

    assert not sample.accepted


def test_motion_history_answers_whether_the_mount_moved_during_a_capture() -> None:
    """The pump samples while the UI thread is blocked, which is exactly when it matters."""
    from ministar.ui.station import FakeCamera, Station

    station = Station(FakeCamera())
    assert station.motion_since(0.0) == -1.0, "silence is not stillness"

    with station._lock:  # the pump's own bookkeeping
        station._motion.extend([(100.0, 2.4), (101.0, 9.5), (102.0, 2.6)])

    assert station.motion_since(100.5) == 9.5
    assert station.motion_since(101.5) == 2.6
    assert station.motion_since(103.0) == -1.0


def test_every_capture_gets_fresh_noise(sky, tmp_path) -> None:
    """The frame counter seeds the noise; a fixed counter made every capture identical.

    Identical noise correlates with itself at zero shift, which is not a subtle failure: it
    made the live stack's bump detector report zero residual however far the mount moved.
    """
    from ministar.io.fits import read_fits

    a, _ = read_fits(sky.camera.capture_to(tmp_path / "a.fit"))
    b, _ = read_fits(sky.camera.capture_to(tmp_path / "b.fit"))
    assert not (a == b).all()
    assert sky.camera._frames == 2


def test_the_pump_does_not_start_until_a_preview_is_wanted() -> None:
    """The startup bug: a DSLR's live view raises the mirror, so the pump must wait."""
    from ministar.ui.station import FakeCamera, Station

    camera = FakeCamera(shape=(64, 64), count=4)
    grabs = {"count": 0}
    original = camera.grab_live_jpeg

    def counting() -> bytes:
        grabs["count"] += 1
        return original()

    camera.grab_live_jpeg = counting  # type: ignore[method-assign]
    station = Station(camera)
    station.start()
    try:
        time.sleep(0.4)
        assert grabs["count"] == 0, "the pump grabbed a preview before anyone asked"
        station.want_live_view(True)
        deadline = time.monotonic() + 3.0
        while grabs["count"] == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert grabs["count"] > 0
    finally:
        station.stop()
