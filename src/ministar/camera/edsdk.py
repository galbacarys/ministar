"""Canon EDSDK backend — not implemented yet.

Plan, from ``docs/research/08-edsdk-binding-plan.md``:

Approach
    Fork ``pyedsdk`` (MIT, PyPI 0.2.16) rather than write a binding from
    scratch. It already provides the ctypes plumbing, the ``EdsError`` restype
    hook, the Tv/Av/ISO enum tables, the Windows message pump EDSDK requires,
    and a working ``LiveViewStream``. We vendor it and patch five gaps.

What we must add
    1. ``EdsGetPropertyDesc`` — enumerate legal Tv/Av/ISO for *this* body.
       Without it, an illegal value returns ``EDS_ERR_DEVICE_BUSY``, which is
       indistinguishable from "device busy".
    2. Download to a **memory stream**, not a file: ``EdsGetDirectoryItemInfo``
       -> ``EdsCreateMemoryStream(size)`` -> ``EdsDownload`` ->
       ``EdsDownloadComplete`` -> ``EdsGetPointer``/``EdsGetLength``.
    3. A configurable timeout. Upstream hard-codes ``_waitForEvent(timeout=15)``
       and calls it with no argument, so any exposure >= 15 s raises
       ``TimeoutError``.
    4. ``WillSoonShutDown`` handling via
       ``EdsSendStatusCommand(cam, kEdsCameraStatusCommand_ExtendShutDownTimer, 0)``.
       The camera really does try to sleep mid-session — observed twice in one
       90-minute NINA session (08 §7.4).
    5. Reconnect on ``EDS_ERR_COMM_DISCONNECTED`` / ``EDS_ERR_INVALID_HANDLE``
       (08 §7.3).

What we deliberately do NOT implement
    * **Bulb.** Tv covers 1/32000 s .. 30 s and our subs are 0.4-3 s, so
      ``BulbStart``/``BulbEnd``, the two firing dialects, and the per-body
      quirks (the 500D returning 44313 while succeeding anyway) are all
      unnecessary. See 08 §6. Revisit only for a polar-target mode.
    * **A DEVICE_BUSY retry engine.** The observed failures were a mirror-lockup
      setting, so we check the setting and keep a small bounded retry as
      insurance (08 §7.2b).

Licensing
    ``EDSDK.dll`` is Canon proprietary and must be shipped inside the installer
    and never committed (08 §4). The Python binding is MIT and the notice must
    be preserved.
"""

from __future__ import annotations

from pathlib import Path

from ministar.camera.base import CameraInfo, CameraSettings, Frame


class EdsdkUnavailable(RuntimeError):
    """Raised when EDSDK.dll cannot be located or loaded."""


class EdsdkCamera:
    """Placeholder so the interface shape is visible and importable."""

    def __init__(self, dll_path: Path | None = None) -> None:
        self.dll_path = dll_path

    def connect(self) -> CameraInfo:
        raise NotImplementedError(
            "EDSDK backend not implemented. See docs/research/08-edsdk-binding-plan.md"
        )

    def disconnect(self) -> None:
        raise NotImplementedError

    def get_settings(self) -> CameraSettings:
        raise NotImplementedError

    def apply(self, settings: CameraSettings) -> None:
        raise NotImplementedError

    def capture(self, exposure_s: float) -> Frame:
        raise NotImplementedError

    def live_view(self) -> Frame:
        raise NotImplementedError
