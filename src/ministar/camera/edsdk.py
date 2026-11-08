"""Canon EDSDK backend: the verified subset.

Implements :class:`ministar.focus.driver.FocusDriver` completely, so the whole
autofocus routine can run against real hardware, plus the parts of
:class:`ministar.camera.base.Camera` that have been verified.

What is verified on a real EOS 60D (brief 16)
    enumerate, open session, live view (1056x704 JPEG), lens drive including
    **STM focus-by-wire**, and the ``0xA102`` warm-up retry.

What is deliberately not implemented yet
    ``capture``: it needs a raw decoder, and the session captures *files* through
    ``capture_to`` instead (16 §11).

How ISO/Tv are set (2026-10-28)
    The body's own ``EdsGetPropertyDesc`` narrows Canon's code tables to the values
    *this* body offers, and every write is read back. An illegal Tv is reported by the
    SDK as ``0x81`` (DEVICE_BUSY), indistinguishable from a transport fault, so the
    read-back is what separates "the camera refused the value" from "the cable is
    unhappy" (08 §7.2b).

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
from pathlib import Path
from typing import Any

import numpy as np

from ministar.camera import edsdk_ffi
from ministar.camera.base import (
    CameraInfo,
    CameraSettings,
    Frame,
    label_for_seconds,
    snap_shutter,
)
from ministar.camera.edsdk_codes import (
    ISO_LADDER,
    av_fnumber,
    iso_code_for,
    iso_value,
    tv_code_for,
    tv_seconds,
)
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

#: How long to keep trying to open a session that a dead process may still hold.
#:
#: Windows releases the old USB handle when the process dies, but a body left in PC
#: live view does not necessarily accept the next session on the first ask. Measured
#: in the field: restarting the console during live view made connect fail until the
#: camera was power-cycled. A short retry costs nothing on a healthy connect and is
#: the difference between a restart and a body that looks bricked.
CONNECT_ATTEMPTS = 12
CONNECT_RETRY_S = 0.25

#: Below this many stars a frame cannot inform focus (11 §4).
MIN_STARS = 5


class EdsdkUnavailable(RuntimeError):
    """EDSDK cannot be loaded, or no camera is attached."""


class CameraNotConnected(RuntimeError):
    """An operation was attempted before :meth:`EdsdkCamera.connect`."""


class MissingImagingDependency(RuntimeError):
    """Neither OpenCV nor Pillow is installed, so JPEGs cannot be decoded."""


def _close_quietly(ffi: Any) -> None:
    """Release an SDK session we are abandoning; never mask the real error."""
    close = getattr(ffi, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        close()


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
        except EdsdkError as exc:
            # Terminate on the way out. The SDK is initialised per *process*, so leaving it up
            # after a failure poisons every later attempt: the next connect() calls
            # EdsInitializeSDK again and gets 0x2 (EDS_ERR_INTERNAL_ERROR) forever. That is
            # exactly how "reconnect" reported "initialization failed" and never recovered.
            _close_quietly(ffi)
            raise EdsdkUnavailable(f"EDSDK initialisation failed: {exc}") from exc

        # A body left in PC live view by a killed process can refuse or stall the first
        # open: the old handle is released asynchronously by Windows, and the camera may
        # still believe the EVF stream has an owner. Retry briefly rather than declaring
        # the camera missing, then clear any stale stream once a session exists.
        last: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                devices = ffi.list_cameras()
            except EdsdkError as exc:
                last = exc
                devices = []
            if devices:
                try:
                    info = ffi.open_camera(0)
                except Exception as exc:
                    last = exc
                else:
                    # The recovery for a process that died in live view: force EVF output
                    # to nobody before this instance allocates a stream. Harmless when it
                    # is already off, and the only reset available without a session.
                    with contextlib.suppress(Exception):
                        ffi.evf_off()
                    # Deliberately *not* start_live_view() here. On a DSLR that raises the
                    # mirror and cycles the shutter: doing it at connect time made launching
                    # the app look like it had fired a capture, and tied the body up before
                    # the user had asked for anything. The first grab_live_jpeg() (i.e. a
                    # screen that shows a preview) starts it.
                    self._ffi = ffi
                    self._info = info
                    self._closed = False
                    self._position = 0
                    return CameraInfo(name=info.description, port=info.port)
            if attempt + 1 < CONNECT_ATTEMPTS:
                self._sleep(CONNECT_RETRY_S)

        _close_quietly(ffi)
        if last is not None:
            raise EdsdkUnavailable(
                f"EDSDK could not open the camera after {CONNECT_ATTEMPTS} attempts: {last}"
            ) from last
        raise EdsdkUnavailable(
            "no camera visible to EDSDK (powered off, asleep, cable in a "
            "charge-only port, or another application holds the session)"
        )

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
        """Raw live-view JPEG, starting live view on first use.

        Exposed so a UI can stream without decoding here. Starting LV here rather than in
        ``connect`` keeps the mirror down until a screen actually needs a preview.
        """
        ffi = self._require()
        ffi.start_live_view()
        return ffi.grab_jpeg()

    def live_view(self) -> Frame:
        """One live-view frame, decoded. Prefer :meth:`grab_live_jpeg` in a loop."""
        return Frame(pixels=decode_jpeg(self.grab_live_jpeg()), is_preview=True)

    def stop_live_view(self) -> None:
        """Leave live view: the body drops its mirror and stops streaming.

        Live view is not this instrument's imaging state -- the shutter is -- so nothing should
        hold the mirror up once a screen stops showing a preview. Safe to call when live view
        was never started, which is what lets the station call it on every stand-down.
        """
        ffi = self._ffi
        if ffi is not None:
            ffi.stop_live_view()

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

    # -- camera settings -----------------------------------------------------
    def iso_options(self) -> tuple[int, ...]:
        """The ISO values this project will offer for a Canon body: 100-6400 in 1/3 stops.

        Read from the code table rather than from ``EdsGetPropertyDesc``, which is unusable
        on this body (see the module note). Every value here is a legal Canon ISO; a body
        with a narrower range would reject one on write, and the read-back would say so.
        """
        return ISO_LADDER

    def get_settings(self) -> CameraSettings:
        """What the dials say right now, decoded from Canon's property codes."""
        ffi = self._require()
        iso_code = ffi.uint_property(edsdk_ffi.PROP_ISO_SPEED)
        tv_code = ffi.uint_property(edsdk_ffi.PROP_TV)
        try:
            av_code = ffi.uint_property(edsdk_ffi.PROP_AV)
        except edsdk_ffi.EdsdkError:  # a body with no electronic aperture
            av_code = None
        return CameraSettings(
            iso=iso_value(iso_code) or 0,
            # Bulb reads back as 0x0C, whose seconds are deliberately None: the session
            # cannot run at bulb (08 §6), and 0.0 says so rather than inventing 30 s.
            shutter_seconds=tv_seconds(tv_code) or 0.0,
            aperture=None if av_code is None else av_fnumber(av_code),
        )

    def apply(self, settings) -> None:
        """Set ISO and Tv, restricted to the body's own legal values and read back.

        The read-back is not ceremony. Canon reports an illegal property value as
        ``EDS_ERR_DEVICE_BUSY`` (0x81), the same code as a busy transport, so without it a
        rejected shutter speed looks exactly like a bad cable -- the failure mode brief 08
        §7.2b exists to prevent. Worse, the camera can silently clamp to a *different* legal
        value, which would put the wrong exposure duration on every frame of a run.
        """
        ffi = self._require()
        iso_code = iso_code_for(settings.iso)
        self._set_checked(ffi, edsdk_ffi.PROP_ISO_SPEED, iso_code, f"ISO {settings.iso}")
        seconds = snap_shutter(settings.shutter_seconds)
        code = tv_code_for(seconds)
        self._set_checked(ffi, edsdk_ffi.PROP_TV, code, f"shutter {label_for_seconds(seconds)}")

    @staticmethod
    def _set_checked(ffi, prop_id: int, code: int, what: str) -> None:
        ffi.set_uint_property(prop_id, code)
        readback = ffi.uint_property(prop_id)
        if readback != code:
            raise RuntimeError(
                f"the camera did not accept {what}: wrote code 0x{code:02X} and it read "
                f"back 0x{readback:02X} (is the mode dial set to M?)"
            )

    def capture(self, exposure_s: float) -> Frame:
        """Not implemented: a CR2 cannot be decoded into an array without a raw engine.

        The session captures *files* anyway -- see :meth:`capture_to`, which is the
        path the run loop and the rig probe both use.
        """
        raise NotImplementedError(
            "capture() returns a decoded array and no raw decoder is wired in; use "
            "capture_to(path) for the file the camera produced (brief 16 §11)"
        )

    def capture_to(self, path: Path, *, timeout_s: float = 120.0) -> Path:
        """Take one picture and download it to ``path``. Returns the path written.

        The download protocol lives in the FFI so this stays a sequence of four
        decisions -- where files go, arm the handler, shoot, wait -- and can be
        driven by a fake FFI in tests (which is how the ordering got checked before
        any camera was involved).

        ``timeout_s`` must cover the exposure *and* the card write: a 30 s sub is not
        a hung camera. On timeout any offered item is cancelled, because a body left
        holding an unclaimed transfer stops shooting (02 §1.7).
        """
        ffi = self._require()
        ffi.set_save_to_host()
        ffi.arm_transfer_handler()
        try:
            ffi.take_picture()
            item = ffi.wait_for_transfer(timeout_s=timeout_s)
        except Exception:
            ffi.drop_transfers()
            raise
        return ffi.download_to(item, Path(path))

    @property
    def dll_source(self) -> tuple[Path, str] | None:
        """Which ``EDSDK.dll`` this camera would use, and whether it is ours."""
        if self.dll_path is not None:
            return Path(self.dll_path), "explicit"
        try:
            return edsdk_ffi.resolve_dll(None)
        except RuntimeError:
            return None


#: ⚠️ ``EdsGetPropertyDesc`` is **not used** (measured on the EOS 60D, 2026-10-28).
#:
#: Through this binding the descriptor reports ``numElements = 3`` and hands back
#: ``[0x37, 0x10, 0x13]`` for Tv -- a list whose first entry is not a Tv code and whose real
#: ladder starts one slot later; the body's own current value is absent. A first attempt
#: guarded it by "believe the list only if it contains the current value", which failed
#: *catastrophically*: the garbage list contains 0x13 (25 s), and once an earlier bug had
#: put the body at 25 s the guard passed, snapping a 1.6 s request to 25 s. Read-back
#: verified the code, not the seconds, so a 16x overexposure looked successful.
#:
#: The static table plus read-back is the authority until the struct layout is understood
#: against a real SDK build. The FFI keeps ``property_desc`` for that work; nothing here
#: calls it.


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
