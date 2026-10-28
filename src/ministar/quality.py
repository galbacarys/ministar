"""Can this frame be solved -- and if not, why?

A solver reports the same thing for two failures that have nothing in common: a frame gone soft
because focus crept while the lens cooled, and a frame taken with a hand still on the mount.
"no solution found" sends the user looking for a hardware fault that is not there. The
measurements that do distinguish them are cheap; this module is the policy on top of them.

Two scales matter, and getting one wrong makes the whole gate useless:

- **arcseconds, never pixels.** A binned live view (4.93x on the 60D) and a full-resolution
  still are not comparable in pixels, and HFD comes back in pixels.
- **same pipeline.** HFD is threshold- and SNR-dependent: the same scene measured on a full
  frame, a 1200^2 crop and a 600^2 crop gave 3.12, 2.64 and 2.35 px. A baseline is only a
  baseline if it was measured the way the check is (11 §"quality gate").
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

#: HFD this many times the best-focus value is not bad luck. Blunt on purpose: the question is
#: "should the user look at the focus?", not "what happened to it?".
HFD_FACTOR = 2.0

#: Frame-to-frame change on the 32x32 thumbnail, in grey levels per second, above which the
#: mount is being moved. Measured on the fake (11 §"quality gate"): the noise floor and a
#: drifting sky are indistinguishable at **2.4-2.9 levels/s** (the frame-to-frame noise
#: dominates), while a hand moving the head by 5' between frames gives 4.0-8.5. So the
#: threshold sits above the noise, which means bumps smaller than a few arcminutes are not
#: visible here -- and a bump that small would usually still solve.
MOVING_LEVELS_PER_S = 5.0

#: HFD jumping around is the other half of the same question, and it uses what the pump already
#: publishes. A defocused frame is *stably* soft; a frame taken while the mount is being moved
#: is soft *erratically*. Relative spread of the median over the window.
HFD_UNSTABLE_FRACTION = 0.5

#: How much history the stillness test looks at.
STILLNESS_WINDOW_S = 5.0

#: A centre crop this wide costs 0.06 s to measure, against 0.80 s for the whole frame -- a gate
#: that costs what the solve costs is not a gate.
CROP_PX = 1200


def hfd_arcsec(hfd_px: float, *, width_px: int, width_arcsec: float) -> float:
    """Pixel HFD to arcseconds, using the frame it was measured on.

    ``width_arcsec`` is the angular width those ``width_px`` pixels cover, which is the one way
    to compare a full-resolution still with a binned live view without knowing the binning.
    """
    if width_px <= 0 or not math.isfinite(hfd_px) or not math.isfinite(width_arcsec):
        return float("nan")
    return hfd_px * width_arcsec / width_px


def focus_verdict(
    hfd_arcsec_value: float,
    baseline_arcsec: float | None,
    *,
    factor: float = HFD_FACTOR,
) -> str | None:
    """A sentence when the frame is soft, or ``None`` when there is nothing to report."""
    if not math.isfinite(hfd_arcsec_value):
        return "cannot judge focus: the frame is saturated or has no usable stars"
    if baseline_arcsec is None or not math.isfinite(baseline_arcsec) or baseline_arcsec <= 0:
        return None
    if hfd_arcsec_value < factor * baseline_arcsec:
        return None
    return (
        f'stars are {hfd_arcsec_value:.1f}" across, against {baseline_arcsec:.1f}" at best '
        "focus: focus has drifted, or the frame is smeared"
    )


def motion_verdict(
    motion_levels_per_s: float, *, threshold: float = MOVING_LEVELS_PER_S
) -> str | None:
    """A sentence when the mount was moving, or ``None``.

    ``-1`` means nothing was sampled, and silence is not stillness -- it says so rather than
    pretending the mount was still.
    """
    if motion_levels_per_s < 0:
        return None
    if motion_levels_per_s <= threshold:
        return None
    return (
        f"the picture was changing at {motion_levels_per_s:.1f} levels/s: the mount moved, "
        "so the stars are smeared -- settle it and try again"
    )


@dataclass
class Stillness:
    """Is the mount being moved? Fed one snapshot per UI tick, so it costs nothing.

    Two signals, neither sufficient alone:

    - **the picture is changing faster than the sky explains** (``Snapshot.motion``): catches a
      hand on the head, but its noise floor means it misses movement under a few arcminutes.
    - **HFD is bouncing** relative to its own median: a defocused frame is stably soft, so
      erratic sharpness means the mount, not the focus. Catches what the first signal is too
      deaf for, and is the half that distinguishes the two failures.

    Not a substitute for a settle pause: it says whether the mount *was* moving, and the useful
    question before a capture is whether it has stopped.
    """

    window_s: float = STILLNESS_WINDOW_S
    samples: deque[tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=200))

    def observe(self, snapshot, now: float) -> None:
        """Record one live-view sample. ``hfd`` may be nan when nothing was detected."""
        self.samples.append((now, float(snapshot.motion), float(snapshot.hfd)))
        while self.samples and now - self.samples[0][0] > self.window_s:
            self.samples.popleft()

    @property
    def motion(self) -> float:
        rates = [rate for _, rate, _ in self.samples if rate >= 0]
        return max(rates) if rates else -1.0

    @property
    def hfd_spread(self) -> float:
        """Relative spread of the recent HFD samples; 0 when there is nothing to compare."""
        values = [hfd for _, _, hfd in self.samples if math.isfinite(hfd)]
        if len(values) < 3:
            return 0.0
        median = float(np.median(values))
        if median <= 0:
            return 0.0
        return float((max(values) - min(values)) / median)

    @property
    def moving(self) -> bool:
        if self.motion > MOVING_LEVELS_PER_S:
            return True
        return self.hfd_spread > HFD_UNSTABLE_FRACTION

    def describe(self) -> str:
        if not self.samples:
            return "no live view yet"
        if self.moving:
            if self.motion > MOVING_LEVELS_PER_S:
                return f"mount moving ({self.motion:.1f} levels/s)"
            return f"mount moving (sharpness swinging {self.hfd_spread * 100:.0f}%)"
        return "mount still"


def centre_crop(image, *, size: int = CROP_PX):
    """The middle of a frame, which is where the gate measures (see CROP_PX)."""
    height, width = image.shape[:2]
    w = min(size, width)
    h = min(size, height)
    x0 = (width - w) // 2
    y0 = (height - h) // 2
    return image[y0 : y0 + h, x0 : x0 + w]


__all__ = [
    "CROP_PX",
    "HFD_FACTOR",
    "HFD_UNSTABLE_FRACTION",
    "MOVING_LEVELS_PER_S",
    "STILLNESS_WINDOW_S",
    "Stillness",
    "centre_crop",
    "focus_verdict",
    "hfd_arcsec",
    "motion_verdict",
]
