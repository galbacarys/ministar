"""The FOCUS step: live view, HFD, and the lens drive.

Rendered both standalone (for bench work) and inside the session wizard, so the
widgets live in :func:`render_focus` and both callers share them. The camera
itself belongs to :class:`ministar.ui.station.Station`, not to this module.

Run it standalone::

    python -m ministar.ui.focus            # real camera
    python -m ministar.ui.focus --fake     # synthetic stars, no hardware
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from nicegui import ui

from ministar.focus.driver import Direction
from ministar.ui.station import FakeCamera, Snapshot, Station
from ministar.ui.theme import install

#: Ordered far-to-near so the row reads like the lens barrel.
DRIVE_ROW: tuple[tuple[str, Direction], ...] = (
    ("FAR 3", Direction.FAR_3),
    ("FAR 2", Direction.FAR_2),
    ("FAR 1", Direction.FAR_1),
    ("NEAR 1", Direction.NEAR_1),
    ("NEAR 2", Direction.NEAR_2),
    ("NEAR 3", Direction.NEAR_3),
)


def render_focus(
    station: Station,
    *,
    on_accept: Callable[[Snapshot], None] | None = None,
) -> None:
    """Draw the focus controls into the current container.

    ``on_accept`` is offered as a button when given; the wizard uses it to record
    the achieved focus position and HFD into the session.
    """
    name_label = ui.label("").classes("mn-title")
    preview = ui.image("").classes("mn-preview")
    metrics = ui.label("").classes("mn-metrics")
    warn = ui.label("").classes("mn-warn")

    count = ui.toggle({1: "1", 3: "3", 10: "10"}, value=1).classes("mn-title")
    with ui.row().classes("items-center justify-center"):
        for label, direction in DRIVE_ROW:
            ui.button(label, on_click=lambda d=direction: station.request(d, count.value)).classes(
                "mn-drive"
            ).style("margin: 3px")

    if on_accept is not None:
        ui.button(
            "use this focus",
            on_click=lambda: on_accept(station.snapshot()),
        ).classes("mn-action").style("margin-top: 8px")

    ui.label("lens must be on MF (focus-by-wire)").classes("mn-dim")

    def tick() -> None:
        snap = station.snapshot()
        name_label.text = f"{snap.name}   frame {snap.frames}"
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
    host: str = "127.0.0.1",
    port: int = 8080,
    native: bool = False,
    show: bool = True,
) -> None:
    global _STATION

    if fake:
        camera = FakeCamera()
        initial_error = ""
    else:
        from ministar.camera import EdsdkCamera

        camera = EdsdkCamera()
        initial_error = ""
        try:
            camera.connect()
        except Exception as exc:
            initial_error = f"{type(exc).__name__}: {exc}"

    _STATION = Station(camera, initial_error=initial_error)
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
    parser.add_argument("--host", default="127.0.0.1")
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
