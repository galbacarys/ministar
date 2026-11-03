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

#: Frame-to-frame change on the 32x32 thumbnail, in grey levels, below which a *quiet* field is
#: still quiet. A floor, not the threshold -- see ``MOVING_NOISE_RATIO``.
#:
#: It was the whole threshold until 2026-10-28 (at 5.0 "levels/s"), and on real hardware that
#: made a static scene read as "mount moving" at 9.5. Measured on an EOS 60D pointed at a fixed
#: star field: the per-frame change is **0.9 grey levels** and its whole spread over 59 frames is
#: 0.8-1.2. The old number could not have worked, for two reasons -- it came from the fake
#: camera, and it was a *rate*, so a preview running at 102 ms per frame inflated that 0.9 into
#: 8.8 levels/s. Noise is a property of the sensor and the JPEG, not of time.
MOVING_LEVELS_PER_FRAME = 1.5

#: How far above its own noise floor the picture must move to count as "the mount moved".
#:
#: Measured on the static field above: a floor of 0.9 levels with a spread of +-0.2, so 3x leaves
#: a wide margin. A hand on the head moves whole cells rather than a fraction of one, so it is
#: an order of magnitude beyond that. This is the part that generalises: an ISO change, a darker
#: lens or a noisier sensor all raise the floor and the threshold with it, which one constant
#: could never do.
MOVING_NOISE_RATIO = 3.0

#: HFD jumping around is the other half of the same question, and it uses what the pump already
#: publishes. A defocused frame is *stably* soft; a frame taken while the mount is being moved
#: is soft *erratically*. Relative interquartile spread over the window -- see ``hfd_spread``.
#:
#: Measured on a *static* monitor starfield (60D, 72 live-view frames, 40 stars): HFD ran
#: 4.81-7.10 around a median of 5.99 frame to frame, from the beat between an LCD's refresh and
#: the rolling shutter. As ``(max-min)/median`` that is **38%**, and over some 5 s windows it
#: reached 73% -- so this signal alone called a motionless mount moving. As IQR/median the same
#: data is **12%**. The max-minus-min of a window is one outlier away from lying, in this metric
#: as much as in the thumbnail delta.
HFD_UNSTABLE_FRACTION = 0.5

#: How much history the stillness test looks at.
STILLNESS_WINDOW_S = 5.0

#: Two failed solves in a row is a pattern, not bad luck. The attempt already contains a hinted
#: solve and a blind retry, so a second failure means six or more seconds have gone into
#: nothing and the third press will usually do the same. Stopping is kinder than continuing.
GIVE_UP_AFTER = 2


def should_hold_off(failures: int, *, after: int = GIVE_UP_AFTER) -> bool:
    """Has the solver been tried enough times that the user should have to ask again?"""
    return failures >= after


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


def motion_threshold(noise_floor: float = -1.0) -> float:
    """The rate above which the mount counts as moving, for *this* field's noise.

    ``noise_floor`` is what the same measurement reads when nothing is happening; ``-1`` (no
    reading) falls back to the absolute floor, which is the older behaviour and is what a
    synthetic field needs.
    """
    if noise_floor <= 0.0:
        return MOVING_LEVELS_PER_FRAME
    return max(MOVING_LEVELS_PER_FRAME, MOVING_NOISE_RATIO * noise_floor)


def motion_verdict(
    motion_levels_per_s: float,
    *,
    threshold: float | None = None,
    noise_floor: float = -1.0,
) -> str | None:
    """A sentence when the mount was moving, or ``None``.

    ``-1`` means nothing was sampled, and silence is not stillness -- it says so rather than
    pretending the mount was still. The threshold defaults to ``motion_threshold(noise_floor)``
    rather than the constant: a fixed number cannot work across preview rates and sensors.
    """
    if motion_levels_per_s < 0:
        return None
    limit = motion_threshold(noise_floor) if threshold is None else threshold
    if motion_levels_per_s <= limit:
        return None
    return (
        f"the picture changed {motion_levels_per_s:.1f} levels between frames against a "
        f"{limit:.1f} limit for this field: the mount moved, so the stars are smeared -- "
        "settle it and try again"
    )


def motion_blocked(
    motion_levels: float,
    *,
    ignore: bool = False,
    noise_floor: float = -1.0,
) -> bool:
    """Whether the movement check should hold the shutter, given the operator's override.

    The override exists because this whole gate is a heuristic about the *picture*, and a picture
    can be changing for reasons that have nothing to do with the mount: a starfield displayed on
    a monitor beats against the sensor's rolling shutter, a laptop screen dims, a cloud edge
    crosses. None of that is worth losing a capture over -- the operator can see the mount, and
    the gate cannot. "Inform, never block" applies to the gate itself (README).

    ``ignore=True`` therefore never blocks. The reading is still reported (the caller quotes it),
    so the override changes the decision without hiding the evidence.
    """
    if ignore:
        return False
    return motion_verdict(motion_levels, noise_floor=noise_floor) is not None


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
    def noise_floor(self) -> float:
        """What this field reads when nothing is happening: a low percentile of the window.

        A percentile rather than the median because the window contains the movement being
        judged: a bump lasting a second would drag a median upward and hide itself. The 25th
        percentile sits on the quiet frames unless the mount is moving continuously, which is
        not the case this gate exists for.
        """
        rates = sorted(rate for _, rate, _ in self.samples if rate >= 0)
        if len(rates) < 6:
            return -1.0
        # p10, not p25: the window contains the movement being judged, and a bump lasting a
        # second would drag a higher percentile up and hide itself. Measured with p25: a reading
        # that sat at 18.9 for six seconds lifted its own limit to 18.3 and the gate went quiet.
        return float(rates[max(0, len(rates) // 10)])

    @property
    def threshold(self) -> float:
        return motion_threshold(self.noise_floor)

    @property
    def hfd_spread(self) -> float:
        """Relative *interquartile* spread of the recent HFD samples; 0 when unmeasurable.

        Interquartile, not max-minus-min: a single frame where the detector found a different
        set of stars swings the extremes by tens of percent, which says nothing about the mount.
        Measured on a static field, the two statistics disagree by 3x on identical data (38% vs
        12%), and only the robust one can be compared against a threshold.
        """
        values = [hfd for _, _, hfd in self.samples if math.isfinite(hfd)]
        if len(values) < 6:
            return 0.0
        median = float(np.median(values))
        if median <= 0:
            return 0.0
        q1, q3 = np.percentile(values, [25.0, 75.0])
        return float((q3 - q1) / median)

    @property
    def moving(self) -> bool:
        if self.motion > self.threshold:
            return True
        return self.hfd_spread > HFD_UNSTABLE_FRACTION

    def describe(self) -> str:
        if not self.samples:
            return "no live view yet"
        if self.moving:
            if self.motion > self.threshold:
                return (
                    f"mount moving ({self.motion:.1f} levels between frames, over the "
                    f"{self.threshold:.1f} limit for this field)"
                )
            return f"mount moving (sharpness swinging {self.hfd_spread * 100:.0f}%)"
        return f"mount still ({self.motion:.1f} of {self.threshold:.1f} levels between frames)"


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
    "GIVE_UP_AFTER",
    "HFD_FACTOR",
    "HFD_UNSTABLE_FRACTION",
    "MOVING_LEVELS_PER_FRAME",
    "MOVING_NOISE_RATIO",
    "STILLNESS_WINDOW_S",
    "Stillness",
    "centre_crop",
    "focus_verdict",
    "hfd_arcsec",
    "motion_blocked",
    "motion_threshold",
    "motion_verdict",
    "should_hold_off",
]
