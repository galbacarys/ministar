"""The FOCUS step, in two stages.

**1. Hand focus.** The lens is driven by the arrow buttons while live view runs, and the
only job is to get recognisably round stars. No scoring, no curve, no "good enough"
badge: the judgement is the user's, because at this point the software has no idea
whether that is a star or a hot pixel. The stage ends with *looks good, start focus*.

**2. Automatic focus.** From wherever the hand left it, the real routine takes over
(:func:`ministar.focus.routine.autofocus`, ``Start.AT_CURRENT``): sweep, fit the V,
refine, land. Progress is reported as it goes, because a sweep is tens of pulses and a
screen that looks frozen is indistinguishable from a hung camera.

Rendered both standalone (bench work) and inside the session wizard, so the widgets
live here and both callers share them. The camera belongs to
:class:`ministar.ui.station.Station`; the adapter in
:mod:`ministar.focus.station_driver` makes that thread-paced camera look like the
synchronous driver the routine expects.

Run it standalone::

    python -m ministar.ui.focus            # real camera
    python -m ministar.ui.focus --fake     # synthetic stars, no hardware
"""

from __future__ import annotations

import argparse
import threading
from collections.abc import Callable
from enum import Enum

from nicegui import ui

from ministar.focus.driver import Direction
from ministar.focus.routine import FocusResult, Start, autofocus
from ministar.focus.station_driver import StationDriver
from ministar.ui.station import FakeCamera, Snapshot, Station
from ministar.ui.theme import install

#: The hand-off from the eye to the routine. Wording matters: it says the *user* decides,
#: which is the whole point of the manual stage.
MANUAL_ACCEPT_LABEL = "looks good, start focus"

#: Ordered far-to-near so the row reads like the lens barrel.
DRIVE_ROW: tuple[tuple[str, Direction], ...] = (
    ("FAR 3", Direction.FAR_3),
    ("FAR 2", Direction.FAR_2),
    ("FAR 1", Direction.FAR_1),
    ("NEAR 1", Direction.NEAR_1),
    ("NEAR 2", Direction.NEAR_2),
    ("NEAR 3", Direction.NEAR_3),
)


class Stage(Enum):
    """Which half of the step the user is in."""

    MANUAL = "manual"
    AUTO = "auto"
    DONE = "done"


def describe_result(result: FocusResult) -> str:
    """One honest line about how the automatic stage went."""
    if not result.ok:
        return f"autofocus failed: {result.reason} (position {result.position:+d})"
    return (
        f"focused at position {result.position:+d}   HFD {result.hfd:.2f} px"
        f"   ({len(result.samples)} samples, {result.pulses} pulses)"
    )


def render_focus(
    station: Station,
    *,
    on_accept: Callable[[Snapshot], None] | None = None,
    run_autofocus: Callable[[StationDriver], FocusResult] | None = None,
) -> None:
    """Draw the two-stage focus step into the current container.

    ``on_accept`` is offered once a focus has been achieved; the wizard uses it to record
    the position and HFD into the session.
    """
    adapter_factory: Callable[[Station], StationDriver] = StationDriver
    runner = run_autofocus or (lambda driver: autofocus(driver, start=Start.AT_CURRENT))

    name_label = ui.label("").classes("mn-title")
    preview = ui.image("").classes("mn-preview")
    metrics = ui.label("").classes("mn-metrics")
    warn = ui.label("").classes("mn-warn")

    manual = ui.column().classes("w-full items-center")
    with manual:
        ui.label("1. hand focus").classes("mn-heading")
        ui.label("drive until the stars look like points, not discs").classes("mn-dim")
        count = ui.toggle({1: "1", 3: "3", 10: "10"}, value=1).classes("mn-title")
        with ui.row().classes("items-center justify-center"):
            for label, direction in DRIVE_ROW:
                ui.button(
                    label, on_click=lambda d=direction: station.request(d, count.value)
                ).classes("mn-drive").style("margin: 3px")
        ui.label("lens must be on MF (focus-by-wire)").classes("mn-dim")

    auto = ui.column().classes("w-full items-center")
    with auto:
        ui.label("2. automatic focus").classes("mn-heading")
        progress = ui.label("starting...").classes("mn-body")
    auto.set_visibility(False)

    done = ui.column().classes("w-full items-center")
    with done:
        ui.label("focus").classes("mn-heading")
        outcome = ui.label("").classes("mn-metrics")
        if on_accept is not None:
            ui.button("use this focus", on_click=lambda: on_accept(station.snapshot())).classes(
                "mn-action"
            )
    done.set_visibility(False)

    # Shared between the worker thread and the timer: the routine is synchronous, so its
    # only route to the screen is a mutable result slot.
    state: dict[str, object] = {}
    timer: ui.timer | None = None

    def start_auto() -> None:
        manual.set_visibility(False)
        auto.set_visibility(True)
        progress.text = "sweeping..."
        driver = adapter_factory(station)

        def worker() -> None:
            try:
                state["result"] = runner(driver)
            except Exception as exc:  # a broken run must not take the screen down
                state["error"] = f"{type(exc).__name__}: {exc}"

        threading.Thread(target=worker, name="autofocus", daemon=True).start()
        if timer is not None:
            timer.activate()

    start = ui.button(MANUAL_ACCEPT_LABEL, on_click=start_auto).classes("mn-action")
    start.style("margin-top: 8px")

    def poll() -> None:
        nonlocal timer
        result = state.get("result")
        error = state.get("error")
        snap = station.snapshot()
        if result is None and error is None:
            progress.text = (
                f"sweeping...  position {snap.position:+d}   HFD {snap.hfd:.2f} px"
                if snap.stars
                else f"sweeping...  position {snap.position:+d}   (no stars yet)"
            )
            return

        if timer is not None:
            timer.active = False
        auto.set_visibility(False)
        done.set_visibility(True)
        start.set_visibility(False)
        if error is not None:
            outcome.text = f"autofocus failed: {error}"
            return
        assert isinstance(result, FocusResult)
        outcome.text = describe_result(result)

    def tick() -> None:
        snap = station.snapshot()
        # Just the camera: a frame counter that ticks ten times a second is noise, not
        # information. The count is still carried on the snapshot -- the focus driver
        # uses it to prove a frame arrived after a pulse.
        name_label.text = snap.name
        if snap.data_uri:
            preview.source = snap.data_uri
            preview.update()

        hfd_text = (
            f"{snap.hfd:6.2f} px   ({snap.stars} stars)" if snap.stars else "   --       (no stars)"
        )
        moved = ""
        if snap.changed >= 0.0:
            moved = "   moved: " + ("YES" if snap.changed >= 2.0 else "no")

        metrics.text = (
            f"HFD      {hfd_text}\n"
            f"contrast {snap.contrast:10.2f}   (Tenengrad; NOT a focus metric)\n"
            f"position {snap.position:+5d}   pending {snap.pending:3d}{moved}"
        )
        warn.text = snap.error

    ui.timer(0.10, tick)
    timer = ui.timer(0.30, poll, active=False)


_STATION: Station | None = None


@ui.page("/focus")
def focus_page() -> None:
    install()
    station = _STATION
    with ui.column().classes("w-full items-center").style("padding: 10px"):
        ui.label("FOCUS").classes("mn-heading")
        if station is None:  # pragma: no cover - only if imported without run()
            ui.label("station is not running").classes("mn-warn")
            return
        render_focus(station)


def run(
    *,
    fake: bool = False,
    host: str = "0.0.0.0",
    port: int = 8080,
    native: bool = False,
    show: bool = True,
) -> None:
    global _STATION

    if fake:
        camera = FakeCamera()
        connect = None
        initial_error = ""
    else:
        from ministar.camera import EdsdkCamera

        camera = EdsdkCamera()
        # The pump thread opens the session, not this one: EDSDK calls belong to the thread
        # that opened it (see Station). Errors arrive on the snapshot, where the screen shows
        # them, rather than as a startup dialog that would have to block to get them.
        connect = camera.connect
        initial_error = ""

    _STATION = Station(camera, connect=connect, initial_error=initial_error)
    _STATION.start()
    try:
        ui.run(
            host=host,
            port=port,
            title="ministar focus",
            dark=True,
            reload=False,
            show=show,
            native=native,
        )
    finally:
        _STATION.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ministar focus screen")
    parser.add_argument("--fake", action="store_true", help="synthetic stars, no camera")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--native", action="store_true", help="pywebview window")
    parser.add_argument("--no-show", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)
    run(
        fake=args.fake,
        host=args.host,
        port=args.port,
        native=args.native,
        show=not args.no_show,
    )
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    raise SystemExit(main())
