"""Canon EDSDK backend: the verified subset.

Implements :class:`ministar.focus.driver.FocusDriver` completely, so the whole
autofocus routine can run against real hardware, plus the parts of
:class:`ministar.camera.base.Camera` that have been verified.

What is verified on a real EOS 60D (brief 16)
    enumerate, open session, live view (1056x704 JPEG), lens drive including
    **STM focus-by-wire**, and the ``0xA102`` warm-up retry.

What is deliberately not implemented yet
    ``capture``, ``get_settings`` and ``apply``. Setting Tv/Av/ISO needs the
    body's legal value tables (``EdsGetPropertyDesc``) and Canon's Tv encoding,
    and capture needs a memory download path -- none of which has been exercised
    on hardware. Raised explicitly rather than guessed, because a wrong Tv value
    returns ``EDS_ERR_DEVICE_BUSY`` and would look like transport trouble
    (08 §7.2b). See brief 16 §11 for the list.

Design note: the FFI is injected. ``EdsdkCamera`` never constructs ctypes
objects itself, so every branch below is testable against a fake -- which is why
the autofocus logic could be written and thoroughly tested before any camera
existed.
"""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from ministar.camera.base import CameraInfo, Frame
from ministar.camera.edsdk_ffi import (
    FAR_STEPS,
    NEAR_STEPS,
    DeviceInfo,
    Edsdk,
    EdsdkError,
)
from ministar.focus.driver import Direction, FocusSample, PulseResult
from ministar.focus.metrics import measure_hfd

#: Far is Near with this bit set; the whole asymmetry of the drive API.
_FAR_BIT = 0x8000

#: Canon needs a settle between pulses or it coalesces them.
PULSE_SETTLE_S = 0.06

#: Below this many stars a frame cannot inform focus (11 §4).
MIN_STARS = 5


class EdsdkUnavailable(RuntimeError):
    """EDSDK cannot be loaded, or no camera is attached."""


class CameraNotConnected(RuntimeError):
    """An operation was attempted before :meth:`EdsdkCamera.connect`."""


class MissingImagingDependency(RuntimeError):
    """Neither OpenCV nor Pillow is installed, so JPEGs cannot be decoded."""


def decode_jpeg(jpeg: bytes) -> np.ndarray:
    """Decode a live-view JPEG.

    OpenCV is a runtime dependency -- it is also the warp/registration backend for
    the live stack (brief 15) -- so normally this is a one-liner. Pillow is kept
    as a fallback so a source tree with only the ``bench`` extra still works.

    The imports stay inside the function so that importing :mod:`ministar.camera`
    costs nothing and so the failure is actionable rather than at import time.
    """
    try:
        import cv2
    except ImportError:
        pass
    else:
        array = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            raise ValueError("cv2 could not decode the live-view frame")
        return array

    try:
        from io import BytesIO

        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise MissingImagingDependency(
            "OpenCV is a required dependency but is not importable; reinstalling "
            "ministar should restore it. Pillow (pip install ministar[bench]) is "
            "also accepted as a fallback decoder."
        ) from exc

    return np.asarray(Image.open(BytesIO(jpeg)))


def lens_param(direction: Direction) -> int:
    """EDSDK's ``EdsEvfDriveLens`` value for one of our directions."""
    return direction.magnitude if direction.toward_near else _FAR_BIT | direction.magnitude


class EdsdkCamera:
    """A Canon body over USB. Use as a context manager or call connect/close."""

    def __init__(
        self,
        dll_path: Any = None,
        *,
        ffi_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        settle_s: float = PULSE_SETTLE_S,
    ) -> None:
        self.dll_path = dll_path
        self._ffi_factory = ffi_factory
        self._sleep = sleep
        self._settle_s = settle_s
        self._ffi: Any = None
        self._info: DeviceInfo | None = None
        self._position = 0
        self._closed = True

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> CameraInfo:
        if self._ffi is not None:
            raise RuntimeError("camera is already connected")

        factory = self._ffi_factory
        try:
            ffi = factory() if factory is not None else Edsdk(self.dll_path)
        except RuntimeError as exc:
            raise EdsdkUnavailable(str(exc)) from exc

        try:
            ffi.initialize()
            devices = ffi.list_cameras()
        except EdsdkError as exc:
            raise EdsdkUnavailable(f"EDSDK initialisation failed: {exc}") from exc

        if not devices:
            raise EdsdkUnavailable(
                "no camera visible to EDSDK (powered off, asleep, cable in a "
                "charge-only port, or another application holds the session)"
            )

        info = ffi.open_camera(0)
        ffi.start_live_view()
        self._ffi = ffi
        self._info = info
        self._closed = False
        self._position = 0
        return CameraInfo(name=info.description, port=info.port)

    def disconnect(self) -> None:
        ffi, self._ffi = self._ffi, None
        self._closed = True
        if ffi is None:
            return
        # Closing must never raise; a half-open device is better than a crash
        # during teardown.
        with contextlib.suppress(Exception):
            ffi.stop_live_view()
        with contextlib.suppress(Exception):
            ffi.close()

    def __enter__(self) -> EdsdkCamera:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    @property
    def connected(self) -> bool:
        return self._ffi is not None

    def _require(self) -> Any:
        if self._ffi is None:
            raise CameraNotConnected("connect() the camera first")
        return self._ffi

    @property
    def name(self) -> str:
        return self._info.description if self._info else ""

    # -- frame access -------------------------------------------------------
    def grab_live_jpeg(self) -> bytes:
        """Raw live-view JPEG. Exposed so a UI can stream without decoding here."""
        return self._require().grab_jpeg()

    def live_view(self) -> Frame:
        """One live-view frame, decoded. Prefer :meth:`grab_live_jpeg` in a loop."""
        return Frame(pixels=decode_jpeg(self.grab_live_jpeg()), is_preview=True)

    def keep_alive(self) -> None:
        """Push back the auto-power-off timer (08 §7.4)."""
        self._require().keep_alive()

    # -- FocusDriver --------------------------------------------------------
    @property
    def position(self) -> int:
        """Focus position estimate, in Near1-equivalents from the starting point.

        ⚠️ This is a **counter, not a measurement.** The three pulse tiers are not
        proportional to each other, so a ``Near3`` is not really three ``Near1``
        units; the sum is only useful for logging and for detecting that a move
        happened. Anything needing a real position has to come from the HFD sweep
        (11 §2.2), which is why the step-ratio calibration was retired.
        """
        return self._position

    def reset_position(self, position: int = 0) -> None:
        self._position = int(position)

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        """Send ``count`` pulses of ``direction``, updating the position counter."""
        if self._ffi is None:
            return PulseResult(ok=False, moved=False, pulses_sent=0, detail="not connected")

        param = lens_param(direction)
        step = direction.magnitude if direction.toward_near else -direction.magnitude
        count = max(1, int(count))
        sent = 0
        for _ in range(count):
            try:
                self._ffi.drive_lens(param)
            except EdsdkError as exc:
                return PulseResult(
                    ok=False,
                    moved=sent > 0,
                    pulses_sent=sent,
                    detail=f"{exc} (is the lens switch on MF?)",
                )
            sent += 1
            self._position += step
            if self._settle_s:
                self._sleep(self._settle_s)
        return PulseResult(ok=True, moved=True, pulses_sent=sent, detail="")

    def measure(
        self,
        *,
        decode: Callable[[bytes], np.ndarray] | None = None,
        min_stars: int = MIN_STARS,
    ) -> FocusSample:
        """Grab a live-view frame and measure star sharpness.

        A frame that cannot support a measurement is reported as *not accepted*
        rather than raising: rejecting frames is a normal, expected outcome of a
        focus sweep (11 §4).
        """
        if self._ffi is None:
            return FocusSample(
                position=self._position,
                hfd=float("nan"),
                star_count=0,
                accepted=False,
                reason="not connected",
            )
        try:
            jpeg = self.grab_live_jpeg()
        except EdsdkError as exc:
            return FocusSample(
                position=self._position,
                hfd=float("nan"),
                star_count=0,
                accepted=False,
                reason=str(exc),
            )
        if not jpeg:
            return FocusSample(
                position=self._position,
                hfd=float("nan"),
                star_count=0,
                accepted=False,
                reason="empty live-view frame",
            )

        image = (decode or decode_jpeg)(jpeg)
        hfd, stars = measure_hfd(image)

        accepted = stars >= min_stars and math.isfinite(hfd)
        if accepted:
            reason = ""
        elif stars < min_stars:
            reason = f"too few usable stars ({stars} < {min_stars})"
        else:
            reason = "median HFD was not finite"
        return FocusSample(
            position=self._position,
            hfd=float(hfd),
            star_count=int(stars),
            accepted=accepted,
            reason=reason,
        )

    # -- Camera: not yet verified on hardware -------------------------------
    def get_settings(self):
        raise NotImplementedError(
            "reading ISO/Tv needs EdsGetPropertyDesc for this body's legal values; "
            "not yet exercised on hardware (brief 16 §11)"
        )

    def apply(self, settings) -> None:
        raise NotImplementedError(
            "setting ISO/Tv is not yet verified; an illegal Tv value returns "
            "EDS_ERR_DEVICE_BUSY and would masquerade as transport trouble (08 §7.2b)"
        )

    def capture(self, exposure_s: float) -> Frame:
        raise NotImplementedError(
            "capture and memory download are not yet exercised on hardware (brief 16 §11)"
        )


__all__ = [
    "FAR_STEPS",
    "NEAR_STEPS",
    "CameraNotConnected",
    "EdsdkCamera",
    "EdsdkError",
    "EdsdkUnavailable",
    "MissingImagingDependency",
    "decode_jpeg",
    "lens_param",
]
