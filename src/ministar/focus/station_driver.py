"""A :class:`FocusDriver` over the UI station's camera thread.

The autofocus routine is synchronous: it pulses the lens, measures, and decides, all in
a loop. The station owns its camera in another thread and publishes immutable
snapshots. This adapter bridges the two, which matters because it lets the screen run
the **real** routine rather than a second implementation that would drift from it.

The one subtlety is what "the move has been applied" means. Queuing a pulse returns
immediately by design (a click must not block), so a pulse is only finished when the
station has emptied its queue *and* produced a frame after the request. Requiring the
newer frame is what stops a fast measurement from reading the pre-move picture.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ministar.focus.driver import Direction, FocusSample, PulseResult


class StationLike(Protocol):
    """The part of :class:`ministar.ui.station.Station` this adapter uses."""

    def snapshot(self) -> Any: ...

    def request(self, direction: Direction, count: int = 1) -> int: ...


@dataclass
class StationDriver:
    """Drive a station like a camera, for the autofocus routine."""

    station: StationLike
    #: Star count below which a frame is not sky. Mirrors ``RoutineConfig.min_stars``;
    #: the routine itself only looks at ``accepted``, so the gate has to live here.
    min_stars: int = 5
    timeout_s: float = 5.0
    poll_s: float = 0.02

    @property
    def position(self) -> int:
        return int(self.station.snapshot().position)

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        count = max(1, int(count))
        before = int(self.station.snapshot().frames)
        accepted = int(self.station.request(direction, count))
        if accepted == 0:
            return PulseResult(ok=False, moved=False, pulses_sent=0, detail="camera queue full")

        def applied(snapshot: Any) -> bool:
            return bool(snapshot.frames > before and snapshot.pending == 0 and not snapshot.error)

        if not self._await(applied):
            return PulseResult(
                ok=False,
                moved=False,
                pulses_sent=0,
                detail=f"camera did not apply {accepted} pulse(s) within {self.timeout_s:g} s",
            )
        return PulseResult(ok=True, moved=True, pulses_sent=accepted, detail="")

    def reset_position(self, position: int = 0) -> None:
        camera = getattr(self.station, "camera", None)
        reset = getattr(camera, "reset_position", None)
        if callable(reset):
            reset(position)

    def measure(self) -> FocusSample:
        snapshot = self.station.snapshot()
        hfd = float(snapshot.hfd)
        stars = int(snapshot.stars)
        accepted = bool(not snapshot.error and stars >= self.min_stars and math.isfinite(hfd))
        reason = snapshot.error or ("" if accepted else f"only {stars} star(s) in frame")
        return FocusSample(
            position=int(snapshot.position),
            hfd=hfd,
            star_count=stars,
            accepted=accepted,
            reason=reason,
        )

    def _await(self, predicate) -> bool:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if predicate(self.station.snapshot()):
                return True
            time.sleep(self.poll_s)
        return False


__all__ = ["StationDriver", "StationLike"]
