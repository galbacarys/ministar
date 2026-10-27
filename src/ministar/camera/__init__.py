"""Camera control. See ``docs/research/02-canon-usb-control.md`` and 08."""

from ministar.camera.base import (
    Camera,
    CameraInfo,
    CameraSettings,
    Frame,
    label_for_seconds,
    snap_shutter,
)

# Imported last: edsdk pulls in ctypes and the focus metrics, and the base types
# above are what most callers want. Importing the backend here is safe because
# EDSDK.dll is only loaded when a camera is actually constructed.
from ministar.camera.edsdk import (
    CameraNotConnected,
    EdsdkCamera,
    EdsdkUnavailable,
    MissingImagingDependency,
    decode_jpeg,
)

__all__ = [
    "Camera",
    "CameraInfo",
    "CameraNotConnected",
    "CameraSettings",
    "EdsdkCamera",
    "EdsdkUnavailable",
    "Frame",
    "MissingImagingDependency",
    "decode_jpeg",
    "label_for_seconds",
    "snap_shutter",
]
