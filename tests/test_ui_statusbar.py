"""The camera footer: what it says, and that reconnecting actually reopens the session.

The failure this exists for is the one that stops a night: the body asleep, the cable knocked,
or a session that EDSDK will not come back from. Restarting the console is not an acceptable
repair at the telescope, so the reconnect has to work from any screen -- and it has to be honest
about *why* it is offering itself.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from ministar.ui import station as station_module
from ministar.ui.station import Analysis, Station, StationTimeout
from ministar.ui.statusbar import _Rate, describe


# -- what it says ------------------------------------------------------------
def test_a_healthy_idle_camera_is_dim_not_alarming() -> None:
    said = describe(name="Canon EOS 60D")
    assert said.text == "camera  Canon EOS 60D"
    assert "live view off" in said.detail
    assert said.tone == "mn-dim"


def test_a_streaming_camera_reports_the_rate() -> None:
    said = describe(name="Canon EOS 60D", live_on=True, fps=8.6)
    assert said.detail == "live view  8.6 fps"
    assert said.tone == "mn-body"


def test_a_preview_that_is_not_delivering_does_not_claim_to_be_live() -> None:
    """The rate is only shown when there is one: "live view" with no frames is a lie."""
    said = describe(name="Canon EOS 60D", live_on=True, fps=-1.0)
    assert said.detail == "live view"


def test_an_error_is_shown_in_full_and_in_warning_colour() -> None:
    reason = "EdsdkUnavailable: no camera visible to EDSDK (powered off, asleep, cable)"
    said = describe(name="Canon EOS 60D", error=reason)
    assert said.tone == "mn-warn"
    assert said.detail == reason, "the reason is the useful part; it must not be summarised"


def test_no_session_says_so_and_points_at_the_button() -> None:
    said = describe(connected=False)
    assert said.text == "camera  --  no session"
    assert said.detail == "press reconnect"
    assert said.tone == "mn-warn"


def test_a_run_is_named_as_imaging() -> None:
    said = describe(name="Canon EOS 60D", suspended=True)
    assert said.detail == "imaging (live view off)"
    assert said.tone == "mn-body"


def test_the_rate_needs_a_window_before_it_claims_anything() -> None:
    rate = _Rate(window_s=1.0)
    assert rate.update(0.0, 0) == -1.0
    assert rate.update(0.1, 1) == -1.0, "two frames 0.1 s apart is not a rate"
    assert rate.update(1.0, 9) == pytest.approx(9.0, abs=0.5)


# -- reconnecting ------------------------------------------------------------
def synthetic_frame() -> np.ndarray:
    rng = np.random.default_rng(2)
    return rng.normal(12.0, 3.0, (24, 32, 3)).clip(0, 255).astype(np.uint8)


@pytest.fixture
def analysed(monkeypatch) -> None:
    def fake_analyse(jpeg: bytes) -> Analysis:
        return Analysis(
            hfd=1.8,
            stars=9,
            contrast=3.0,
            thumb=np.zeros((32, 32), dtype=np.float32),
            pixels=synthetic_frame(),
            width=1056,
            height=704,
        )

    monkeypatch.setattr(station_module, "analyse", fake_analyse)


class CountingCamera:
    """A body that can be opened and closed, and that records which thread did it."""

    name = "counting"
    disposable_frames = True
    position = 0

    def __init__(self, *, refuse_after: int = 0) -> None:
        self.connects = 0
        self.disconnects = 0
        self.threads: set[int] = set()
        self.frames = 0
        self.refuse_after = refuse_after
        self.block_connect: threading.Event | None = None

    def connect(self):
        self.connects += 1
        self.threads.add(threading.get_ident())
        if self.block_connect is not None:
            self.block_connect.wait(60.0)
        if self.refuse_after and self.connects > self.refuse_after:
            raise RuntimeError("no camera visible to EDSDK")
        return SimpleNamespace(name=self.name, port="fake")

    def disconnect(self) -> None:
        self.disconnects += 1
        self.threads.add(threading.get_ident())

    def grab_live_jpeg(self) -> bytes:
        self.threads.add(threading.get_ident())
        self.frames += 1
        return b"jpeg"

    def stop_live_view(self) -> None:
        self.threads.add(threading.get_ident())


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.005) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition never became true")


def test_reconnect_closes_and_reopens_the_session(analysed) -> None:
    camera = CountingCamera()
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        wait_until(lambda: station.connected)
        assert camera.connects == 1

        assert station.reconnect() is True

        assert camera.connects == 2, "a reconnect must actually open a new session"
        assert camera.disconnects >= 1, "the old session was never closed"
    finally:
        station.stop()


def test_reconnect_happens_on_the_one_thread_allowed_to_touch_the_camera(analysed) -> None:
    camera = CountingCamera()
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        wait_until(lambda: station.connected)
        station.reconnect()
        assert camera.threads == {station.thread_id}, (
            f"the reconnect left the owning thread: {camera.threads}"
        )
    finally:
        station.stop()


def test_a_reconnect_that_finds_nothing_says_so_and_keeps_the_reason(analysed) -> None:
    camera = CountingCamera(refuse_after=1)  # first connect works, the retry does not
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        wait_until(lambda: station.connected)

        assert station.reconnect() is False
        assert not station.connected
        assert "no camera visible" in station.snapshot().error
    finally:
        station.stop()


def test_a_wedged_reconnect_times_out_rather_than_hanging_the_button(analysed) -> None:
    camera = CountingCamera()
    camera.block_connect = threading.Event()  # a body that never answers
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        with pytest.raises(StationTimeout):
            station.reconnect(wait_s=0.25)
    finally:
        camera.block_connect.set()
        station.stop()


def test_reconnecting_brings_the_preview_back(analysed) -> None:
    """What the operator is waiting for: press reconnect on FOCUS and see stars again."""
    camera = CountingCamera()
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        station.want_live_view(True)
        wait_until(lambda: camera.frames >= 2)

        station.reconnect()

        before = camera.frames
        wait_until(lambda: camera.frames > before)
        assert station.live_view_on
    finally:
        station.stop()
