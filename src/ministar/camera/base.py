"""Camera abstraction.

The backend is EDSDK over USB via ``ctypes`` (08). Nothing in this module
imports ``ctypes.windll``, so it is testable anywhere.

One design point runs through this file: **the camera's shutter speed is a
discrete table, not a dial.** Canon's Tv values above 1 s are
``1, 1.3, 1.6, 2, 2.5, 3.2, 4, 5, 6, 8, 10, 13, 15, 20, 25, 30``. Brief 03 derives
optimum sub lengths like "2.2 s at 50 mm", which is not requestable. Snapping
has to happen *before* frame counts and SNR are computed, or the arithmetic
quietly disagrees with what the camera will do (08 §6).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np

#: Canon shutter speeds in seconds, longest last. This is the astro-relevant
#: span of the Tv table (the full table also includes 1/8000 .. 1/500).
TV_SECONDS: tuple[float, ...] = (
    1 / 4000,
    1 / 3200,
    1 / 2500,
    1 / 2000,
    1 / 1600,
    1 / 1250,
    1 / 1000,
    1 / 800,
    1 / 640,
    1 / 500,
    1 / 400,
    1 / 320,
    1 / 250,
    1 / 200,
    1 / 160,
    1 / 125,
    1 / 100,
    1 / 80,
    1 / 60,
    1 / 50,
    1 / 40,
    1 / 30,
    1 / 25,
    1 / 20,
    1 / 15,
    1 / 13,
    1 / 10,
    1 / 8,
    1 / 6,
    1 / 5,
    1 / 4,
    0.3,
    0.4,
    0.5,
    0.6,
    0.8,
    1.0,
    1.3,
    1.6,
    2.0,
    2.5,
    3.2,
    4.0,
    5.0,
    6.0,
    8.0,
    10.0,
    13.0,
    15.0,
    20.0,
    25.0,
    30.0,
)

#: Longest exposure reachable without bulb. Beyond this, bulb is required —
#: descoped for v1 because our subs are 0.4-3 s (08 §6).
MAX_TV_SECONDS = TV_SECONDS[-1]


def snap_shutter(seconds: float) -> float:
    """Snap a requested exposure to the nearest legal Tv value.

    Nearest is measured in **log space**, because the steps are 1/3 stop: the
    arithmetic midpoint between 2 s and 3.2 s is 2.7 s, not 2.6 s.
    """
    if seconds <= 0:
        raise ValueError("exposure must be positive")
    if seconds >= MAX_TV_SECONDS:
        return MAX_TV_SECONDS

    target = math.log(seconds)
    return min(TV_SECONDS, key=lambda tv: abs(math.log(tv) - target))


def label_for_seconds(seconds: float) -> str:
    """Human label for a Tv value, e.g. ``'2"'`` or ``'1/500'``."""
    snapped = snap_shutter(seconds)
    if snapped >= 1.0:
        # Above 1 s Canon labels are integral except for the 1/3-stop values,
        # which we render with one decimal.
        return f'{snapped:g}"'
    return f"1/{round(1 / snapped)}"


def fov_height_deg(sensor_height_mm: float, focal_length_mm: float) -> float:
    """Field height in degrees.

    ASTAP's ``-fov`` is the image **height**, not the diameter, despite what its
    own help text says (01 §3). Feed this straight to ``SolveHints``.
    """
    return 2 * math.atan(sensor_height_mm / (2 * focal_length_mm)) * 180 / math.pi


def pixel_scale_arcsec(pixel_um: float, focal_length_mm: float) -> float:
    """Plate scale, the standard ``206.265 * pixel / focal`` relation."""
    return 206.265 * pixel_um / focal_length_mm


@dataclass(frozen=True)
class CameraInfo:
    name: str
    port: str = ""
    category: str = "Canon"


@dataclass(frozen=True)
class CameraSettings:
    """Desired or reported camera state.

    ``mirror_lockup`` matters more than it looks: the `EDS_ERR_DEVICE_BUSY`
    storms in NINA's logs on this rig were caused by a wrong mirror-lockup
    setting, not by transport trouble (08 §7.2b). We check it rather than
    retry through it.
    """

    iso: int
    shutter_seconds: float
    aperture: float | None = None
    mirror_lockup: bool = False


@dataclass(frozen=True)
class Frame:
    """One captured or previewed image."""

    pixels: np.ndarray
    exposure_s: float = 0.0
    iso: int = 0
    captured_at: datetime | None = None
    is_preview: bool = False

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])


@runtime_checkable
class Camera(Protocol):
    """What the rest of the app is allowed to assume about a camera."""

    def connect(self) -> CameraInfo: ...

    def disconnect(self) -> None: ...

    def get_settings(self) -> CameraSettings: ...

    def apply(self, settings: CameraSettings) -> None:
        """Set ISO/Tv/Av, snapping shutter speed to the legal Tv table."""
        ...

    def capture(self, exposure_s: float) -> Frame: ...

    def live_view(self) -> Frame: ...


def nearest_index(seconds: float) -> int:
    """Index of the snapped value in :data:`TV_SECONDS` (used by the EDSDK layer)."""
    return bisect.bisect_left(TV_SECONDS, snap_shutter(seconds))
