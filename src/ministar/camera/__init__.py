"""Camera control. See ``docs/research/02-canon-usb-control.md`` and 08."""

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
    "label_for_seconds",
    "snap_shutter",
]
