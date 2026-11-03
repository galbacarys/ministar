"""EdsdkCamera protocol logic, against a fake FFI.

No camera and no EDSDK.dll involved. The FFI is injected precisely so that the
decisions that can be wrong -- direction mapping, position bookkeeping, pulse
counts, frame acceptance -- are testable anywhere, on any OS.
"""

from __future__ import annotations

import ctypes
import math
from pathlib import Path

import numpy as np
import pytest

from ministar.camera import edsdk_ffi
from ministar.camera.edsdk import (
    CameraNotConnected,
    EdsdkCamera,
    EdsdkUnavailable,
    decode_jpeg,
    lens_param,
)
from ministar.focus.driver import Direction

SIZE = 96


class FakeFfi:
    """Minimal stand-in exposing the same surface as ``edsdk_ffi.Edsdk``."""

    def __init__(
        self,
        devices: list[edsdk_ffi.DeviceInfo] | None = None,
        *,
        jpeg: bytes = b"pretend-jpeg",
        drive_error: Exception | None = None,
        grab_error: Exception | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.devices = (
            devices
            if devices is not None
            else [edsdk_ffi.DeviceInfo(port="usb#1", description="Fake EOS", sub_type=1)]
        )
        self.jpeg = jpeg
        self.drive_error = drive_error
        self.grab_error = grab_error
        self.fail_after = fail_after
        #: Still-capture behaviour: an ordered log of what the protocol asked for.
        self.events: list[object] = []
        self.transfer_error: Exception | None = None
        self.shot_error: Exception | None = None
        self.transfer_item: object = "offered-item"
        self.sent: list[int] = []
        self.keepalives = 0
        self.initialized = False
        self.live = False
        self.closed = False
        #: The body's current codes: ISO 800, Tv 1.6 s, Av f/2.8 by default.
        self.properties: dict[int, int] = {
            edsdk_ffi.PROP_ISO_SPEED: 0x60,
            edsdk_ffi.PROP_TV: 0x33,
            edsdk_ffi.PROP_AV: 0x20,
        }
        #: What this body says it accepts. Empty means "cannot enumerate".
        self.descriptions: dict[int, tuple[int, ...]] = {
            edsdk_ffi.PROP_ISO_SPEED: (0x48, 0x50, 0x58, 0x60, 0x68, 0x70),
            edsdk_ffi.PROP_TV: (0x30, 0x33, 0x35, 0x38, 0x3B, 0x3D, 0x40),
        }
        self.set_calls: list[tuple[int, int]] = []
        #: A camera that refuses the write outright, and one that silently clamps it.
        self.refuse_properties = False
        self.clamp_properties = False
        self.opened_index: int | None = None

    def initialize(self) -> None:
        self.initialized = True

    def list_cameras(self):
        return list(self.devices)

    def open_camera(self, index: int = 0):
        self.opened_index = index
        return self.devices[index]

    def start_live_view(self) -> None:
        self.live = True

    def stop_live_view(self) -> None:
        self.live = False

    def grab_jpeg(self, timeout_s: float = 6.0) -> bytes:
        if self.grab_error is not None:
            raise self.grab_error
        return self.jpeg

    def drive_lens(self, param: int, **_kw) -> None:
        if self.drive_error is not None or (
            self.fail_after is not None and len(self.sent) >= self.fail_after
        ):
            raise self.drive_error or edsdk_ffi.EdsdkError("EdsSendCommand", 0x81)
        self.sent.append(param)

    def keep_alive(self) -> None:
        self.keepalives += 1

    # -- still capture ------------------------------------------------------
    def set_save_to_host(self) -> None:
        self.events.append("save_to")

    def arm_transfer_handler(self) -> None:
        self.events.append("arm")

    def take_picture(self) -> None:
        self.events.append("shoot")
        if self.shot_error is not None:
            raise self.shot_error

    def wait_for_transfer(self, timeout_s: float = 30.0) -> object:
        self.events.append("wait")
        if self.transfer_error is not None:
            raise self.transfer_error
        return self.transfer_item

    def download_to(self, item: object, path) -> Path:
        self.events.append(("download", str(path)))
        return Path(path)

    def drop_transfers(self) -> int:
        self.events.append("drop")
        return 1

    def close(self) -> None:
        self.closed = True

    # -- properties (ISO / Tv / Av) -----------------------------------------
    def uint_property(self, prop_id: int) -> int:
        return self.properties[prop_id]

    def set_uint_property(self, prop_id: int, value: int) -> None:
        if self.refuse_properties:
            raise edsdk_ffi.EdsdkError(f"EdsSetPropertyData(0x{prop_id:08X})", 0x81)
        self.set_calls.append((prop_id, value))
        if not self.clamp_properties:
            self.properties[prop_id] = value

    def property_desc(self, prop_id: int) -> tuple[int, ...]:
        return self.descriptions.get(prop_id, ())


def camera_for(fake: FakeFfi) -> EdsdkCamera:
    return EdsdkCamera(ffi_factory=lambda: fake, sleep=lambda _s: None)


def star_field(sigmas: tuple[float, ...] = (1.5,), peak: float = 4000.0) -> np.ndarray:
    img = np.full((SIZE, SIZE), 200.0, dtype=np.float64)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    centres = [(24, 24), (24, 48), (24, 72), (48, 24), (48, 48), (48, 72), (72, 24), (72, 48)]
    for i, (cy, cx) in enumerate(centres):
        s = sigmas[i % len(sigmas)]
        img += peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * s * s)))
    return np.clip(img, 0, 65535).astype(np.uint16)


# -- connecting -------------------------------------------------------------
def test_connect_does_not_start_live_view() -> None:
    """Connecting must be quiet: on a DSLR live view flips the mirror and cycles the shutter."""
    fake = FakeFfi()
    cam = camera_for(fake)
    info = cam.connect()

    assert info.name == "Fake EOS"
    assert info.port == "usb#1"
    assert fake.initialized
    assert not fake.live  # nothing has asked for a preview yet
    assert cam.connected


def test_the_first_preview_starts_live_view() -> None:
    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()
    cam.grab_live_jpeg()
    assert fake.live


def test_connect_is_not_repeatable() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    with pytest.raises(RuntimeError, match="already connected"):
        cam.connect()


def test_connect_with_no_camera_raises_unavailable() -> None:
    cam = camera_for(FakeFfi(devices=[]))
    with pytest.raises(EdsdkUnavailable, match="no camera visible"):
        cam.connect()


def test_connect_when_sdk_will_not_load_raises_unavailable() -> None:
    def exploding() -> FakeFfi:
        raise RuntimeError("could not load EDSDK.dll")

    with pytest.raises(EdsdkUnavailable, match=r"EDSDK\.dll"):
        EdsdkCamera(ffi_factory=exploding).connect()


def test_disconnect_is_idempotent() -> None:
    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()
    cam.disconnect()
    assert fake.closed and not cam.connected
    cam.disconnect()  # must not raise


# -- direction mapping ------------------------------------------------------
@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (Direction.NEAR_1, 0x0001),
        (Direction.NEAR_2, 0x0002),
        (Direction.NEAR_3, 0x0003),
        (Direction.FAR_1, 0x8001),
        (Direction.FAR_2, 0x8002),
        (Direction.FAR_3, 0x8003),
    ],
)
def test_direction_maps_to_edsdk_value(direction: Direction, expected: int) -> None:
    assert lens_param(direction) == expected


def test_far_is_near_with_the_bit_set() -> None:
    for near, far in zip(
        (Direction.NEAR_1, Direction.NEAR_2, Direction.NEAR_3),
        (Direction.FAR_1, Direction.FAR_2, Direction.FAR_3),
        strict=True,
    ):
        assert lens_param(far) == lens_param(near) | 0x8000


# -- pulsing ----------------------------------------------------------------
def test_pulse_sends_the_mapped_value_count_times() -> None:
    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()

    result = cam.pulse(Direction.NEAR_2, count=3)

    assert result.ok and result.moved and result.pulses_sent == 3
    assert fake.sent == [0x0002, 0x0002, 0x0002]


def test_position_moves_toward_near_and_away_from_far() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    assert cam.position == 0

    cam.pulse(Direction.NEAR_3)
    assert cam.position == 3

    cam.pulse(Direction.FAR_1, count=2)
    assert cam.position == 1


def test_reset_position_reanchors_the_counter() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    cam.pulse(Direction.NEAR_1)
    cam.reset_position(40)
    assert cam.position == 40


def test_pulse_before_connect_reports_failure_without_raising() -> None:
    result = EdsdkCamera().pulse(Direction.NEAR_1)
    assert not result.ok
    assert result.detail == "not connected"


def test_pulse_failure_keeps_the_partial_count_and_position() -> None:
    """A pulse that fails mid-run has already moved the lens; say so honestly."""
    fake = FakeFfi(fail_after=2)
    cam = camera_for(fake)
    cam.connect()

    result = cam.pulse(Direction.NEAR_1, count=5)

    assert not result.ok
    assert result.moved
    assert result.pulses_sent == 2
    assert cam.position == 2
    assert "MF" in result.detail


# -- measuring --------------------------------------------------------------
def test_measure_accepts_a_star_field() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()

    sample = cam.measure(decode=lambda _jpeg: star_field())

    assert sample.accepted
    assert sample.star_count >= 5
    assert math.isfinite(sample.hfd)
    assert sample.reason == ""


def test_measure_rejects_a_blank_frame() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()

    sample = cam.measure(decode=lambda _jpeg: np.zeros((SIZE, SIZE), dtype=np.uint8))

    assert not sample.accepted
    assert sample.star_count < 5
    assert "too few usable stars" in sample.reason


def test_measure_reports_a_grab_failure_as_not_accepted() -> None:
    fake = FakeFfi(grab_error=edsdk_ffi.EdsdkError("EdsDownloadEvfImage", 0xA102))
    cam = camera_for(fake)
    cam.connect()

    sample = cam.measure(decode=lambda _jpeg: star_field())

    assert not sample.accepted
    assert "0x0000A102" in sample.reason


def test_measure_records_the_current_position() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    cam.pulse(Direction.NEAR_3, count=2)

    assert cam.measure(decode=lambda _jpeg: star_field()).position == 6


# -- camera settings (ISO / Tv) --------------------------------------------
def test_get_settings_decodes_the_dials() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    settings = cam.get_settings()
    assert settings.iso == 800
    assert settings.shutter_seconds == pytest.approx(1.6)
    assert settings.aperture == pytest.approx(2.8)


def test_apply_sets_iso_and_tv_in_the_bodys_codes() -> None:
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()
    cam.apply(CameraSettings(iso=1600, shutter_seconds=2.0))

    assert (edsdk_ffi.PROP_ISO_SPEED, 0x68) in fake.set_calls
    assert (edsdk_ffi.PROP_TV, 0x30) in fake.set_calls
    assert cam.get_settings().iso == 1600
    assert cam.get_settings().shutter_seconds == pytest.approx(2.0)


def test_an_iso_the_table_knows_is_written_as_asked() -> None:
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()
    cam.apply(CameraSettings(iso=4000, shutter_seconds=1.0))
    assert fake.properties[edsdk_ffi.PROP_ISO_SPEED] == 0x73  # ISO 4000
    assert cam.get_settings().iso == 4000


def test_an_iso_off_the_ladder_is_refused_rather_than_substituted() -> None:
    """ISO 900 is not a camera setting; silently using 800 would be a different exposure."""
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()
    with pytest.raises(ValueError, match="not on the supported ladder"):
        cam.apply(CameraSettings(iso=900, shutter_seconds=1.0))


def test_the_body_offers_the_iso_ladder_it_can_actually_use() -> None:
    cam = camera_for(FakeFfi())
    values = cam.iso_options()
    assert 100 in values and 6400 in values
    assert all(value in values for value in (200, 400, 800, 1600, 3200))


def test_a_write_that_is_silently_clamped_is_an_error() -> None:
    """A clamped Tv would put the wrong exposure on every frame of a run."""
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    fake.clamp_properties = True
    cam = camera_for(fake)
    cam.connect()
    with pytest.raises(RuntimeError, match="did not accept"):
        cam.apply(CameraSettings(iso=1600, shutter_seconds=2.0))


def test_a_refused_write_reports_the_property() -> None:
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    fake.refuse_properties = True
    cam = camera_for(fake)
    cam.connect()
    with pytest.raises(edsdk_ffi.EdsdkError, match="0x00000402"):
        cam.apply(CameraSettings(iso=1600, shutter_seconds=2.0))


def test_the_property_descriptor_is_not_trusted() -> None:
    """The 60D's descriptor is nonsense through this binding (2026-10-28): a lying one must
    not be able to snap a 1.6 s request to 25 s, which is what happened on hardware."""
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    # Exactly what the body returned: a list containing 0x13 (25 s) and the current 0x33.
    fake.descriptions = {edsdk_ffi.PROP_TV: (0x37, 0x10, 0x13, 0x33)}
    cam = camera_for(fake)
    cam.connect()
    cam.apply(CameraSettings(iso=800, shutter_seconds=1.6))
    assert fake.properties[edsdk_ffi.PROP_TV] == 0x33  # 1.6 s from the table, not 25 s


def test_a_body_that_cannot_enumerate_still_sets_by_table() -> None:
    from ministar.camera.base import CameraSettings

    fake = FakeFfi()
    fake.descriptions = {}
    cam = camera_for(fake)
    cam.connect()
    cam.apply(CameraSettings(iso=800, shutter_seconds=1.6))
    assert cam.get_settings().iso == 800


def test_capture_still_says_it_is_not_implemented() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    with pytest.raises(NotImplementedError, match="brief 16"):
        cam.capture(1.0)


def test_keep_alive_reaches_the_ffi() -> None:
    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()
    cam.keep_alive()
    assert fake.keepalives == 1


def test_operations_after_disconnect_raise_not_connected() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    cam.disconnect()
    with pytest.raises(CameraNotConnected):
        cam.grab_live_jpeg()


# -- protocol conformance ---------------------------------------------------
def test_edsdk_camera_satisfies_both_protocols() -> None:
    """The promoted backend must actually be usable where the protocols are expected.

    ``runtime_checkable`` protocols only verify the surface exists, which is
    exactly right here: the point is that every method the routine and the app
    call is present, not that hardware has been exercised.
    """
    from ministar.camera.base import Camera
    from ministar.focus.driver import FocusDriver

    cam = camera_for(FakeFfi())
    assert isinstance(cam, FocusDriver)
    assert isinstance(cam, Camera)


# -- decoding ---------------------------------------------------------------
def test_decode_jpeg_uses_an_installed_backend() -> None:
    """Exercises the real decoder rather than an injected one.

    Skipped where no imaging library is installed, which is why the rest of this
    module injects ``decode`` instead of depending on one.
    """
    cv2 = pytest.importorskip("cv2")
    pixels = (np.random.default_rng(0).random((32, 48, 3)) * 255).astype(np.uint8)
    jpeg = cv2.imencode(".jpg", pixels)[1].tobytes()

    decoded = decode_jpeg(jpeg)

    assert decoded.shape == (32, 48, 3)


# -- still capture ----------------------------------------------------------
def test_capture_to_downloads_where_it_was_told(tmp_path: Path) -> None:
    fake = FakeFfi()
    cam = camera_for(fake)
    cam.connect()

    out = cam.capture_to(tmp_path / "shot.cr2")

    assert out == tmp_path / "shot.cr2"
    # The order is the protocol: where files go, arm the handler, shoot, wait, take.
    assert fake.events == ["save_to", "arm", "shoot", "wait", ("download", str(out))]


def test_a_capture_that_never_arrives_is_cancelled(tmp_path: Path) -> None:
    """An unclaimed transfer stops the body shooting, so the failure path must cancel."""
    fake = FakeFfi()
    fake.transfer_error = TimeoutError("camera did not offer a file within 120 s")
    cam = camera_for(fake)
    cam.connect()

    with pytest.raises(TimeoutError):
        cam.capture_to(tmp_path / "x.cr2")

    assert fake.events[-1] == "drop"
    assert not any(isinstance(event, tuple) and event[0] == "download" for event in fake.events)


def test_a_shutter_that_raises_also_cancels(tmp_path: Path) -> None:
    fake = FakeFfi()
    fake.shot_error = edsdk_ffi.EdsdkError("EdsSendCommand(TakePicture)", 0x81)
    cam = camera_for(fake)
    cam.connect()

    with pytest.raises(edsdk_ffi.EdsdkError):
        cam.capture_to(tmp_path / "x.cr2")

    assert "drop" in fake.events


def test_capture_needs_a_connection(tmp_path: Path) -> None:
    with pytest.raises(CameraNotConnected):
        EdsdkCamera().capture_to(tmp_path / "x.cr2")


# -- the live-view start sequence, at the FFI level -------------------------
class RecordingLib:
    """The ctypes DLL surface ``Edsdk`` calls, recording property writes.

    The camera-level fakes above stub ``start_live_view`` itself, so they cannot see which
    properties the real one writes -- which is how a call that hangs the body survived.
    """

    def __init__(self) -> None:
        self.writes: list[int] = []
        self.values: list[int] = []
        self.streams = 0
        self.refs = 0
        self.releases = 0
        self.initialised = 0
        self.terminated = 0

    def EdsInitializeSDK(self):
        self.initialised += 1
        return 0

    def EdsTerminateSDK(self):
        self.terminated += 1
        return 0

    def EdsCloseSession(self, camera):
        return 0

    def EdsSetPropertyData(self, camera, prop, param, size, ptr):
        import ctypes

        self.writes.append(prop)
        self.values.append(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value)
        return 0

    def EdsRelease(self, ref):
        self.releases += 1
        return 0

    def EdsCreateMemoryStream(self, size, out):
        self.streams += 1
        return 0

    def EdsCreateEvfImageRef(self, stream, out):
        self.refs += 1
        return 0


def a_bare_ffi(lib: RecordingLib) -> edsdk_ffi.Edsdk:
    """An Edsdk with no DLL behind it: just the attributes the lifecycle and EVF code touches."""
    ffi = edsdk_ffi.Edsdk.__new__(edsdk_ffi.Edsdk)
    ffi._camera = ctypes.c_void_p(1)
    ffi._live = False
    ffi._stream = None
    ffi._evf_image = None
    ffi._initialized = False
    ffi.EdsInitializeSDK = lib.EdsInitializeSDK
    ffi.EdsTerminateSDK = lib.EdsTerminateSDK
    ffi.EdsCloseSession = lib.EdsCloseSession
    ffi.EdsSetPropertyData = lib.EdsSetPropertyData
    ffi.EdsCreateMemoryStream = lib.EdsCreateMemoryStream
    ffi.EdsCreateEvfImageRef = lib.EdsCreateEvfImageRef
    ffi.EdsRelease = lib.EdsRelease
    return ffi


def test_starting_live_view_never_writes_evf_mode() -> None:
    """``Evf_Mode`` blocks forever on an EOS 60D; the output route alone is enough.

    Measured on the body: `Evf_Mode = 1` never returns, while `Evf_OutputDevice = PC`
    answers in 0.06 s and the first JPEG arrives after the usual ~1 s of 0xA102. A hang is
    not something a caller can defend against -- no exception, no timeout, no message -- so
    the guard has to be "this write does not exist".
    """
    lib = RecordingLib()
    a_bare_ffi(lib).start_live_view()

    assert edsdk_ffi.PROP_EVF_MODE not in lib.writes, (
        "writing Evf_Mode hangs the camera; only the output route may be set"
    )
    assert lib.writes == [edsdk_ffi.PROP_EVF_OUTPUT_DEVICE]


def test_starting_live_view_is_idempotent_and_allocates_once() -> None:
    lib = RecordingLib()
    ffi = a_bare_ffi(lib)

    ffi.start_live_view()
    ffi.start_live_view()

    assert lib.writes == [edsdk_ffi.PROP_EVF_OUTPUT_DEVICE], "second start wrote again"
    assert (lib.streams, lib.refs) == (1, 1), "the EVF stream was allocated twice"
    assert ffi._live is True


def test_stopping_live_view_routes_the_evf_away_and_frees_the_stream() -> None:
    """Leaving live view must reach the body, and must not leak EDSDK objects doing it.

    Stand-down now happens on every screen change and every run, so a stream and an EvfImageRef
    left behind each time is an object leaked per visit. ``Evf_OutputDevice = 0`` is what puts a
    DSLR's mirror back down -- the point of stopping at all.
    """
    lib = RecordingLib()
    ffi = a_bare_ffi(lib)
    ffi.start_live_view()

    ffi.stop_live_view()

    assert lib.writes == [edsdk_ffi.PROP_EVF_OUTPUT_DEVICE] * 2
    assert lib.values == [edsdk_ffi.EVF_OUTPUT_PC, edsdk_ffi.EVF_OUTPUT_OFF]
    assert lib.releases == 2, "the EVF stream and image ref were not released"
    assert ffi._live is False
    assert ffi._stream is None and ffi._evf_image is None

    # And it must be able to come back: a screen change is not a one-way trip.
    ffi.start_live_view()
    assert lib.values[-1] == edsdk_ffi.EVF_OUTPUT_PC
    assert (lib.streams, lib.refs) == (2, 2)


def test_stopping_live_view_that_was_never_started_does_nothing() -> None:
    """The station calls this on every stand-down, including runs where nobody previewed."""
    lib = RecordingLib()
    ffi = a_bare_ffi(lib)

    ffi.stop_live_view()

    assert lib.writes == []
    assert lib.releases == 0


# -- the SDK is a process-wide singleton -------------------------------------
@pytest.fixture
def sdk_state(monkeypatch):
    """The initialisation count is process-global, so each test starts from zero."""
    monkeypatch.setattr(edsdk_ffi, "_SDK_USERS", 0)
    return edsdk_ffi


def test_the_sdk_is_initialised_once_per_process(sdk_state) -> None:
    """``EdsInitializeSDK`` twice in one process fails with 0x2 and never recovers.

    That is what the reconnect button reported: "initialization failed, 0x2". A second
    ``Edsdk`` -- a reconnect, or a retry after a failed connect -- must reuse the
    initialisation that is already up, not perform it again.
    """
    lib = RecordingLib()
    first = a_bare_ffi(lib)
    second = a_bare_ffi(lib)

    first.initialize()
    second.initialize()  # must not raise: this is the reported failure

    assert lib.initialised == 1, "the SDK was initialised twice in one process"
    assert sdk_state._sdk_users() == 2


def test_the_sdk_is_terminated_by_the_last_user(sdk_state) -> None:
    """Whoever closes last does the terminate, so a reopen still works afterwards."""
    lib = RecordingLib()
    first = a_bare_ffi(lib)
    second = a_bare_ffi(lib)
    first.initialize()
    second.initialize()

    first.close()
    assert lib.terminated == 0, "shutting one user down killed the SDK for the other"

    second.close()
    assert lib.terminated == 1
    assert sdk_state._sdk_users() == 0

    # And a fresh instance can initialise again, which is the whole point.
    a_bare_ffi(lib).initialize()
    assert lib.initialised == 2


def test_terminating_is_idempotent(sdk_state) -> None:
    """``disconnect`` may be called twice; the count must not go negative or double-terminate."""
    lib = RecordingLib()
    ffi = a_bare_ffi(lib)
    ffi.initialize()
    ffi.close()
    ffi.close()

    assert lib.terminated == 1
    assert sdk_state._sdk_users() == 0


def test_a_connect_that_finds_no_camera_terminates_the_sdk() -> None:
    """The leak behind the reported bug: init succeeded, so the SDK stayed up.

    ``connect`` initialises, then discovers there is no body -- asleep, unplugged, or held by
    another app. If it walks away without terminating, the *next* connect calls
    ``EdsInitializeSDK`` again and gets 0x2 for the rest of the process's life.
    """
    fake = FakeFfi(devices=[])
    cam = camera_for(fake)

    with pytest.raises(EdsdkUnavailable):
        cam.connect()

    assert fake.closed, "the SDK was left initialised after a failed connect"


def test_a_failed_connect_can_be_retried() -> None:
    """Turn the camera on, press reconnect: the second attempt must be able to initialise."""
    fake = FakeFfi(devices=[])
    cam = camera_for(fake)

    with pytest.raises(EdsdkUnavailable):
        cam.connect()

    fake.devices = [edsdk_ffi.DeviceInfo(port="usb#1", description="Fake EOS", sub_type=1)]
    fake.closed = False
    info = cam.connect()

    assert info.name == "Fake EOS"
    assert not fake.closed, "a successful connect must not close the session it just opened"
