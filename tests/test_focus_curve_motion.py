"""V-curve fitting and focus move planning."""

from __future__ import annotations

import math

import pytest

from ministar.focus.curve import fit_v_curve
from ministar.focus.driver import Direction
from ministar.focus.motion import DEFAULT_RATIOS, plan_move

REAL_RATIOS = {Direction.NEAR_1: 1.0, Direction.NEAR_2: 4.0, Direction.NEAR_3: 12.0}


def hyperbola(p: float, p0: float = 500.0, hmin: float = 2.0, w: float = 40.0) -> float:
    return hmin * math.sqrt(1.0 + ((p - p0) / w) ** 2)


def asymmetric(p: float, p0: float = 500.0, hmin: float = 2.0) -> float:
    w = 25.0 if p < p0 else 60.0
    return hmin * math.sqrt(1.0 + ((p - p0) / w) ** 2)


def test_recovers_symmetric_minimum() -> None:
    samples = [(p, hyperbola(p)) for p in range(380, 640, 20)]
    fit = fit_v_curve(samples)
    assert fit is not None
    assert abs(fit.best_position - 500.0) < 8.0
    assert fit.best_hfd == pytest.approx(2.0, rel=0.15)


def test_recovers_asymmetric_minimum() -> None:
    """The whole reason for fitting 1/HFD: the real curve is asymmetric.

    A symmetric parabola is inevitably dragged toward the shallow shoulder, so we
    assert on the *resulting blur* rather than the position — what matters is that
    landing there gets you to focus, not that you agree with the true minimum to the
    unit. Measured bias here is ~14 units on a 40-unit curve width, which leaves the
    HFD 2.7% above optimum.
    """
    samples = [(p, asymmetric(p)) for p in range(380, 640, 20)]
    fit = fit_v_curve(samples)
    assert fit is not None
    assert abs(fit.best_position - 500.0) < 25.0
    assert asymmetric(fit.best_position) < 2.0 * 1.05


def test_outlier_does_not_shift_the_minimum() -> None:
    samples = [(p, hyperbola(p)) for p in range(380, 640, 20)]
    samples[3] = (samples[3][0], samples[3][1] * 3.0)  # a cloud-ruined sample
    fit = fit_v_curve(samples)
    assert fit is not None
    assert abs(fit.best_position - 500.0) < 12.0


def test_monotonic_data_has_no_minimum() -> None:
    samples = [(p, 10.0 - p / 100.0) for p in range(0, 200, 20)]
    assert fit_v_curve(samples) is None


def test_too_few_samples() -> None:
    assert fit_v_curve([(100, 3.0), (120, 2.5), (140, 3.1)]) is None


def test_minimum_outside_range_is_rejected() -> None:
    """Extrapolating a quadratic past the data is fiction, not an estimate."""
    samples = [(p, hyperbola(p, p0=5000.0, w=40.0)) for p in range(380, 640, 20)]
    assert fit_v_curve(samples) is None


def test_duplicate_positions_are_collapsed() -> None:
    """The refine phase re-measures a position; that must not kill the fit."""
    samples = [(p, hyperbola(p)) for p in range(380, 640, 20)]
    samples += [(500.0, hyperbola(500.0)), (500.0, hyperbola(500.0) * 1.2)]
    fit = fit_v_curve(samples)
    assert fit is not None
    assert abs(fit.best_position - 500.0) < 8.0


def test_nan_samples_ignored() -> None:
    samples = [(p, hyperbola(p)) for p in range(380, 640, 20)]
    samples.append((999, float("nan")))
    samples.append((1000, 0.0))
    fit = fit_v_curve(samples)
    assert fit is not None
    assert abs(fit.best_position - 500.0) < 8.0


def test_move_uses_large_tiers_when_calibrated() -> None:
    plan = plan_move(5030, REAL_RATIOS)
    assert plan.within_budget
    # 5030 = 419*12 + 2, so 419 Near3 plus 2 Near1.
    assert plan.total_pulses == 421
    assert plan.steps[0] == (Direction.NEAR_3, 419)
    assert plan.units == 5030


def test_move_beats_the_reference_implementation() -> None:
    """The reference sends one pulse per 100 ms; at 5030 units that is 8.4 minutes."""
    naive = plan_move(5030, DEFAULT_RATIOS)
    calibrated = plan_move(5030, REAL_RATIOS)
    assert naive.total_pulses == 5030
    assert calibrated.total_pulses < naive.total_pulses / 10


def test_move_direction_sign() -> None:
    towards_near = plan_move(100, REAL_RATIOS)
    towards_far = plan_move(-100, REAL_RATIOS)
    assert all(d.toward_near for d, _ in towards_near.steps)
    assert all(not d.toward_near for d, _ in towards_far.steps)


def test_zero_move_is_empty() -> None:
    plan = plan_move(0, REAL_RATIOS)
    assert plan.is_empty
    assert plan.total_pulses == 0


def test_budget_is_reported() -> None:
    plan = plan_move(5030, DEFAULT_RATIOS, max_pulses=100)
    assert not plan.within_budget


def test_default_ratios_never_use_unmeasured_tiers() -> None:
    """Guessing a tier ratio is worse than not using the tier at all."""
    plan = plan_move(37, DEFAULT_RATIOS)
    assert {d for d, _ in plan.steps} == {Direction.NEAR_1}
    assert plan.units == 37
