"""The boundary between the autofocus logic and the camera.

Deliberately tiny. The whole autofocus routine is pure logic driven through this
protocol, so it can be tested against a synthetic V-curve with no camera at all
(11 §10). Anything to do with EDSDK stays behind this line.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, runtime_checkable


class Direction(IntEnum):
    """Pulse magnitude and sign, mirroring ``EdsEvfDriveLens``.

    ``NEAR`` moves focus toward the camera (away from the far stop, so the
    position counter increases); ``FAR`` moves toward the stop.
    """

    FAR_3 = -3
    FAR_2 = -2
    FAR_1 = -1
    NEAR_1 = 1
    NEAR_2 = 2
    NEAR_3 = 3

    @property
    def toward_near(self) -> bool:
        return self > 0

    @property
    def magnitude(self) -> int:
        return abs(int(self))


#: Canon's three pulse tiers, smallest first.
TIERS: tuple[Direction, ...] = (Direction.NEAR_1, Direction.NEAR_2, Direction.NEAR_3)


@dataclass(frozen=True)
class FocusSample:
    """One measurement of the focus state.

    ``hfd`` is only meaningful when ``accepted`` is true: a frame rejected by the
    star-count or saturation gate has an unreliable HFD (11 §4).
    """

    position: int
    hfd: float
    star_count: int
    accepted: bool
    reason: str = ""


@dataclass(frozen=True)
class PulseResult:
    """Outcome of one or more focus pulses."""

    ok: bool
    moved: bool
    pulses_sent: int
    detail: str = ""


@runtime_checkable
class FocusDriver(Protocol):
    """What the routine needs from a camera with a focusable lens."""

    @property
    def position(self) -> int:
        """Estimate of the focus position, in Near1-equivalents from the stop."""
        ...

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        """Send ``count`` pulses of ``direction`` and update ``position``."""
        ...

    def reset_position(self, position: int = 0) -> None:
        """Re-anchor the position counter, e.g. after homing to the far stop."""
        ...

    def measure(self) -> FocusSample:
        """Capture (or grab a live-view frame) and measure star sharpness."""
        ...
