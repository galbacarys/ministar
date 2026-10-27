"""Fitting the focus V-curve.

The curve of HFD against focus position is a V-shaped minimum, but it is
**asymmetric** — one shoulder falls off much faster than the other — so fitting a
parabola directly to HFD is a poor model and biases the estimate toward the
shallow side. The standard trick, used here, is to fit in ``1 / HFD`` space where
the curve is closer to parabolic:

    1 / HFD  =  a·p² + b·p + c        extremum at  p* = -b / 2a

⚠️ **``1 / HFD`` has a MAXIMUM at best focus, not a minimum** — HFD is smallest when
focused, so its reciprocal is largest. The parabola is therefore concave *down* and
``a`` must be **negative**. Getting this sign wrong rejects every valid curve while
happily accepting monotonic garbage, so it is asserted explicitly below.

Two guards matter more than the fit itself:

* ``a`` must be negative (see above), otherwise there is no peak and we are looking
  at a monotonic run — usually a frame still far from focus.
* ``p*`` must lie near the sampled range. A quadratic extrapolated far past the
  data is fiction, and landing on it wastes a long move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ministar.focus.noise import MAD_TO_SIGMA

#: Below this many usable samples the fit is not worth trusting; do another sweep.
MIN_SAMPLES = 5


@dataclass(frozen=True)
class VCurveFit:
    best_position: float
    best_hfd: float
    a: float
    b: float
    c: float
    r_squared: float
    samples_used: int
    samples_rejected: int


def _prepare(samples: list[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    """Drop unusable samples and collapse duplicate positions.

    Duplicates are real: the refine phase re-measures the position it just landed
    on. Keeping the *sharpest* reading per position is the useful choice, and
    without this a couple of repeats can leave fewer than ``MIN_SAMPLES`` distinct
    positions and silently kill the fit.
    """
    best_at: dict[float, float] = {}
    for position, hfd in samples:
        if not (math.isfinite(hfd) and hfd > 0):
            continue
        key = float(position)
        value = 1.0 / hfd
        if key not in best_at or value > best_at[key]:
            best_at[key] = value

    positions = sorted(best_at)
    return np.asarray(positions), np.asarray([best_at[p] for p in positions])


def _fit_quadratic(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    a, b, c = np.polyfit(x, y, 2)
    predicted = a * x * x + b * x + c
    ss_res = float(((y - predicted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return float(a), float(b), float(c), r_squared


def fit_v_curve(
    samples: list[tuple[float, float]],
    *,
    max_extrapolation_units: float | None = None,
    outlier_sigma: float = 2.5,
    window: int = 3,
) -> VCurveFit | None:
    """Fit a minimum to ``(position, hfd)`` samples.

    Only the samples bracketing the measured minimum are fitted. A single
    parabola across the whole sweep is badly biased, because the flat shoulders far
    from focus dominate the least-squares solution and drag the estimate away from
    the bottom — measured 520 vs a true 500 on a synthetic curve. Real focus
    implementations fit locally for the same reason.

    Returns ``None`` when the data cannot support a minimum: too few points, a
    non-convex fit, or an optimum extrapolated outside the fitted window.
    """
    x, y = _prepare(samples)
    if len(x) < MIN_SAMPLES:
        return None

    # Work in position order and keep a window around the sharpest sample.
    order = np.argsort(x)
    x, y = x[order], y[order]
    sharpest = int(np.argmax(y))  # 1/HFD is largest at best focus
    lo = max(0, sharpest - window)
    hi = min(len(x), sharpest + window + 1)
    x, y = x[lo:hi], y[lo:hi]

    if len(x) < MIN_SAMPLES or len(np.unique(x)) < MIN_SAMPLES:
        return None

    a, b, c, r2 = _fit_quadratic(x, y)

    # One robust pass: drop points the first fit explains badly, then refit.
    rejected = 0
    if outlier_sigma > 0 and len(x) > MIN_SAMPLES:
        residual = np.abs(y - (a * x * x + b * x + c))
        # Residuals about the fit, not about their own median: this is a scale
        # estimate for the fit, and MAD_TO_SIGMA names the 1.4826 conversion.
        scale = MAD_TO_SIGMA * float(np.median(residual))
        if scale > 0:
            keep = residual <= outlier_sigma * scale
            rejected = int((~keep).sum())
            if keep.sum() >= MIN_SAMPLES and len(np.unique(x[keep])) >= MIN_SAMPLES:
                x, y = x[keep], y[keep]
                a, b, c, r2 = _fit_quadratic(x, y)

    # 1/HFD peaks at best focus, so the fitted parabola must open downwards.
    if a >= 0:
        return None

    best_position = -b / (2 * a)
    lo, hi = float(x.min()), float(x.max())
    margin = max_extrapolation_units
    if margin is None:
        # Default: tolerate one sample spacing beyond either end, no more.
        spacing = float(np.median(np.diff(np.sort(x)))) if len(x) > 1 else 1.0
        margin = spacing
    if not (lo - margin <= best_position <= hi + margin):
        return None

    best_inv = a * best_position * best_position + b * best_position + c
    if best_inv <= 0:
        return None

    return VCurveFit(
        best_position=best_position,
        best_hfd=1.0 / best_inv,
        a=a,
        b=b,
        c=c,
        r_squared=r2,
        samples_used=len(x),
        samples_rejected=rejected,
    )


def depth_of_minimum(samples: list[tuple[float, float]], fit: VCurveFit) -> float:
    """How much the measured best beats the sampled worst, as an HFD ratio.

    A shallow V is a warning: on a short lens the focus curve barely moves, so the
    "minimum" is mostly noise and we should prefer the safer stop-based position
    (11 §3). Ratio is ``worst_hfd / best_hfd``; ≥1.0 always, and near 1.0 means
    there is no usable signal.
    """
    hfds = [h for _, h in samples if math.isfinite(h) and h > 0]
    if len(hfds) < 2:
        return 1.0
    worst = max(hfds)
    return worst / fit.best_hfd if fit.best_hfd > 0 else 1.0
