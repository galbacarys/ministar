"""The session wizard: a shell around one screen per phase.

Deliberately thin. It draws the step rail, the current screen, and back/next, and
asks :class:`ministar.session.SessionState` whether moving on is allowed -- it never
re-derives those rules. That matters because the *ordering is a dependency chain*
(brief 12 §0): FOCUS must precede POINT because a defocused frame will not solve.
Duplicating that judgement in the UI guarantees the two drift apart.

The rail is generated from ``ORDER``, so adding a phase to the state machine
(CALIBRATE and FLATS are still pending) makes it appear here with no change.

Run it::

    python -m ministar.ui.app            # real camera
    python -m ministar.ui.app --fake     # synthetic stars, no hardware
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from nicegui import ui

from ministar.config import Settings
from ministar.session import ORDER, SessionState
from ministar.ui import screens
from ministar.ui.station import FakeCamera, Station
from ministar.ui.theme import install


@dataclass
class App:
    session: SessionState
    settings: Settings
    station: Station | None


_APP: App | None = None


@ui.page("/")
def wizard_page() -> None:
    install()
    ui.label("ministar").classes("mn-title")

    app = _APP
    if app is None:  # pragma: no cover - only if imported without run()
        ui.label("app is not running").classes("mn-warn")
        return

    def go_back() -> None:
        index = ORDER.index(app.session.phase)
        if index > 0:
            app.session.phase = ORDER[index - 1]
            content.refresh()

    def go_next() -> None:
        index = ORDER.index(app.session.phase)
        if index >= len(ORDER) - 1 or not app.session.can_advance():
            return
        app.session.advance()
        content.refresh()

    @ui.refreshable
    def content() -> None:
        index = ORDER.index(app.session.phase)

        with ui.row().classes("items-center justify-center").style("gap: 4px; flex-wrap: wrap"):
            for i, phase in enumerate(ORDER):
                css = "mn-step"
                if phase is app.session.phase:
                    css += " mn-step-now"
                elif i < index:
                    css += " mn-step-done"
                ui.label(phase.name).classes(css).style("padding: 2px 6px")

        # Blockers, verbatim from the state machine. No toast/notification: those
        # are bright, and a white flash costs dark adaptation (brief 14 §6).
        for blocker in app.session.blockers():
            ui.label(f"* {blocker}").classes("mn-warn")

        context = screens.Context(
            state=app.session,
            settings=app.settings,
            station=app.station,
            refresh=content.refresh,
        )
        with (
            ui.column()
            .classes("mn-card")
            .style("width: 100%; max-width: 940px; padding: 16px; gap: 10px")
        ):
            ui.label(app.session.phase.name).classes("mn-heading")
            renderer = screens.RENDERERS.get(app.session.phase)
            if renderer is None:
                ui.label("no screen for this phase").classes("mn-warn")
            else:
                renderer(context)

        with ui.row().classes("justify-center").style("gap: 10px; margin-top: 8px"):
            back = ui.button("back", on_click=go_back).classes("mn-action")
            forward = ui.button("next", on_click=go_next).classes("mn-action")
            back.set_enabled(index > 0)
            forward.set_enabled(index < len(ORDER) - 1 and app.session.can_advance())

    content()


def run(
    *,
    fake: bool = False,
    host: str = "127.0.0.1",
    port: int = 8080,
    native: bool = False,
    show: bool = True,
) -> None:
    global _APP

    settings = Settings.load()

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

    station = Station(camera, initial_error=initial_error)
    station.start()
    _APP = App(session=SessionState(), settings=settings, station=station)

    try:
        ui.run(
            host=host,
            port=port,
            title="ministar",
            dark=True,
            reload=False,
            show=show,
            native=native,
        )
    finally:
        station.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ministar session wizard")
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
