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
    #: Live-view frames averaged into one HFD measurement.
    #:
    #: Single-frame HFD is noisy enough to make focusing unreliable rather than slow: measured
    #: on a monitor starfield (a hard test -- aliased, flat, low contrast) the reading at the
    #: *best* position jumped between 3 and 6 px. A curve that noisy has no usable minimum, and
    #: the routine then either refuses to focus or lands by luck. Averaging costs the frame time
    #: -- at ~8.6 fps ten frames is ~1.2 s per measurement, so a 12-sample sweep is ~15 s -- and
    #: buys a reading stable enough to fit. The median is used rather than the mean: same cost,
    #: and one bad frame (a cloud, a gust, a dropped JPEG) cannot drag the sample.
    frames_per_measure: int = 10
    #: How long to wait for those frames before measuring with what arrived.
    measure_timeout_s: float = 6.0

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
        """HFD at the current position, averaged over several frames.

        The averaging is the point: see ``frames_per_measure``. The station may average
        pixels before publishing (``Station.live_average``); when it does, fewer published
        snapshots are collected so the number of raw frames spent per sample stays about
        the same. Fewer frames than asked for is not an error -- a slow camera or a closing
        screen returns what arrived, and the caller is told how many frames it came from.
        """
        snapshot = self.station.snapshot()
        if snapshot.error:
            return self._sample(snapshot.position, math.nan, 0, snapshot.error)

        # Each published snapshot may already be a pixel average of several live-view
        # frames (Station.live_average). Keep the *raw* frame budget roughly constant:
        # when the station averages, fewer snapshots are needed, and the averaging is
        # what removes the detection noise a median of HFDs cannot.
        average = max(1, int(getattr(self.station, "live_average", 1)))
        wanted = max(1, math.ceil(self.frames_per_measure / average))
        readings = self._frames(wanted)
        if not readings:
            # Live view is not delivering. Say so rather than inventing a measurement.
            return self._sample(
                snapshot.position,
                math.nan,
                0,
                f"no live-view frames within {self.measure_timeout_s:g} s",
            )

        usable = [reading for reading in readings if self._usable(reading)]
        if not usable:
            stars = max(stars for _, stars in readings)
            return self._sample(
                snapshot.position,
                math.nan,
                stars,
                f"only {stars} star(s) in {len(readings)} frame(s)",
            )

        hfds = sorted(hfd for hfd, _ in usable)
        star_counts = sorted(stars for _, stars in usable)
        hfd = hfds[len(hfds) // 2]
        stars = star_counts[len(star_counts) // 2]
        # A majority of the frames must have been usable, or this is a star-poor field with an
        # occasional lucky detection rather than a measurement.
        accepted = len(usable) * 2 >= len(readings) and math.isfinite(hfd)
        reason = "" if accepted else f"only {len(usable)} of {len(readings)} frames usable"
        return self._sample(snapshot.position, hfd, stars, reason, accepted=accepted)

    def _usable(self, reading: tuple[float, int]) -> bool:
        hfd, stars = reading
        return stars >= self.min_stars and math.isfinite(hfd) and hfd > 0.0

    @staticmethod
    def _sample(
        position: int, hfd: float, stars: int, reason: str, *, accepted: bool | None = None
    ) -> FocusSample:
        ok = bool(accepted) if accepted is not None else False
        return FocusSample(
            position=int(position),
            hfd=float(hfd),
            star_count=int(stars),
            accepted=ok,
            reason=reason,
        )

    def _frames(self, count: int) -> list[tuple[float, int]]:
        """Collect ``count`` *distinct* new frames, as (hfd, stars).

        Distinct by the station's frame counter, because the pump republished a snapshot many
        times between frames and counting those repeats would average the same image ten times
        and call it ten samples.
        """
        first = int(self.station.snapshot().frames)
        readings: list[tuple[float, int]] = []
        seen = first
        deadline = time.monotonic() + self.measure_timeout_s
        while len(readings) < count and time.monotonic() < deadline:
            snapshot = self.station.snapshot()
            if int(snapshot.frames) > seen:
                seen = int(snapshot.frames)
                readings.append((float(snapshot.hfd), int(snapshot.stars)))
                continue
            time.sleep(self.poll_s)
        return readings

    def _await(self, predicate) -> bool:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if predicate(self.station.snapshot()):
                return True
            time.sleep(self.poll_s)
        return False


__all__ = ["StationDriver", "StationLike"]
