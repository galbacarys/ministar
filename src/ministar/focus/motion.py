"""Motion planning for the focus motor.

This is where we beat the reference implementation. `Camera-Lens-AF`'s ``Move``
sends only ``Near1``/``Far1``, one pulse per ~100 ms, so travelling 5030 units
from the focus stop to 400 mm focus takes **~8.4 minutes** (11 §8). Unacceptable
for a check on every reframe.

We instead decompose a move across the three pulse tiers. The ratios between
tiers are undocumented and lens-dependent, so **they must be measured** -- and
until they are, we deliberately plan using ``NEAR_1`` only. Guessing a ratio is
worse than not using the tier: if we assume one ``Near2`` moves 1 unit but it
really moves 4, every move overshoots by 4x.
"""

from __future__ import annotations

from dataclasses import dataclass

from ministar.focus.driver import Direction

#: Tier -> how many Near1-units one pulse of that tier moves.
StepRatios = dict[Direction, float]

#: Safe default: only the smallest tier, used until calibration measures the rest.
DEFAULT_RATIOS: StepRatios = {Direction.NEAR_1: 1.0}


@dataclass(frozen=True)
class MovePlan:
    """A decomposed move, ready to execute."""

    steps: tuple[tuple[Direction, int], ...]
    units: int
    total_pulses: int
    within_budget: bool

    @property
    def is_empty(self) -> bool:
        return not self.steps


def plan_move(
    delta_units: int,
    ratios: StepRatios | None = None,
    *,
    max_pulses: int = 2000,
) -> MovePlan:
    """Plan the fewest pulses that move ``delta_units`` from the stop.

    Positive ``delta_units`` moves toward near focus (position increases),
    negative moves toward the far stop. Greedy largest-tier-first is optimal here
    because the ratios are whole and monotonically increasing.
    """
    ratios = ratios or DEFAULT_RATIOS
    if delta_units == 0:
        return MovePlan(steps=(), units=0, total_pulses=0, within_budget=True)

    toward_near = delta_units > 0
    remaining = abs(delta_units)

    # Largest tier first. Only tiers we have a measured ratio for are eligible.
    tiers = sorted(ratios.items(), key=lambda kv: kv[1], reverse=True)

    steps: list[tuple[Direction, int]] = []
    for tier, ratio in tiers:
        if ratio <= 0:
            continue
        count = int(remaining // ratio)
        if count <= 0:
            continue
        direction = _signed(tier.magnitude, toward_near)
        steps.append((direction, count))
        remaining -= count * ratio
        if remaining <= 0:
            break

    # Only Near1 (ratio 1.0) guarantees we can always finish the remainder.
    if remaining > 0 and ratios.get(Direction.NEAR_1, 1.0) == 1.0:
        steps.append((_signed(1, toward_near), int(remaining)))
        remaining = 0

    total = sum(count for _, count in steps)
    moved = abs(delta_units) - remaining
    return MovePlan(
        steps=tuple(steps),
        units=int(moved),
        total_pulses=total,
        within_budget=total <= max_pulses and remaining == 0,
    )


def _signed(magnitude: int, toward_near: bool) -> Direction:
    magnitude = max(1, min(3, magnitude))
    return (
        {1: Direction.NEAR_1, 2: Direction.NEAR_2, 3: Direction.NEAR_3}[magnitude]
        if toward_near
        else {1: Direction.FAR_1, 2: Direction.FAR_2, 3: Direction.FAR_3}[magnitude]
    )


def flip(direction: Direction) -> Direction:
    return _signed(direction.magnitude, not direction.toward_near)
