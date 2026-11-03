"""The camera status line, and the button that gets it back.

Global on purpose. The failures this exists for happen on whatever screen the operator happens
to be on -- the body falls asleep, the cable is knocked, a session wedges -- and hunting for the
one page that can fix it is not a thing to do in the dark with a hand on the mount. So the footer
sits under every phase, says what the camera is doing, and offers the only repair there is:
close the session and open a new one.

Two rules it keeps:

* **It tells the truth about the body, not about the wish.** "live view" means frames are
  arriving; a preview that was asked for but is not delivering shows as an error, not as a state.
* **The button is always there**, not only after a failure. A reconnect after moving the tripod
  and knocking the cable is a normal action, and a control that appears only in trouble is a
  control nobody can find in trouble.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nicegui import ui
from nicegui.run import io_bound

if TYPE_CHECKING:  # pragma: no cover - typing only, and importing app here would be a cycle
    from ministar.ui.app import App

#: How often the status line is refreshed. One second is enough to notice a body that has gone
#: away, and slow enough that it cannot become the reason a screen feels busy.
REFRESH_S = 1.0

#: Window over which the preview rate is measured. Long enough to be a rate rather than a
#: difference of two integers, short enough to react when the stream stalls.
RATE_WINDOW_S = 3.0


@dataclass(frozen=True)
class CameraStatus:
    """One line about the camera, and how to colour it."""

    text: str
    detail: str
    tone: str


def describe(
    *,
    name: str = "",
    connected: bool = True,
    error: str = "",
    live_on: bool = False,
    suspended: bool = False,
    fps: float = -1.0,
) -> CameraStatus:
    """What the footer says, for a given camera state. Pure, so it can be pinned by tests."""
    who = f"camera  {name}" if name else "camera"
    if error:
        # The reason is the useful part and it is often long ("EdsdkUnavailable: no camera
        # visible ..."), so it goes on its own line rather than being truncated.
        return CameraStatus(text=who, detail=error, tone="mn-warn")
    if not connected:
        return CameraStatus(text=f"{who}  --  no session", detail="press reconnect", tone="mn-warn")
    if live_on:
        rate = f"  {fps:.1f} fps" if fps > 0 else ""
        return CameraStatus(text=who, detail=f"live view{rate}", tone="mn-body")
    if suspended:
        return CameraStatus(text=who, detail="imaging (live view off)", tone="mn-body")
    return CameraStatus(text=who, detail="connected, live view off", tone="mn-dim")


class _Rate:
    """Frames per second over a window, from a monotonically increasing frame counter."""

    def __init__(self, window_s: float = RATE_WINDOW_S) -> None:
        self.window_s = float(window_s)
        self._samples: list[tuple[float, int]] = []

    def update(self, now: float, frames: int) -> float:
        """Feed one observation; returns the rate, or -1 while there is not enough to say."""
        self._samples.append((now, frames))
        horizon = now - self.window_s
        while len(self._samples) > 2 and self._samples[0][0] < horizon:
            self._samples.pop(0)
        first_t, first_frames = self._samples[0]
        elapsed = now - first_t
        if elapsed < 0.5:
            return -1.0
        return (frames - first_frames) / elapsed


def footer(app: App, *, on_reconnected=None) -> None:
    """Draw the reconnect button over the status line, beneath the wizard.

    ``on_reconnected`` is called after a successful reconnect so the shell can re-ask for live
    view according to the phase it is on (``app.apply_live_view``); passing it in rather than
    importing keeps this module free of a circular import.
    """
    station = app.station
    rate = _Rate()

    with ui.column().classes("items-center").style("gap: 2px; margin-top: 14px"):
        button = ui.button("reconnect camera").classes("mn-action")
        status = ui.label("").classes("mn-dim")
        detail = ui.label("").classes("mn-dim").style("max-width: 900px; text-align: center")

    def update() -> None:
        if station is None:
            status.text = "camera  none in this session"
            detail.text = ""
            button.set_enabled(False)
            return
        snap = station.snapshot()
        live = station.live_view_on
        fps = rate.update(time.monotonic(), snap.frames) if live else -1.0
        said = describe(
            name=snap.name,
            connected=station.connected,
            error=snap.error,
            live_on=live,
            suspended=station.suspended,
            fps=fps,
        )
        status.text = said.text
        status.classes(replace=f"mn-body {said.tone}")
        detail.text = said.detail
        detail.classes(replace=f"mn-warn {said.tone}" if said.tone == "mn-warn" else said.tone)

    async def reconnect() -> None:
        if station is None:
            return
        button.disable()
        detail.text = "reconnecting..."
        try:
            opened = await io_bound(station.reconnect)
        except Exception as exc:
            # A wedged SDK call has to arrive here as a sentence, not as a spinner that never
            # stops: the station's own timeout is what makes that possible.
            detail.text = f"reconnect failed: {type(exc).__name__}: {exc}"
            button.enable()
            return
        update()
        if not opened:
            detail.text = "reconnect failed: no camera answered"
        if on_reconnected is not None:
            on_reconnected()
        button.enable()

    button.on_click(reconnect)
    update()
    ui.timer(REFRESH_S, update)


__all__ = ["REFRESH_S", "CameraStatus", "describe", "footer"]
