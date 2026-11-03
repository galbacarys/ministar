"""The autofocus routine against a synthetic V-curve.

This is 11 §10's validation plan: the whole routine is pure logic behind
``FocusDriver``, so it can be proven correct with no camera attached at all.
"""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

from ministar.focus.calibration import CalibrationStore
from ministar.focus.driver import Direction, FocusSample, PulseResult
from ministar.focus.routine import RoutineConfig, Start, autofocus, move_focus

#: Near1-equivalents moved by one pulse of each tier.
STEP_UNITS = {1: 1, 2: 4, 3: 12}

REAL_RATIOS = {Direction.NEAR_1: 1.0, Direction.NEAR_2: 4.0, Direction.NEAR_3: 12.0}


class FakeFocuser:
    """A lens that behaves like the real thing: asymmetric V, hard far stop."""

    def __init__(
        self,
        best: int = 500,
        hmin: float = 2.0,
        stars: int = 20,
        flat: bool = False,
        stop_floor: int = -60,
        detect_limit: float = 25.0,
        width_far: float = 25.0,
        width_near: float = 60.0,
    ) -> None:
        self._pos = 0
        self.best = best
        self.hmin = hmin
        self.stars = stars
        self.flat = flat
        self.stop_floor = stop_floor
        #: A real star becomes undetectable well before focus: a 200 px donut is
        #: not a star to any detector. Without this the fake reports stars
        #: everywhere and acquire stops on its first frame.
        self.detect_limit = detect_limit
        #: How fast HFD rises away from focus, below and above the minimum. These are what
        #: make one lens's curve narrow and another's wide.
        self.width_far = width_far
        self.width_near = width_near
        self.frames = 0
        self.pulse_count = 0

    @property
    def position(self) -> int:
        return self._pos

    def reset_position(self, position: int = 0) -> None:
        self._pos = position

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        before = self._pos
        delta = STEP_UNITS[direction.magnitude] * count * (1 if direction > 0 else -1)
        self._pos = max(self.stop_floor, self._pos + delta)
        self.pulse_count += count
        return PulseResult(ok=True, moved=self._pos != before, pulses_sent=count)

    def _hfd(self, p: int) -> float:
        if self.flat:
            return self.hmin
        width = self.width_far if p < self.best else self.width_near
        return self.hmin * math.sqrt(1.0 + ((p - self.best) / width) ** 2)

    def measure(self) -> FocusSample:
        self.frames += 1
        hfd = self._hfd(self._pos)
        visible = hfd <= self.detect_limit
        return FocusSample(self._pos, hfd, self.stars if visible else 0, True)


def test_homing_detects_the_stop() -> None:
    driver = FakeFocuser()
    config = RoutineConfig(home_pulses=40)
    autofocus(driver, config, start=Start.COLD, ratios=REAL_RATIOS)
    # Homing must have hit the floor and then re-anchored at zero.
    assert driver.position >= 0


def test_converges_on_a_synthetic_curve() -> None:
    driver = FakeFocuser(best=500)
    result = autofocus(driver, RoutineConfig(), ratios=REAL_RATIOS)
    assert result.ok
    assert result.reason == ""
    assert abs(driver.position - 500) <= 40
    assert result.hfd < 2.4


def test_flat_curve_keeps_a_position_and_says_so() -> None:
    """On a short lens the curve is nearly flat; we must not invent a minimum."""
    driver = FakeFocuser(flat=True)
    result = autofocus(driver, RoutineConfig(), ratios=REAL_RATIOS)
    assert result.ok
    assert "flat curve" in result.reason


def test_no_stars_fails_cleanly() -> None:
    driver = FakeFocuser(stars=0)
    result = autofocus(driver, RoutineConfig(), ratios=REAL_RATIOS)
    assert not result.ok
    assert "no stars" in result.reason.lower()
    assert math.isnan(result.hfd)


def test_pulse_budget_is_respected() -> None:
    driver = FakeFocuser()
    result = autofocus(driver, RoutineConfig(max_total_pulses=300), ratios=REAL_RATIOS)
    assert result.pulses <= 300
    assert driver.pulse_count <= 300


def test_known_offset_is_used_to_skip_the_search() -> None:
    fast = FakeFocuser(best=500)
    slow = FakeFocuser(best=500)
    autofocus(fast, RoutineConfig(), start=Start.COLD, known_offset=500, ratios=REAL_RATIOS)
    autofocus(slow, RoutineConfig(), start=Start.COLD, ratios=REAL_RATIOS)
    assert fast.frames <= slow.frames


def test_calibration_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "lenses.json"
    store = CalibrationStore(path)
    lens = store.get("Canon Lens Driver (EF100-400mm f/4.5-5.6L IS II USM)")
    lens.record(100.0, 0)
    lens.record(400.0, 5030)
    store.save()

    reloaded = CalibrationStore(path).load()
    entry = reloaded.get("Canon Lens Driver (EF100-400mm f/4.5-5.6L IS II USM)")
    assert entry.offset_for(400.0) == 5030
    assert entry.offset_for(100.0) == 0
    # Focal lengths are matched with a tolerance, not exactly.
    assert entry.offset_for(399.0) == 5030
    assert entry.offset_for(250.0) is None


def test_calibration_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "lenses.json"
    path.write_text("{not json", encoding="utf-8")
    store = CalibrationStore(path).load()
    assert len(store) == 0  # a corrupt file must never block a session


def test_calibration_defaults_to_near1_only(tmp_path: Path) -> None:
    """Until ratios are measured we plan with the small tier only."""
    store = CalibrationStore(tmp_path / "l.json")
    ratios = store.get("any").ratios()
    assert ratios == {Direction.NEAR_1: 1.0}


def test_cold_start_reaches_a_far_offset() -> None:
    """The real 400 mm case: focus sits ~5030 units from the stop.

    This is why the routine's acquire budget had to grow and widen -- a 700-unit
    budget could never reach it, and the failure would present as "no stars found"
    on a perfectly good sky.
    """
    driver = FakeFocuser(best=5030, stop_floor=-100)
    result = autofocus(driver, RoutineConfig(), start=Start.COLD, ratios=REAL_RATIOS)
    assert result.ok
    assert abs(driver.position - 5030) <= 60
    assert result.pulses < 1000


def test_at_current_skips_homing_and_search() -> None:
    """The per-reframe check must be cheap: no home, no long walk."""
    driver = FakeFocuser(best=500)
    driver.reset_position(470)
    result = autofocus(driver, RoutineConfig(), start=Start.AT_CURRENT, ratios=REAL_RATIOS)
    assert result.ok
    assert result.pulses < 40
    assert abs(driver.position - 500) <= 40


# -- which way is focus? -----------------------------------------------------
def test_a_sweep_that_starts_past_focus_turns_round_and_finds_it() -> None:
    """The reported bug: focus behind the start, sweep walked away and blamed the lens."""
    driver = FakeFocuser(best=500)
    driver.reset_position(700)
    notes: list[str] = []

    result = autofocus(
        driver,
        RoutineConfig(),
        start=Start.AT_CURRENT,
        ratios=REAL_RATIOS,
        on_progress=notes.append,
    )

    assert result.ok
    assert abs(driver.position - 500) <= 40, f"landed at {driver.position}, focus is at 500"
    assert "flat curve" not in result.reason, "the curve was not flat, we were on the wrong side"
    assert any("turned round" in note for note in notes), f"the turn was silent: {notes}"
    assert sum(1 for phase in result.phases if "turned" in phase) == 1


def test_the_sweep_says_which_way_it_is_going() -> None:
    driver = FakeFocuser(best=500)
    driver.reset_position(300)
    notes: list[str] = []
    autofocus(
        driver,
        RoutineConfig(),
        start=Start.AT_CURRENT,
        ratios=REAL_RATIOS,
        on_progress=notes.append,
    )
    assert "sweeping toward near" in notes


# -- generic across lenses ---------------------------------------------------
def test_a_narrow_window_is_not_stepped_over() -> None:
    """The 400 mm end: an in-focus window ~100 units wide, a stride that may reach 96."""
    driver = FakeFocuser(best=1000, hmin=2.0, width_far=50.0, width_near=50.0, stop_floor=-500)
    driver.reset_position(700)  # stars in view, 300 units out: the refine case

    result = autofocus(driver, RoutineConfig(), start=Start.AT_CURRENT, ratios=REAL_RATIOS)

    assert result.ok
    assert abs(driver.position - 1000) <= 40, f"landed at {driver.position}, focus is at 1000"
    # No step may be wider than the narrowest window we expect to find.
    positions = [sample.position for sample in result.samples]
    widest = max(abs(b - a) for a, b in pairwise(positions))
    assert widest <= RoutineConfig().sweep_step_max_units


def test_a_wide_window_is_crossed_without_hundreds_of_samples() -> None:
    """The short-lens end: the minimum is thousands of units away and the curve is broad.

    A fixed 24-unit stride cannot cross this inside any sane sample budget, which is why the
    stride is taken from the descent rather than being a constant.
    """
    driver = FakeFocuser(best=3000, hmin=2.0, width_far=1500.0, width_near=1500.0, stop_floor=-100)
    driver.reset_position(0)

    result = autofocus(driver, RoutineConfig(), start=Start.AT_CURRENT, ratios=REAL_RATIOS)

    assert result.ok, f"never reached the trough: {result.reason}"
    assert abs(driver.position - 3000) <= 120, f"landed at {driver.position}"
    # Reaching 3000 with the *base* stride would take 125 samples just to arrive, before the
    # rise that proves it is a trough. Fewer than that is evidence the stride adapted.
    assert len(result.samples) < 125, f"took {len(result.samples)} samples to cross a wide curve"
    assert result.pulses < 800, f"spent {result.pulses} pulses crossing it"


def test_the_same_settings_work_at_both_ends() -> None:
    """One configuration, two very different lenses: the point of a trough search."""
    outcomes = []
    for width, best, start_pos in ((50.0, 1000, 700), (1500.0, 3000, 0), (60.0, 500, 700)):
        driver = FakeFocuser(
            best=best, hmin=2.0, width_far=width, width_near=width, stop_floor=-200
        )
        driver.reset_position(start_pos)
        result = autofocus(driver, RoutineConfig(), start=Start.AT_CURRENT, ratios=REAL_RATIOS)
        outcomes.append((result.ok, abs(driver.position - best) <= max(40, best * 0.05)))

    assert all(ok and close for ok, close in outcomes), outcomes


def test_a_defocused_field_does_not_walk_the_whole_range() -> None:
    """The second half of the report: "it just keeps going up".

    With stars undetectable when far out of focus -- the normal case, a defocused star is not a
    star to any detector -- the old acquire walked 8000 units one way (762 pulses) and gave up,
    when focus was 200 units *behind* the start.
    """
    driver = FakeFocuser(best=500, detect_limit=6.0)
    driver.reset_position(700)

    result = autofocus(driver, RoutineConfig(), start=Start.AT_CURRENT, ratios=REAL_RATIOS)

    assert result.ok, f"gave up instead of searching both ways: {result.reason}"
    assert abs(driver.position - 500) <= 40
    assert driver.pulse_count < 150, f"took {driver.pulse_count} pulses to find a 200-unit move"


def test_a_field_with_no_stars_at_all_fails_within_the_local_span() -> None:
    """No sky is still a failure, but a *bounded* one: the counter's zero means nothing."""
    driver = FakeFocuser(stars=0)
    driver.reset_position(4000)  # far from the fake's stop, as an arbitrary counter would be
    result = autofocus(driver, RoutineConfig(), start=Start.AT_CURRENT, ratios=REAL_RATIOS)

    assert not result.ok
    assert "no stars" in result.reason.lower()
    assert driver.pulse_count < 200, f"searched {driver.pulse_count} pulses for a local check"
    assert abs(driver.position - 4000) <= RoutineConfig().acquire_local_units * 2


def test_a_sweep_that_stops_yielding_stars_gives_up_early() -> None:
    """Rejected samples used to be skipped, so the loop ran to max_sweep_samples regardless."""
    driver = FakeFocuser(best=500, detect_limit=7.0)
    driver.reset_position(480)  # in focus: one accepted sample, then the field dies off
    config = RoutineConfig(sweep_step_units=60)  # step out of the detectable window fast
    result = autofocus(driver, config, start=Start.AT_CURRENT, ratios=REAL_RATIOS)

    assert driver.pulse_count < 120, f"kept sweeping a dead field for {driver.pulse_count} pulses"
    assert result.ok or "no stars" in result.reason.lower()


# -- putting the lens back where the hand left it ----------------------------
def test_move_focus_goes_to_an_absolute_position() -> None:
    """What "use this manual focus" needs: restore the operator's own position."""
    driver = FakeFocuser(best=500)
    driver.reset_position(900)

    assert move_focus(driver, 640, ratios=REAL_RATIOS)
    assert driver.position == 640


def test_move_focus_refuses_to_plan_beyond_its_budget() -> None:
    driver = FakeFocuser(best=5000, stop_floor=-100)
    driver.reset_position(0)

    assert not move_focus(driver, 5000, ratios=REAL_RATIOS, max_pulses=2)
    assert driver.position == 0, "a move it cannot afford must not be half-done"
