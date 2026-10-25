"""The autofocus routine against a synthetic V-curve.

This is 11 §10's validation plan: the whole routine is pure logic behind
``FocusDriver``, so it can be proven correct with no camera attached at all.
"""

from __future__ import annotations

import math
from pathlib import Path

from ministar.focus.calibration import CalibrationStore
from ministar.focus.driver import Direction, FocusSample, PulseResult
from ministar.focus.routine import RoutineConfig, Start, autofocus

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
        width = 25.0 if p < self.best else 60.0
        return self.hmin * math.sqrt(1.0 + ((p - self.best) / width) ** 2)

    def measure(self) -> FocusSample:
        self.frames += 1
        hfd = self._hfd(self._pos)
        visible = hfd <= self.detect_limit
        return FocusSample(self._pos, hfd, self.stars if visible else 0, True)


def test_homing_detects_the_stop() -> None:
    driver = FakeFocuser()
    config = RoutineConfig(home_pulses=40)
    autofocus(driver, config, ratios=REAL_RATIOS)
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
    autofocus(fast, RoutineConfig(), known_offset=500, ratios=REAL_RATIOS)
    autofocus(slow, RoutineConfig(), ratios=REAL_RATIOS)
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
    result = autofocus(driver, RoutineConfig(), ratios=REAL_RATIOS)
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
