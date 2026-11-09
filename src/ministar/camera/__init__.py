"""Camera control. See ``docs/research/02-canon-usb-control.md`` and 08/14.

Two backends share one interface:

* :mod:`ministar.camera.edsdk` is the Windows path;
* :mod:`ministar.camera.gphoto2` is the Linux path.

Nothing here imports either backend at import time. ``make_camera()`` chooses by
platform, or by ``MINISTAR_CAMERA=edsdk|gphoto2``. Keeping the import lazy matters on
the Pi: importing the EDSDK module costs nothing on Linux today, but it is the wrong
dependency to drag into a camera-only install, and the reverse is true on Windows.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any

from ministar.camera.base import (
    Camera,
    CameraInfo,
    CameraSettings,
    Frame,
    label_for_seconds,
    snap_shutter,
)

__all__ = [
    "Camera",
    "CameraInfo",
    "CameraSettings",
    "Frame",
    "backend_name",
    "label_for_seconds",
    "make_camera",
    "snap_shutter",
]


def backend_name() -> str:
    """``edsdk``, ``gphoto2``, or the platform default.

    An explicit override is honoured on either platform, which is how the Linux port is
    developed on a Windows desk and how the EDSDK path can still be exercised from WSL
    when the body is attached there.
    """
    override = os.environ.get("MINISTAR_CAMERA", "").strip().lower()
    if override:
        if override not in {"edsdk", "gphoto2"}:
            raise ValueError(
                f"MINISTAR_CAMERA={override!r} is not a backend; use 'edsdk' or 'gphoto2'"
            )
        return override
    return "edsdk" if sys.platform == "win32" else "gphoto2"


def make_camera(**kwargs: Any) -> Camera:
    """Construct the backend for this platform. Does not connect."""
    name = backend_name()
    if name == "edsdk":
        from ministar.camera.edsdk import EdsdkCamera

        return EdsdkCamera(**kwargs)
    from ministar.camera.gphoto2 import Gphoto2Camera

    return Gphoto2Camera(**kwargs)


def __getattr__(name: str) -> Any:
    """Keep the old ``from ministar.camera import EdsdkCamera`` imports working.

    They now resolve lazily, so a Linux install is not forced to import the EDSDK
    module, and ``decode_jpeg`` comes from the platform-neutral implementation.
    """
    if name == "EdsdkCamera":
        from ministar.camera.edsdk import EdsdkCamera

        return EdsdkCamera
    if name == "Gphoto2Camera":
        from ministar.camera.gphoto2 import Gphoto2Camera

        return Gphoto2Camera
    if name == "decode_jpeg":
        from ministar.camera.gphoto2 import decode_jpeg

        return decode_jpeg
    if name in {"CameraNotConnected", "EdsdkUnavailable", "MissingImagingDependency"}:
        module = importlib.import_module("ministar.camera.edsdk")
        return getattr(module, name)
    if name == "edsdk_ffi":
        return importlib.import_module("ministar.camera.edsdk_ffi")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
