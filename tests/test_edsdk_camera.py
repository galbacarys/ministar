"""EdsdkCamera protocol logic, against a fake FFI.

No camera and no EDSDK.dll involved. The FFI is injected precisely so that the
decisions that can be wrong -- direction mapping, position bookkeeping, pulse
counts, frame acceptance -- are testable anywhere, on any OS.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ministar.camera import edsdk_ffi
from ministar.camera.edsdk import (
    CameraNotConnected,
    EdsdkCamera,
    EdsdkUnavailable,
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
        self.sent: list[int] = []
        self.keepalives = 0
        self.initialized = False
        self.live = False
        self.closed = False
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

    def close(self) -> None:
        self.closed = True


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
def test_connect_reports_device_and_starts_live_view() -> None:
    fake = FakeFfi()
    cam = camera_for(fake)
    info = cam.connect()

    assert info.name == "Fake EOS"
    assert info.port == "usb#1"
    assert fake.initialized and fake.live
    assert cam.connected


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


# -- honest gaps ------------------------------------------------------------
@pytest.mark.parametrize("method", ["get_settings", "capture"])
def test_unverified_paths_raise_clearly(method: str) -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    with pytest.raises(NotImplementedError, match="brief 16"):
        getattr(cam, method)(1.0) if method == "capture" else getattr(cam, method)()


def test_apply_raises_rather_than_guessing_a_shutter_speed() -> None:
    cam = camera_for(FakeFfi())
    cam.connect()
    with pytest.raises(NotImplementedError, match="DEVICE_BUSY"):
        cam.apply(object())


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
