"""The station adapter that lets the real autofocus routine drive the UI.

A fake station stands in for the threaded camera pump, so the ordering rules that can
be wrong -- "wait for a frame *after* the request", "queue full beats waiting", the
star-count gate -- are testable without a camera, a browser or cv2.
"""

from __future__ import annotations

import math
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
    """Just enough Station: snapshots that advance when a pulse is requested."""

    snap: Snap = field(default_factory=Snap)
    requests: list[tuple[Direction, int]] = field(default_factory=list)
    accept: bool = True
    #: Frames produced per request; 0 models a camera that never answers.
    frames_per_request: int = 1

    def snapshot(self) -> Snap:
        return self.snap

    def request(self, direction: Direction, count: int = 1) -> int:
        if not self.accept:
            return 0
        self.requests.append((direction, count))
        self.snap.frames += self.frames_per_request
        self.snap.position += count if direction.toward_near else -count
        return count


def a_driver(station: FakeStation, **kwargs) -> StationDriver:
    values = {"timeout_s": 0.05, "poll_s": 0.001}
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
    station = FakeStation(frames_per_request=0)

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
