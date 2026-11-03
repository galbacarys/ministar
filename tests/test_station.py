"""The station: one camera, one thread, and the invariants that keep it that way.

Written after a real deadlock (2026-10-28). SETUP's "read from a test frame" called
``station.camera.capture_to`` directly -- past the station, outside the camera lock, and on
the NiceGUI event loop -- while the pump was starting live view. Two threads ended up parked
inside EDSDK (``set_save_to_host`` and ``start_live_view``), and because the capture ran on
the event loop the whole console stopped answering. The visible symptom was *not* "the
server is stuck": it was "the focus screen never shows a preview", because the loop that
would have rendered it was inside the camera SDK.

So the rule these tests defend is narrow and total: every entry into the camera goes through
the station, one at a time. A camera that notices overlapping entry into its own methods is
enough to prove it -- no EDSDK, no cv2, no browser.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ministar.camera.base import CameraSettings
from ministar.focus.driver import Direction
from ministar.ui import station as station_module
from ministar.ui.station import Analysis, Station, StationStopped, StationTimeout


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition never became true")


def synthetic_frame(settings: dict | None = None) -> np.ndarray:
    """A dark noisy frame with a handful of stars: what the preview has to make visible."""
    values = {"sky": 12.0, "noise": 3.0, "stars": 6}
    values.update(settings or {})
    rng = np.random.default_rng(1)
    frame = rng.normal(values["sky"], values["noise"], (48, 64, 3))
    for i in range(int(values["stars"])):
        frame[(i * 7 + 5) % 48, (i * 11 + 9) % 64] = 200.0
    return frame.clip(0, 255).astype(np.uint8)


class GuardedCamera:
    """A camera that records *which threads* call it, and whether two are ever inside at once.

    Both numbers matter and for different reasons. ``overlaps`` catches calls running
    concurrently; ``threads`` catches the subtler failure EDSDK actually has -- calls spread
    across threads, which never return at all because the SDK only progresses on the thread
    that opened the session.
    """

    name = "guarded"
    disposable_frames = True
    position = 0

    def __init__(self, *, frame_s: float = 0.005, capture_s: float = 0.02) -> None:
        self._lock = threading.Lock()
        self._inside = 0
        self.overlaps = 0
        self.calls: list[str] = []
        #: Thread ids that have entered the camera, and therefore need to stay at one entry.
        self.threads: set[int] = set()
        self.frames = 0
        self.captures: list[Path] = []
        self.pulses: list[Direction] = []
        self.settings: list = []
        self.live_view_stopped = 0
        self.frame_s = frame_s
        self.capture_s = capture_s
        #: Set by a test to hold a capture open while it inspects the station.
        self.block_capture: threading.Event | None = None
        self.capture_error: Exception | None = None
        self.disconnected = False

    @contextlib.contextmanager
    def _enter(self, what: str):
        with self._lock:
            self._inside += 1
            if self._inside > 1:
                self.overlaps += 1
            self.calls.append(what)
            self.threads.add(threading.get_ident())
        try:
            yield
        finally:
            with self._lock:
                self._inside -= 1

    def grab_live_jpeg(self) -> bytes:
        with self._enter("live"):
            time.sleep(self.frame_s)
            with self._lock:
                self.frames += 1
            return b"not-a-real-jpeg"

    def capture_to(self, path, **kwargs) -> Path:
        with self._enter("capture"):
            if self.block_capture is not None:
                self.block_capture.wait(60.0)
            time.sleep(self.capture_s)
            if self.capture_error is not None:
                raise self.capture_error
            self.captures.append(Path(path))
            return Path(path)

    def pulse(self, direction: Direction) -> None:
        with self._enter("pulse"):
            time.sleep(0.001)
            self.pulses.append(direction)

    def stop_live_view(self) -> None:
        with self._enter("stop-live"):
            self.live_view_stopped += 1

    def connect(self):
        with self._enter("connect"):
            return SimpleNamespace(name=self.name, port="fake")

    def apply(self, settings) -> None:
        with self._enter("apply"):
            self.settings.append(settings)

    def get_settings(self):
        with self._enter("settings"):
            return self.settings[-1] if self.settings else None

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def analysed(monkeypatch) -> None:
    """Stub the JPEG analysis: these tests are about serialisation, not star measurement.

    The stub returns a *realistic* frame -- noisy sky plus a few stars -- rather than zeros,
    because the display stretch runs on it: a uniform array has zero MAD, which is the one
    degenerate case the stretch deliberately leaves black.
    """
    from ministar.focus.synth import StarField  # noqa: F401  (documents where frames come from)

    def fake_analyse(jpeg: bytes) -> Analysis:
        return Analysis(
            hfd=1.8,
            stars=12,
            contrast=3.0,
            thumb=np.zeros((32, 32), dtype=np.float32),
            pixels=synthetic_frame(),
            width=1056,
            height=704,
        )

    monkeypatch.setattr(station_module, "analyse", fake_analyse)


@pytest.fixture
def live_station(analysed):
    """A running station with live view wanted, as the FOCUS screen asks for it."""
    camera = GuardedCamera()
    # connect= is what the app passes: the pump thread opens the session.
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        yield station, camera
    finally:
        station.stop()


# -- when live view is allowed to start -------------------------------------
def test_the_mirror_stays_down_until_a_screen_asks(analysed) -> None:
    camera = GuardedCamera()
    station = Station(camera)
    station.start()
    try:
        time.sleep(0.1)
        assert camera.frames == 0, "the pump grabbed a frame nobody asked to see"
        assert camera.calls == []

        station.want_live_view(True)
        wait_until(lambda: camera.frames >= 2)
    finally:
        station.stop()


# -- the invariant that broke ----------------------------------------------
def test_every_camera_call_lands_on_one_thread(live_station) -> None:
    """The invariant EDSDK actually has -- and the one a lock cannot provide.

    Live view, focus pulses, a still capture and a settings write all happen on a single
    thread, and that thread is the one that opened the session. Measured on an EOS 60D: a
    blocking SDK call from any other thread does not return until the owning thread pumps
    ``EdsGetEvent``, so this property is the difference between a preview and a hang -- not a
    tidiness preference. An earlier design had a per-call lock and still wedged, because the
    calls were on the wrong thread rather than overlapping.
    """
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 2)
    station.request(Direction.NEAR_1, 2)

    # Captures and settings from *other* threads, as RUN and the screens do.
    threads = [
        threading.Thread(target=station.capture_to, args=(Path("one.cr2"),)),
        threading.Thread(target=station.capture_to, args=(Path("two.cr2"),)),
    ]
    for thread in threads:
        thread.start()
    station.apply(CameraSettings(iso=800, shutter_seconds=1.0))
    for thread in threads:
        thread.join(5.0)

    wait_until(lambda: camera.frames >= 4)
    assert camera.threads == {station.thread_id}, (
        f"camera calls were spread over {len(camera.threads)} threads: {camera.threads}"
    )
    assert {"live", "pulse", "capture", "apply"} <= set(camera.calls)


def test_a_capture_holds_off_live_view_while_it_runs(live_station) -> None:
    """A shutter release must not have a live-view download running through it (02 §1.7)."""
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 1)

    camera.block_capture = threading.Event()
    done = threading.Event()

    def capture() -> None:
        try:
            station.capture_to(Path("one.cr2"))
        finally:
            done.set()

    thread = threading.Thread(target=capture, name="test-capture")
    thread.start()
    try:
        wait_until(lambda: "capture" in camera.calls)
        frames = camera.frames
        time.sleep(0.05)
        assert camera.frames == frames, "the pump grabbed a live-view frame during a capture"
    finally:
        camera.block_capture.set()
        assert done.wait(5.0)
        thread.join(5.0)
        camera.block_capture = None

    before = camera.frames
    wait_until(lambda: camera.frames > before)


def test_a_capture_and_the_pump_never_touch_the_camera_at_once(live_station) -> None:
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 2)
    station.request(Direction.NEAR_1, 2)

    station.capture_to(Path("one.cr2"))
    station.capture_to(Path("two.cr2"))

    wait_until(lambda: camera.frames >= 5)
    assert camera.captures == [Path("one.cr2"), Path("two.cr2")]
    assert camera.pulses, "the queued focus pulses never reached the camera"
    assert camera.overlaps == 0, f"two threads were inside the camera at once: {camera.calls}"


def test_a_capture_returns_the_camera_to_live_view(live_station) -> None:
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 1)

    station.capture_to(Path("one.cr2"))

    # The body offers a new frame after the download and the pump must pick it up, or FOCUS
    # would show a frozen preview after every still (16 §11.1 says live view does survive it).
    assert not station.suspended
    before = camera.frames
    wait_until(lambda: camera.frames > before)


def test_a_capture_inside_a_run_leaves_the_run_suspended(live_station) -> None:
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 1)

    # A run suspends once and then captures many frames; clearing that on the first capture
    # would restart live view in the middle of the run, which is the 02 §1.7 failure.
    station.suspend()
    station.capture_to(Path("run.cr2"))
    assert station.suspended
    frames = camera.frames
    time.sleep(0.05)
    assert camera.frames == frames, "live view restarted in the middle of a run"


def test_a_failed_capture_still_gives_the_camera_back(live_station) -> None:
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 1)

    camera.capture_error = RuntimeError("the body refused the shutter")
    with pytest.raises(RuntimeError):
        station.capture_to(Path("bad.cr2"))

    # A raise that left the pump parked would look exactly like the bug that started this.
    assert not station.suspended
    before = camera.frames
    wait_until(lambda: camera.frames > before)


def test_the_frame_after_a_capture_reports_no_motion(analysed) -> None:
    # Slow frames on purpose: the claim is about *one* published snapshot, so there has to be
    # time to look at it before the next frame replaces it. 0.3 s, not 0.08: at 80 ms the full
    # suite's CPU load was enough to publish two frames before the test looked.
    camera = GuardedCamera(frame_s=0.3)
    station = Station(camera)
    station.start()
    try:
        station.want_live_view(True)
        # Two frames in a row with no capture between them, so motion is a measurement rather
        # than "unknown".
        wait_until(lambda: station.snapshot().motion >= 0.0)

        station.capture_to(Path("one.cr2"))
        after = camera.frames
        wait_until(lambda: camera.frames > after)
        # The frame after a capture is not comparable with the one from before it: whatever
        # happened during the shutter and the download is not sky motion divided by elapsed
        # time. Reporting it would tell POINT that the mount is moving (or, after a long
        # capture, that it is perfectly still) based on the capture's own duration.
        assert station.snapshot().motion == -1.0
    finally:
        station.stop()


def test_stop_disconnects_and_joins() -> None:
    camera = GuardedCamera()
    station = Station(camera)
    station.start()
    station.stop()
    assert camera.disconnected


def test_the_session_is_opened_on_the_pump_thread(analysed) -> None:
    """The point of the whole design: the thread that calls the SDK is the one that opened it.

    EDSDK blocks a call from any other thread until the owning thread pumps events, so a
    session opened at startup and then used by the pump is a camera that silently never
    answers. Asserting it here means the next person to move `connect()` fails a test instead
    of discovering it with a camera in the dark.
    """
    camera = GuardedCamera()
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        wait_until(lambda: "connect" in camera.calls)
        assert camera.threads == {station.thread_id}
    finally:
        station.stop()


def test_a_blocked_sdk_call_times_out_instead_of_hanging_the_caller(analysed) -> None:
    """A wedged body must become an error the screen can show, not a frozen console.

    A blocking call inside the DLL cannot be interrupted from Python, so the pump thread may
    stay stuck -- but the thread that asked is free, and it says what happened.
    """
    camera = GuardedCamera()
    camera.block_capture = threading.Event()  # never set: the "SDK never returns"
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        wait_until(lambda: "connect" in camera.calls or True)
        started = time.monotonic()
        with pytest.raises(StationTimeout):
            station.capture_to(Path("stuck.cr2"), wait_s=0.25)
        assert time.monotonic() - started < 2.0, "the caller waited long past its timeout"
        assert station.snapshot().frames >= 0  # the station is still answering
    finally:
        camera.block_capture.set()
        station.stop()


def test_a_station_that_is_not_running_refuses_work(analysed) -> None:
    station = Station(GuardedCamera(), connect=None)
    with pytest.raises(StationStopped):
        station.capture_to(Path("nope.cr2"))


def test_stopping_releases_a_caller_and_says_the_pump_is_stuck(analysed) -> None:
    """A wedged pump must not take the waiting thread with it.

    This is the shutdown half of the same failure: the SDK call cannot be interrupted, so
    ``stop()`` gives up after a bounded wait, releases whoever was queued behind the stuck
    call, and publishes the fact that the camera did not shut down.
    """
    camera = GuardedCamera()
    camera.block_capture = threading.Event()  # never set while the station is being stopped
    station = Station(camera, connect=camera.connect)
    station.start()
    wait_until(lambda: "connect" in camera.calls)

    errors: list[BaseException] = []

    def capture() -> None:
        try:
            station.capture_to(Path("late.cr2"), wait_s=30.0)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=capture, name="test-capture")
    thread.start()
    wait_until(lambda: "capture" in camera.calls)

    station.stop()
    thread.join(5.0)

    assert not thread.is_alive(), "the caller was left waiting on a stopped station"
    assert errors and isinstance(errors[0], StationStopped), f"caller got {errors}"
    assert "did not shut down" in station.snapshot().error
    camera.block_capture.set()  # let the leaked pump thread finish


# -- leaving live view -------------------------------------------------------
def test_leaving_live_view_stands_the_camera_down(live_station) -> None:
    """Live view is a focusing aid, not the imaging state: the body must leave it.

    On a DSLR "leaving live view" is a real state change -- the mirror comes back and the EVF
    stream stops -- so it is the camera that has to be told, not just the pump that has to stop
    asking. Leaving it up costs battery, heats the sensor, and is not what the shutter wants.
    """
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 2)

    station.want_live_view(False)
    wait_until(lambda: camera.live_view_stopped == 1)

    frames = camera.frames
    time.sleep(0.1)
    assert camera.frames == frames, "the pump kept grabbing after live view was given up"
    assert not station.live_view_on


def test_only_one_stand_down_per_visit(live_station) -> None:
    """The pump idles in a loop; the camera must be told once, not eleven times a second."""
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 2)
    station.want_live_view(False)

    wait_until(lambda: camera.live_view_stopped >= 1)
    time.sleep(0.25)
    assert camera.live_view_stopped == 1, "the stand-down is being repeated every tick"


def test_coming_back_to_live_view_restarts_it(live_station) -> None:
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 2)
    station.want_live_view(False)
    wait_until(lambda: camera.live_view_stopped == 1)

    station.want_live_view(True)
    before = camera.frames
    wait_until(lambda: camera.frames > before)
    assert station.live_view_on


def test_a_run_stands_the_camera_down_too(live_station) -> None:
    """A run images with the mirror down: suspend() has to reach the camera, not just the pump."""
    station, camera = live_station
    station.want_live_view(True)
    wait_until(lambda: camera.frames >= 2)

    station.suspend()
    wait_until(lambda: camera.live_view_stopped == 1)
    frames = camera.frames
    time.sleep(0.1)
    assert camera.frames == frames


# -- when the session will not open -----------------------------------------
class RefusingCamera(GuardedCamera):
    """A body that is not there: connect fails, and it says why."""

    def __init__(self, *, fail_until: int = 99) -> None:
        super().__init__()
        self.attempts = 0
        self.fail_until = fail_until

    def connect(self):
        self.attempts += 1
        if self.attempts <= self.fail_until:
            raise RuntimeError("no camera visible to EDSDK")
        return SimpleNamespace(name=self.name, port="fake")


def test_a_failed_connect_keeps_its_reason(analysed) -> None:
    """The useful error must survive; a retry loop would replace it with "not connected".

    Reported by a probe on the real body: the connect error (``0xC0``, the port was held by the
    running console) was visible for under a second and then became the generic
    ``CameraNotConnected: connect() the camera first`` from the next grab attempt -- which says
    nothing about why.
    """
    camera = RefusingCamera()
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        # No ask: the connect is attempted when the pump starts, and a *later* ask is a
        # deliberate retry (the next test). This one is about the message surviving.
        wait_until(lambda: "no camera visible" in station.snapshot().error)
        time.sleep(0.4)
        assert "no camera visible" in station.snapshot().error, (
            f"the connect reason was overwritten: {station.snapshot().error!r}"
        )
        assert station.snapshot().frames == 0
        assert not station.connected
        assert camera.attempts == 1, "a failed connect was retried on a timer"
    finally:
        station.stop()


def test_asking_for_a_preview_retries_a_failed_connect(analysed) -> None:
    """Turn the camera on and revisit the screen: no console restart required."""
    camera = RefusingCamera(fail_until=1)
    station = Station(camera, connect=camera.connect)
    station.start()
    try:
        station.want_live_view(True)
        wait_until(lambda: "no camera visible" in station.snapshot().error)

        station.want_live_view(True)  # the screen re-rendering
        wait_until(lambda: station.connected)
        wait_until(lambda: camera.frames >= 1)
        assert station.snapshot().name == "guarded"
    finally:
        station.stop()
