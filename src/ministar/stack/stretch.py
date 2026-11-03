"""STF autostretch, computed once and then deliberately frozen.

Brief 15 §6: the stretch is the only thing the stacking preview has to get right, and
re-deriving it every frame would make the image pulse — which destroys the one piece of
feedback the stack offers, watching the noise fall. So the parameters are measured when
the stack is worth looking at and then held; the UI exposes an explicit "restretch".

The algorithm is PixInsight's Screen Transfer Function, which is the one everybody's eye
is calibrated to:

* ``shadows = median - 2.8 . 1.4826 . MAD`` — a robust black point, so a few bright stars
  or a hot pixel cannot set it;
* a midpoint transfer function whose midpoint is chosen so the median lands at 0.25.

The MAD-to-sigma constant (1.4826) is the Gaussian consistency factor; 2.8sigma is the
customary clip. Both are conventions, not measurements, and are named here so nobody
later "improves" them into a different look.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Where the median should land after the stretch. 0.25 is STF's default and what makes
#: faint nebulosity visible without blowing out the core.
MIDTONES = 0.25

#: Shadows clip, in robust sigmas below the median.
SHADOWS_SIGMA = 2.8

#: The MAD of a Gaussian, in units of its sigma.
MAD_TO_SIGMA = 1.4826

#: Subsampling stride for the statistics. The median of 4.5 MP is ~200 ms (15 §2.2) and
#: buys nothing over 1/16 of the pixels for a single scalar.
SAMPLE_STRIDE = 8

#: LUT size. 16 bits is the depth of the frames; interpolating a larger LUT would add
#: nothing but memory.
_LUT_SIZE = 1 << 16


def _midtone_for(normalised_median: float, target: float = MIDTONES) -> float:
    """Solve ``MTF(m, M) = target`` for ``m``, where ``M`` is the normalised median.

    With ``MTF(m, x) = (m-1)x / ((2m-1)x - m)`` this is one linear equation in ``m``:
    ``m = M(t-1) / (2tM - t - M)``. Written out rather than iterated because a solver in
    the render path is a latency and a nondeterminism at once.
    """
    if not np.isfinite(normalised_median) or normalised_median <= 0.0:
        return 0.5
    if normalised_median >= 1.0:
        # A flat frame has no contrast to stretch; a midpoint of 1 drives everything to
        # black, so hold the neutral value instead.
        return 0.999
    denominator = 2.0 * target * normalised_median - target - normalised_median
    m = normalised_median * (target - 1.0) / denominator
    return float(np.clip(m, 1e-3, 1.0 - 1e-3))


@dataclass(frozen=True)
class Stf:
    """A frozen stretch: two clip points and the midpoint transfer exponent."""

    shadows: float
    white: float
    midtone: float

    def normalise(self, image: np.ndarray) -> np.ndarray:
        span = max(self.white - self.shadows, 1e-9)
        return np.clip((image - self.shadows) / span, 0.0, 1.0)

    def apply(self, image: np.ndarray) -> np.ndarray:
        """Stretch to ``[0, 1]`` float32, same shape as the input."""
        x = self.normalise(np.asarray(image, dtype=np.float32))
        m = self.midtone
        # MTF(m, x), safe at x = 0 (denominator = -m, and the numerator is 0).
        denominator = (2.0 * m - 1.0) * x - m
        safe = np.where(np.abs(denominator) < 1e-9, -1e-9, denominator)
        return np.clip(((m - 1.0) * x) / safe, 0.0, 1.0).astype(np.float32)

    def to_uint8(self, image: np.ndarray) -> np.ndarray:
        return np.clip(self.apply(image) * 255.0 + 0.5, 0, 255).astype(np.uint8)

    @classmethod
    def measure(
        cls, image: np.ndarray, *, stride: int = SAMPLE_STRIDE, target: float = MIDTONES
    ) -> Stf:
        """Measure the clip points and midpoint from one stacked frame.

        ``target`` is where the median lands after the stretch, and it is a *display*
        choice, not part of the STF algorithm. 0.25 is the PixInsight convention, which
        assumes you are processing on a bright desktop; the live preview is looked at in
        the dark (14 §6) and the sky at 25% grey reads as a lit screen with a haze over it.
        See ``LiveStack.SKY_TARGET``.
        """
        data = np.asarray(image, dtype=np.float32)
        sample = data[::stride, ::stride]
        if sample.ndim == 3 and sample.shape[-1] > 1:
            # Luminance, so the three channels share one transfer. Per-channel STF is what
            # makes a calibrated RGB preview drift in colour as the sky changes.
            flat = sample.reshape(-1, sample.shape[-1]).mean(axis=1)
        else:
            flat = sample.reshape(-1)
        flat = flat[np.isfinite(flat)]
        if flat.size == 0:
            return cls(shadows=0.0, white=1.0, midtone=0.5)

        median = float(np.median(flat))
        mad = float(np.median(np.abs(flat - median)))
        sigma = MAD_TO_SIGMA * mad
        shadows = median - SHADOWS_SIGMA * sigma
        white = float(np.max(flat))
        if not np.isfinite(white) or white <= shadows:
            white = float(np.percentile(flat, 99.9))
        if not np.isfinite(white) or white <= shadows:
            white = shadows + 1.0

        normalised_median = (median - shadows) / (white - shadows)
        return cls(
            shadows=shadows,
            white=white,
            midtone=_midtone_for(normalised_median, target),
        )

    def lut(self) -> np.ndarray:
        """The stretch as a 16-bit lookup table, for callers that want to resample."""
        values = np.arange(_LUT_SIZE, dtype=np.float32)
        return self.to_uint8(values)


__all__ = [
    "MAD_TO_SIGMA",
    "MIDTONES",
    "SAMPLE_STRIDE",
    "SHADOWS_SIGMA",
    "Stf",
]
