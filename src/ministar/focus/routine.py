"""The autofocus routine.

Implements 11 §5: home to the far stop, acquire stars, sweep the V-curve, fit,
refine, verify. Every phase has a hard exit condition, because the dominant
failure mode for this feature is an unbounded loop on a cloudy night.

The routine is pure logic against :class:`FocusDriver`, so it can be exercised
against a synthetic curve with no camera present (11 §10).

Every deliberate movement goes through :func:`_move_to`, which speaks in
**Near1-equivalents from the stop** and delegates the pulse decomposition to
:mod:`ministar.focus.motion`. Nothing else in this file reasons about pulses,
which is what stops the units from getting confused.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from ministar.focus.curve import VCurveFit, depth_of_minimum, fit_v_curve
from ministar.focus.driver import Direction, FocusDriver, FocusSample
from ministar.focus.motion import DEFAULT_RATIOS, StepRatios, plan_move


class Start(Enum):
    """Where the routine begins from.

    ``COLD`` is for the first focus of a session: hammer the far stop to establish
    the coordinate origin, then search for stars. That search is unavoidably slow
    (at 400 mm the focus sits ~5030 units from the stop), which is why the session
    wizard has the user aim it by hand first (12 §3).

    ``AT_CURRENT`` is every subsequent focus: the position counter is already
    anchored, stars are already in view, and we only need a local sweep. This is
    the mode used by the per-reframe check.
    """

    COLD = "cold"
    AT_CURRENT = "at_current"


@dataclass(frozen=True)
class RoutineConfig:
    #: Star count needed before we believe we are looking at sky.
    min_stars: int = 5
    #: Upper bound on homing pulses. Reaching the stop is detected, not assumed.
    home_pulses: int = 80
    #: Acquire: step back from the stop until stars appear.
    #:
    #: The default budget must cover the worst realistic case. At 400 mm the focus
    #: sits ~5030 units from the stop (11 §2.2), so a 700-unit budget could never
    #: reach it -- a latent bug that the manual-bootstrap step in 12 §3 makes
    #: mostly moot, but which should not be left in the code.
    acquire_step_units: int = 16
    acquire_max_units: int = 8000
    #: The stride doubles every this many fruitless steps, so a long search is
    #: covered quickly while a short one stays fine-grained. Capped at 4x so we can
    #: never step over the in-focus window, which is ~100-350 units wide.
    acquire_widen_every: int = 8
    #: Sweep: coarse stride once we have stars.
    sweep_step_units: int = 24
    max_sweep_samples: int = 24
    #: Consecutive worsening samples past the best before we stop sweeping.
    worsening_run: int = 2
    refine_iterations: int = 2
    #: Land when the measured HFD is within this fraction of the fitted minimum.
    hfd_tolerance: float = 0.06
    #: Total pulse budget: the global anti-hang guard.
    max_total_pulses: int = 4000
    #: A curve shallower than this is mostly noise (11 §3, short lenses).
    min_curve_depth: float = 1.10


@dataclass
class FocusResult:
    ok: bool
    position: int
    hfd: float
    reason: str
    pulses: int
    samples: list[FocusSample] = field(default_factory=list)
    fit: VCurveFit | None = None
    phases: list[str] = field(default_factory=list)


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


def _move_to(
    driver: FocusDriver, target: int, ratios: StepRatios, budget: _Budget
) -> tuple[bool, bool]:
    """Move to ``target`` (Near1-units from the stop).

    Returns ``(ok, moved)``. ``moved`` is False when the lens refused to travel,
    which is how end-of-range is detected.
    """
    before = driver.position
    plan = plan_move(target - before, ratios, max_pulses=budget.limit - budget.used)
    if plan.is_empty:
        return True, False
    if not plan.within_budget:
        return False, False
    for direction, count in plan.steps:
        result = driver.pulse(direction, count)
        budget.used += result.pulses_sent
        if not result.ok:
            return False, driver.position != before
    return True, driver.position != before


def _home(driver: FocusDriver, cfg: RoutineConfig, budget: _Budget) -> tuple[bool, str]:
    """Hammer the far stop, then declare that position zero.

    The stop is a real physical reference: Canon places it slightly past infinity
    so the lens can still reach infinity as it expands with temperature (11 §1).
    """
    for _ in range(cfg.home_pulses):
        result = driver.pulse(Direction.FAR_3, 1)
        budget.used += result.pulses_sent
        if not result.ok:
            return False, f"focus pulse failed: {result.detail}"
        if not result.moved:
            driver.reset_position(0)
            return True, ""

    # Never stalled. Either the lens is not moving at all, or the travel is longer
    # than our budget -- both are worth surfacing rather than hiding (11 §9).
    driver.reset_position(0)
    return True, "homing budget exhausted without detecting the stop"


def _measure_ok(driver: FocusDriver, cfg: RoutineConfig) -> FocusSample:
    sample = driver.measure()
    return FocusSample(
        position=sample.position,
        hfd=sample.hfd,
        star_count=sample.star_count,
        accepted=sample.accepted and sample.star_count >= cfg.min_stars,
        reason=sample.reason,
    )


def _acquire(
    driver: FocusDriver,
    cfg: RoutineConfig,
    budget: _Budget,
    known_offset: int | None,
    ratios: StepRatios,
) -> tuple[FocusSample | None, str]:
    """Get stars in view: jump to a known offset, else walk back from the stop."""
    if known_offset is not None and known_offset > 0:
        ok, _ = _move_to(driver, known_offset, ratios, budget)
        if ok:
            sample = _measure_ok(driver, cfg)
            if sample.accepted:
                return sample, ""

    travelled = 0
    steps = 0
    stride = cfg.acquire_step_units
    while travelled < cfg.acquire_max_units:
        if budget.exhausted:
            return None, "pulse budget exhausted while acquiring stars"
        ok, moved = _move_to(driver, driver.position + stride, ratios, budget)
        if not ok:
            return None, "could not drive the focus motor"
        if not moved:
            return None, "reached the end of focus travel without finding stars"
        travelled += stride
        steps += 1
        if cfg.acquire_widen_every and steps % cfg.acquire_widen_every == 0:
            stride = min(stride * 2, cfg.acquire_step_units * 4)
        sample = _measure_ok(driver, cfg)
        if sample.accepted:
            return sample, ""

    return None, "no stars found within the acquire budget"


def _sweep(
    driver: FocusDriver,
    cfg: RoutineConfig,
    budget: _Budget,
    first: FocusSample,
    ratios: StepRatios,
) -> list[FocusSample]:
    """Walk past the minimum, collecting usable samples."""
    samples: list[FocusSample] = [first]
    best = first.hfd
    worsening = 0

    for _ in range(cfg.max_sweep_samples - 1):
        if budget.exhausted:
            break
        ok, moved = _move_to(driver, driver.position + cfg.sweep_step_units, ratios, budget)
        if not ok or not moved:
            break
        sample = _measure_ok(driver, cfg)
        samples.append(sample)
        if not sample.accepted:
            continue
        if sample.hfd < best:
            best = sample.hfd
            worsening = 0
        else:
            worsening += 1
            if worsening >= cfg.worsening_run:
                break

    return samples


def _best_accepted(samples: list[FocusSample], fallback: FocusSample) -> FocusSample:
    accepted = [s for s in samples if s.accepted and math.isfinite(s.hfd)]
    return min(accepted, key=lambda s: s.hfd) if accepted else fallback


def autofocus(
    driver: FocusDriver,
    config: RoutineConfig | None = None,
    *,
    start: Start = Start.AT_CURRENT,
    known_offset: int | None = None,
    ratios: StepRatios | None = None,
) -> FocusResult:
    """Run the full routine. Ordinary failures are reported, never raised.

    Defaults to ``Start.AT_CURRENT`` because that is the real workflow: the user
    rough-focuses by hand on the live view, then this refines locally (12 §4.2).
    ``Start.COLD`` exists for the no-human case and is not on the critical path
    (12 §4.4).
    """
    cfg = config or RoutineConfig()
    ratios = ratios or DEFAULT_RATIOS
    budget = _Budget(cfg.max_total_pulses)
    phases: list[str] = []

    sample: FocusSample | None = None

    if start is Start.COLD:
        homed, home_note = _home(driver, cfg, budget)
        phases.append(f"home:{budget.used}")
        if not homed:
            return FocusResult(
                False, driver.position, math.nan, home_note, budget.used, phases=phases
            )

        sample, acquire_note = _acquire(driver, cfg, budget, known_offset, ratios)
        if sample is None:
            return FocusResult(
                False, driver.position, math.nan, acquire_note, budget.used, phases=phases
            )
        phases.append(f"acquire:{budget.used}")
    else:
        # Already anchored and already looking at sky: only verify we have stars.
        current = _measure_ok(driver, cfg)
        if current.accepted:
            sample = current
        else:
            # Stars vanished (cloud, cap, someone bumped the ring). Fall back to a
            # bounded search rather than blindly sweeping around nothing.
            sample, acquire_note = _acquire(driver, cfg, budget, known_offset, ratios)
            if sample is None:
                return FocusResult(
                    False, driver.position, math.nan, acquire_note, budget.used, phases=phases
                )
            phases.append(f"acquire:{budget.used}")

    samples = _sweep(driver, cfg, budget, sample, ratios)
    phases.append(f"sweep:{budget.used}")

    usable = [(s.position, s.hfd) for s in samples if s.accepted and math.isfinite(s.hfd)]
    fit = fit_v_curve(usable) if len(usable) >= 5 else None

    if fit is None or depth_of_minimum(usable, fit) < cfg.min_curve_depth:
        # Flat or untrustworthy curve: keep the best measured position rather than
        # inventing an optimum. On a short lens this is normal and correct (11 §3).
        best = _best_accepted(samples, sample)
        _move_to(driver, best.position, ratios, budget)
        return FocusResult(
            True,
            driver.position,
            best.hfd,
            "no reliable minimum (flat curve); kept the best measured position",
            budget.used,
            samples,
            fit,
            phases,
        )

    landing = round(fit.best_position)

    for _ in range(cfg.refine_iterations):
        ok, _ = _move_to(driver, landing, ratios, budget)
        if not ok:
            break
        check = _measure_ok(driver, cfg)
        samples.append(check)
        if check.accepted and abs(check.hfd - fit.best_hfd) <= fit.best_hfd * cfg.hfd_tolerance:
            break

        # Sweep a narrow window either side of where we landed, then refit.
        # Five probes across +/-2 windows: the fit needs MIN_SAMPLES points to
        # refit at all, so a 3-point window would make this phase a no-op.
        window = max(2, cfg.sweep_step_units // 3)
        local: list[tuple[float, float]] = []
        probe_positions = [landing + offset * window for offset in (-2, -1, 0, 1, 2)]
        for position in probe_positions:
            # No clamping at zero: the position origin is only the far stop when we
            # homed there. After the user turns the focus ring by hand the counter's
            # zero is arbitrary, and clamping would drag every probe toward it.
            _move_to(driver, position, ratios, budget)
            probe = _measure_ok(driver, cfg)
            samples.append(probe)
            if probe.accepted and math.isfinite(probe.hfd):
                local.append((probe.position, probe.hfd))
        refit = fit_v_curve(local) if len(local) >= 5 else None
        if refit is None:
            break
        fit = refit
        landing = round(refit.best_position)

    phases.append(f"done:{budget.used}")
    _move_to(driver, landing, ratios, budget)
    final = _measure_ok(driver, cfg)
    samples.append(final)

    ok = final.accepted
    return FocusResult(
        ok,
        driver.position,
        final.hfd if final.accepted else math.nan,
        "" if ok else "final frame had too few usable stars",
        budget.used,
        samples,
        fit,
        phases,
    )
