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
from collections.abc import Callable
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
    #: Fraction of the predicted run-out that the next stride may cover.
    #:
    #: The stride is chosen from the *descent itself*: a linear model through the last two
    #: samples predicts how far HFD would still fall, and the next step takes a third of that.
    #: That is what makes one setting work on every lens -- a wide curve (short lens) predicts a
    #: long run-out and gets long steps, while the last few units before the bottom predict a
    #: short one and get short steps. A fixed doubling cannot do both: measured, doubling walked
    #: past a 60-unit trough in one 96-unit step and landed 51 units off.
    sweep_approach_fraction: float = 1.0 / 3.0
    #: HFD an in-focus star plausibly has, in *pixels*, on any lens.
    #:
    #: This is the one quantity that does not need calibrating per rig: HFD is measured in
    #: pixels, and the two things that set it trade off -- a longer lens has a finer plate
    #: scale (more pixels per arcsecond) and a smaller diffracted/seeing disc in arcseconds
    #: relative to it. Measured across the hardware briefs, an in-focus star lands at roughly
    #: 1.5-4 px. So "still well above this" means we are on a shoulder and may step boldly,
    #: while "at or below it" means we are near the bottom and must creep -- which is what
    #: stops a wide stride from jumping a shallow trough (measured: a 96-unit step over a
    #: 36-unit approach, landing 69 units past focus).
    sweep_floor_hfd: float = 2.5
    #: Widest the sweep stride may grow to while the curve is still falling.
    #:
    #: The stride doubles while HFD keeps improving, so a wide curve (a short lens) is crossed
    #: in a bounded number of samples instead of hundreds. It is capped *below the narrowest
    #: in-focus window we expect* -- brief 11 §5 puts that at ~100 units on the 400 mm end --
    #: because a stride wider than the window can step over the trough and never see it.
    sweep_step_max_units: int = 96
    #: Extra samples allowed while the curve is still falling at the cap.
    #:
    #: Generous because the alternative is worse: "ran out of samples mid-descent" means the
    #: minimum is still ahead, so the honest answer is to keep looking (measured: a 24 mm-class
    #: curve 3000 units from the start needs ~70 samples because the last 500 units are inside
    #: a few percent of the floor and must be crept). The budget is still hard-bounded, and a
    #: field that stops yielding usable samples ends the walk long before this.
    #:
    #: "Ran out of samples mid-descent" is not a trough, it is an unfinished search: the
    #: minimum is still ahead. Extending is what makes the sweep work on a lens whose curve is
    #: wider than a fixed sample count allows for.
    sweep_extend_samples: int = 96
    max_sweep_samples: int = 24
    #: Consecutive worsening samples past the best before we stop sweeping.
    worsening_run: int = 2
    #: Consecutive *unusable* samples (no stars, unmeasurable HFD) that end a sweep.
    #:
    #: Without this a star-poor field walks the whole sweep budget without ever producing a
    #: "worse" sample to stop on: every rejected measurement was skipped, so the loop only
    #: ended at max_sweep_samples. Measured on the real lens: 576 units of travel in one
    #: direction with nothing to show for it.
    max_rejected_run: int = 3
    #: How far either way an ``AT_CURRENT`` search looks for stars before giving up.
    #:
    #: ``acquire_max_units`` is sized for a COLD start (the focus can be 5000+ units from the
    #: stop at 400 mm). AT_CURRENT has no homing, so its counter origin is arbitrary and a
    #: long walk proves nothing -- it just grinds. Measured before this bound existed: 8000
    #: units in one direction, 762 pulses, ending in "no stars found".
    acquire_local_units: int = 200
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


def move_focus(
    driver: FocusDriver,
    position: int,
    *,
    ratios: StepRatios | None = None,
    max_pulses: int | None = None,
) -> bool:
    """Move the lens to an absolute position in the routine's units.

    Public because the screen needs it: after an automatic attempt, "use the manual focus"
    means putting the lens back where the operator had it by hand. Reaching into ``_move_to``
    from a UI would let the unit conventions drift apart again, which is the thing the module
    note at the top says must not happen.
    """
    cfg = RoutineConfig()
    budget = _Budget(max_pulses if max_pulses is not None else cfg.max_total_pulses)
    ok, _ = _move_to(driver, int(position), ratios or DEFAULT_RATIOS, budget)
    return ok


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
    *,
    span: int | None = None,
    both_ways: bool = False,
) -> tuple[FocusSample | None, str]:
    """Get stars in view: jump to a known offset, else walk until stars appear.

    ``both_ways`` is for ``AT_CURRENT``, where no homing has happened: the counter's zero means
    nothing, so a search that only walks one way can spend thousands of units going the wrong
    one. Measured before this existed: 8000 units and 762 pulses, then "no stars found", on a
    field whose focus was 200 units *behind* the start.
    """
    if known_offset is not None and known_offset > 0:
        ok, _ = _move_to(driver, known_offset, ratios, budget)
        if ok:
            sample = _measure_ok(driver, cfg)
            if sample.accepted:
                return sample, ""

    reach = cfg.acquire_max_units if span is None else span
    origin = driver.position
    failure = "no stars found within the acquire budget"

    for direction in (1, -1) if both_ways else (1,):
        _move_to(driver, origin, ratios, budget)
        travelled = 0
        steps = 0
        stride = cfg.acquire_step_units
        while travelled < reach:
            if budget.exhausted:
                return None, "pulse budget exhausted while acquiring stars"
            ok, moved = _move_to(driver, driver.position + direction * stride, ratios, budget)
            if not ok:
                return None, "could not drive the focus motor"
            if not moved:
                # End of focus travel on this side.
                failure = "no stars found before the end of focus travel"
                break
            travelled += stride
            steps += 1
            if cfg.acquire_widen_every and steps % cfg.acquire_widen_every == 0:
                stride = min(stride * 2, cfg.acquire_step_units * 4)
            sample = _measure_ok(driver, cfg)
            if sample.accepted:
                return sample, ""
        else:
            failure = (
                f"no stars within {reach} units of the focus position in "
                f"{'either direction' if both_ways else 'the inward direction'}"
            )

    return None, failure


def _sweep(
    driver: FocusDriver,
    cfg: RoutineConfig,
    budget: _Budget,
    first: FocusSample,
    ratios: StepRatios,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[list[FocusSample], str]:
    """Find the trough: walk until HFD has fallen and then risen again.

    Brief 11 §5's sweep assumes it starts on the far side of focus -- true after a COLD start
    (home to the stop, walk in), false for ``AT_CURRENT``, which is what the wizard runs. The
    counter's zero is arbitrary there, and whoever just turned the focus ring by hand is as
    likely to have overshot as to be short.

    Two ways to get the direction, and only one of them survives noise:

    * **Bet on a gradient** -- measure one step each way, walk toward the sharper. Works on a
      synthetic curve and fails on a real one: one frame of HFD is noisy (seeing, JPEG), and
      the difference across a single 24-unit step is smaller than that noise at both ends of
      the lens range -- an in-focus window is ~100-350 units at 400 mm and thousands at 14 mm,
      so "one step" means something different on every lens.
    * **Walk until the trough shows itself** -- a *confirmed* trough is a run of improving
      samples followed by a rise. That is a shape, not a threshold, so it means the same thing
      on every lens. If the very first samples only worsen, the arm is wrong and we turn round;
      if we hit the sample cap while still falling, the minimum is still ahead and the search
      extends rather than reporting a curve it never crossed.

    Returns the samples and a note describing what happened, which the caller reports: the
    operator should be told when the sweep changes direction rather than left watching the
    position counter run away.
    """
    forward, improved, turned_up = _walk(driver, cfg, budget, first, 1, ratios, on_progress)
    samples = [first, *forward]

    if improved and turned_up:
        # Fell, then rose: the trough is in this arm.
        return samples, ""
    if improved and not turned_up:
        # Still falling when the extended allowance ran out. The minimum is further on.
        if on_progress is not None:
            on_progress("still falling: the trough is further along")
        return samples, "still falling at the end of the sweep"

    # This arm only got worse, so focus is the other way. Re-anchor at the start before
    # searching outward: every sample on the way back is worse than the start (that is why we
    # are turning round), so judging improvements while retracing would spend the whole
    # worsening allowance before reaching the point we already know is better.
    note = f"turned round at {first.position}"
    if on_progress is not None:
        on_progress(note)
    _move_to(driver, first.position, ratios, budget)
    back, back_improved, back_turned = _walk(driver, cfg, budget, first, -1, ratios, on_progress)
    samples.extend(back)
    if back_improved and back_turned:
        return samples, note
    if back_improved:
        return samples, "still falling at the end of the sweep"
    return samples, note if not back else "no trough in either direction"


def _walk(
    driver: FocusDriver,
    cfg: RoutineConfig,
    budget: _Budget,
    start: FocusSample,
    direction: int,
    ratios: StepRatios,
    on_progress: Callable[[str], None] | None,
) -> tuple[list[FocusSample], bool, bool]:
    """Walk one way from ``start``. Returns (samples, improved, turned_up).

    ``turned_up`` is the trough confirmation: HFD rose for ``worsening_run`` accepted samples
    after its best, so the minimum lies inside the walked span.
    """
    if on_progress is not None:
        # Positions count Near1-equivalents from the stop, so + is toward near.
        on_progress("sweeping toward near" if direction > 0 else "sweeping toward far")

    stride = cfg.sweep_step_units
    usable_start = start.accepted and math.isfinite(start.hfd)
    best = float(start.hfd) if usable_start else math.inf
    improved = False
    turned_up = False
    worsening = 0
    rejected = 0
    samples: list[FocusSample] = []

    while not budget.exhausted:
        # While the curve is still falling the search is unfinished, so it is allowed more
        # samples; once it has turned up the ordinary cap applies.
        extending = improved and not turned_up
        allowance = cfg.max_sweep_samples + (cfg.sweep_extend_samples if extending else 0)
        if len(samples) >= allowance:
            break
        ok, moved = _move_to(driver, driver.position + direction * stride, ratios, budget)
        if not ok or not moved:
            break
        sample = _measure_ok(driver, cfg)
        samples.append(sample)

        if not (sample.accepted and math.isfinite(sample.hfd)):
            rejected += 1
            if rejected >= cfg.max_rejected_run:
                if on_progress is not None:
                    on_progress(f"no usable stars for {rejected} steps")
                break
            continue
        rejected = 0

        if sample.hfd < best:
            fall = best - float(sample.hfd)
            best = float(sample.hfd)
            improved = True
            worsening = 0
            # How far is left to fall, if the descent continues linearly? Take a fraction of
            # that, bounded by the narrowest in-focus window we expect. A shoulder predicts a
            # long run-out (long steps, so a wide curve is crossed quickly); the last few units
            # before the bottom predict a short one (short steps, so the trough is not jumped).
            near_floor = sample.hfd <= cfg.sweep_floor_hfd * 1.05
            if near_floor:
                # At the sharpness an in-focus star can have: the trough is close, so take the
                # smallest step we can justify. The linear extrapolation is useless here -- a
                # shallow descent predicts a long run-out and would stride straight past.
                stride = cfg.sweep_step_units
            elif fall > 0.0 and stride > 0:
                remaining = sample.hfd / (fall / stride)
                approach = remaining * cfg.sweep_approach_fraction
                stride = max(
                    cfg.sweep_step_units,
                    min(cfg.sweep_step_max_units, int(approach)),
                )
        else:
            worsening += 1
            stride = cfg.sweep_step_units
            if worsening >= cfg.worsening_run:
                turned_up = True
                break

    return samples, improved, turned_up


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
    on_progress: Callable[[str], None] | None = None,
) -> FocusResult:
    """Run the full routine. Ordinary failures are reported, never raised.

    Defaults to ``Start.AT_CURRENT`` because that is the real workflow: the user
    rough-focuses by hand on the live view, then this refines locally (12 §4.2).
    ``Start.COLD`` exists for the no-human case and is not on the critical path
    (12 §4.4).

    ``on_progress`` receives short notes as the routine changes its mind -- "turned round at
    476" is the one that matters, because the alternative is an operator watching the position
    counter climb away from focus with nothing saying why.
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
            # Stars vanished (cloud, cap, someone bumped the ring). Look a *bounded* way
            # either side rather than walking the whole range: with no homing the counter's
            # zero is arbitrary, so distance travelled is not evidence of anything.
            sample, acquire_note = _acquire(
                driver,
                cfg,
                budget,
                known_offset,
                ratios,
                span=cfg.acquire_local_units,
                both_ways=True,
            )
            if sample is None:
                return FocusResult(
                    False, driver.position, math.nan, acquire_note, budget.used, phases=phases
                )
            phases.append(f"acquire:{budget.used}")

    samples, sweep_note = _sweep(driver, cfg, budget, sample, ratios, on_progress)
    phases.append(f"sweep:{budget.used}")
    if sweep_note.startswith("turned round"):
        phases.append(sweep_note.replace(" ", "-"))

    usable = [(s.position, s.hfd) for s in samples if s.accepted and math.isfinite(s.hfd)]
    fit = fit_v_curve(usable) if len(usable) >= 5 else None

    if fit is None or depth_of_minimum(usable, fit) < cfg.min_curve_depth:
        # Flat or untrustworthy curve: keep the best measured position rather than
        # inventing an optimum. On a short lens this is normal and correct (11 §3).
        best = _best_accepted(samples, sample)
        _move_to(driver, best.position, ratios, budget)
        reason = "no reliable minimum (flat curve); kept the best measured position"
        if sweep_note:
            reason = f"{reason} ({sweep_note})"
        return FocusResult(
            True,
            driver.position,
            best.hfd,
            reason,
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
