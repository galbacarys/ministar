"""Robust noise estimation for star detection.

Two things live here for one reason: noise scale is the single most load-bearing
number in star detection, and it used to be an unexplained ``1.4826`` duplicated
in two modules. Choosing the wrong estimator was a real bug, not a tidiness issue
(brief 16 §7), so both the constant and the estimator are now named and derived.

Why this matters more than it looks
    Every acceptance threshold in :mod:`ministar.focus.metrics` is a multiple of
    the estimated noise: ``background + k*noise`` to find candidates, then
    ``min_snr * local_noise`` to accept one. If the noise estimate is too small,
    the thresholds move down and **noise itself becomes stars** -- with no error,
    no warning, and a plausible-looking HFD. The failure is silent and the output
    is a number, which is the worst combination.
"""

from __future__ import annotations

from statistics import NormalDist

import numpy as np

#: Converts a median absolute deviation into a standard deviation.
#:
#: For a normal distribution the median absolute deviation is
#: ``Phi^-1(0.75) * sigma = 0.674490 sigma``, so inverting it gives
#:
#:     sigma = MAD / Phi^-1(0.75) = 1.482602218505602 * MAD
#:
#: The value is derived at import rather than pasted, so the relationship cannot
#: drift away from the number. It evaluates to ``1.482602218505602``.
MAD_TO_SIGMA = 1.0 / NormalDist().inv_cdf(0.75)


def mad_sigma(values: np.ndarray) -> float:
    """Sigma from the median absolute deviation.

    This is the textbook robust estimator -- consistent for Gaussian data, and
    unmoved by a handful of stars. It is the right default for a *residual* series
    (see :mod:`ministar.focus.curve`).

    ⚠️ **Not usable on a clipped or one-sided distribution.** On a genuinely black
    frame about half the pixels sit exactly at the dtype floor, so the median is
    that floor and the MAD is **zero**. Use :func:`robust_sigma` for image data.
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    median = float(np.median(arr))
    return MAD_TO_SIGMA * float(np.median(np.abs(arr - median)))


def robust_sigma(values: np.ndarray) -> float:
    """Sigma as ``p84 - p50``. Survives a clipped black floor.

    For any Gaussian, ``p84 = median + sigma`` by definition, so the difference is
    exactly one sigma. Unlike the MAD it only needs the **upper** tail, which is
    what makes it work on a near-black frame where the lower tail has been clipped
    away at zero.

    Measured on synthetic frames, true sigma 3.0 (brief 16 §7):

    | background | MAD estimate | p84 - p50 | truth |
    |---|---|---|---|
    | 0.2 (fully clipped) | 1.66 | **3.00** | 3.0 |
    | 1.0 (partly clipped) | 1.48 | **2.00** | 3.0 |
    | 20.0 (clean) | 2.97 | **3.00** | 3.0 |

    Partially clipped frames still underestimate (the 2.00), so treat this as
    "cannot collapse to zero" rather than "exact". It is never worse than what it
    replaces, and it never returns a value that lets a black frame look starry.
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    sigma = float(np.percentile(arr, 84) - np.percentile(arr, 50))
    if sigma > 0:
        return sigma
    # Constant input: no measurable noise. A tiny floor keeps callers that divide
    # by sigma alive, while still admitting no peaks (a blank frame has none).
    return max(float(arr.std()), 1e-6)
