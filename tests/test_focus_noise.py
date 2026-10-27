"""Noise estimation, including the clipped-black-floor case that broke detection."""

from __future__ import annotations

import numpy as np
import pytest

from ministar.focus.noise import MAD_TO_SIGMA, mad_sigma, robust_sigma


def sample(mean: float, sigma: float = 3.0, size: int = 20_000, seed: int = 5) -> np.ndarray:
    """Synthetic frame shaped like what the camera delivers: 8-bit.

    The dtype matters to the bug. On float data only ~47% of a near-black frame
    clips to zero, so the MAD survives; on 8-bit data the rounding pulls a clear
    majority to exactly 0, which is what collapses the MAD to zero in the field.
    """
    rng = np.random.default_rng(seed)
    return np.clip(rng.normal(mean, sigma, size), 0, 255).astype(np.uint8)


def test_mad_to_sigma_is_the_normal_consistency_constant() -> None:
    """1 / Phi^-1(0.75), the factor that makes MAD an estimate of sigma."""
    assert pytest.approx(1.482602218505602, rel=1e-12) == MAD_TO_SIGMA


def test_both_estimators_recover_sigma_on_a_clean_gaussian() -> None:
    arr = sample(20.0, 3.0)
    assert robust_sigma(arr) == pytest.approx(3.0, abs=0.1)
    assert mad_sigma(arr) == pytest.approx(3.0, abs=0.2)


def test_mad_collapses_at_the_black_floor_but_robust_sigma_does_not() -> None:
    """The exact failure mode: half the pixels pinned at zero -> MAD of zero.

    A zero sigma previously fell back to ``std``, which underestimates on a
    one-sided clipped distribution and let noise through as stars (brief 16 §7).
    """
    arr = sample(0.2, 3.0)
    assert mad_sigma(arr) == 0.0
    assert robust_sigma(arr) == pytest.approx(3.0, abs=0.3)


def test_robust_sigma_never_returns_zero() -> None:
    """Callers divide by sigma; a constant frame must not produce a singularity."""
    assert robust_sigma(np.full(100, 7.0)) > 0.0
    assert robust_sigma(np.empty(0)) == 0.0


def test_robust_sigma_is_not_inflated_by_a_few_stars() -> None:
    """Bright outliers must not be mistaken for noise."""
    arr = sample(200.0, 2.0)
    arr[:50] = 255
    assert robust_sigma(arr) == pytest.approx(2.0, abs=0.2)
