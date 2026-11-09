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
import contextlib
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request
from nicegui import app as nicegui_app
from nicegui import ui

from ministar.config import Settings
from ministar.rig import RigProfile
from ministar.session import ORDER, STATE_FILENAME, Phase, SessionState
from ministar.ui import screens, statusbar
from ministar.ui.station import FakeCamera, Station
from ministar.ui.theme import install


@dataclass
class App:
    session: SessionState
    settings: Settings
    station: Station | None
    #: Fake-camera mode carries its own 135 mm rig, so the demo does not depend on (or
    #: disturb) the profile measured from a real frame.
    rig: RigProfile | None = None


_APP: App | None = None


def _session_path(settings: Settings) -> Path:
    return settings.paths.data / STATE_FILENAME


def _save_session(app: App | None) -> None:
    """Persist the wizard, swallowing disk errors: a full card must not kill teardown."""
    if app is None:
        return
    with contextlib.suppress(OSError):
        app.session.save(_session_path(app.settings))


def _exit_soon() -> None:
    """Let the HTTP response leave before the process does."""
    time.sleep(0.1)
    os._exit(0)


def _stop_station() -> None:
    """Stop the run loop, save the wizard, and release the camera. Idempotent."""
    from ministar import run as run_loop

    _save_session(_APP)
    run_loop.stop()
    if _APP is not None and _APP.station is not None:
        _APP.station.stop()


@nicegui_app.get("/api/status")
def _status() -> dict[str, object]:
    """Read-only health for the phone, systemd and a remote developer.

    Deliberately tiny and unauthenticated: it exposes no controls and no user data,
    only enough to answer "is the process up and does it own the camera?".
    """
    app = _APP
    if app is None:
        return {"running": False}
    snap = app.station.snapshot() if app.station is not None else None
    camera = None
    if snap is not None:
        camera = {
            "name": snap.name,
            "error": snap.error,
            "frames": snap.frames,
            "stars": snap.stars,
            "hfd": round(snap.hfd, 3) if math.isfinite(snap.hfd) else None,
            "width": snap.width,
            "height": snap.height,
        }
    return {"running": True, "phase": app.session.phase.name, "camera": camera}


# uvicorn turns SIGTERM/Ctrl-C into an ASGI shutdown. Without this hook the process
# exits with the camera still owned, which is exactly the wedged session the next
# launch has to recover from.
nicegui_app.on_shutdown(_stop_station)


@nicegui_app.post("/_shutdown")
def _shutdown(request: Request) -> dict[str, str]:
    """Close the camera, then exit. The clean counterpart to ``taskkill /F``.

    A killed process runs no Python teardown, so the body is left in PC live view and
    the next launch has to recover a stream it never owned. ``tools/ui.sh`` calls this
    before falling back to a hard kill, which makes a restart put the mirror down and
    close the session on the way out.

    Local only: this is a process kill switch, and the console already has no login.
    """
    client = request.client.host if request.client else ""
    if client not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}:
        raise HTTPException(status_code=403, detail="local only")
    _stop_station()
    threading.Thread(target=_exit_soon, name="shutdown", daemon=True).start()
    return {"status": "stopping"}


@ui.page("/")
def wizard_page() -> None:
    install()
    ui.label("ministar").classes("mn-title")

    app = _APP
    if app is None:  # pragma: no cover - only if imported without run()
        ui.label("app is not running").classes("mn-warn")
        return

    last_saved = {"payload": app.session.to_dict()}

    def persist() -> None:
        _save_session(app)
        last_saved["payload"] = app.session.to_dict()

    def autosave() -> None:
        payload = app.session.to_dict()
        if payload != last_saved["payload"]:
            _save_session(app)
            last_saved["payload"] = payload

    # Screens mutate the state directly, so a timer is the one place that catches
    # every change without threading a save call through every callback.
    ui.timer(2.0, autosave)

    def go_back() -> None:
        index = ORDER.index(app.session.phase)
        if index > 0:
            end_run_if_leaving(app)
            app.session.phase = ORDER[index - 1]
            apply_live_view(app)
            persist()
            content.refresh()

    def go_next() -> None:
        index = ORDER.index(app.session.phase)
        if index >= len(ORDER) - 1 or not app.session.can_advance():
            return
        end_run_if_leaving(app)
        app.session.advance()
        apply_live_view(app)
        persist()
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
        blockers = ui.column().classes("items-center").style("gap: 2px")

        context = screens.Context(
            state=app.session,
            settings=app.settings,
            station=app.station,
            refresh=content.refresh,
            rig=app.rig,
            # Deferred: update_nav is defined below, once the buttons exist.
            nav=lambda: update_nav(),
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

        def update_nav() -> None:
            """Re-evaluate the blocker list and the navigation buttons.

            One place decides both, so a screen that changes state mid-render cannot
            leave the shell showing a stale answer.
            """
            blockers.clear()
            with blockers:
                for blocker in app.session.blockers():
                    ui.label(f"* {blocker}").classes("mn-warn")
            position = ORDER.index(app.session.phase)
            back.set_enabled(position > 0)
            forward.set_enabled(position < len(ORDER) - 1 and app.session.can_advance())

        update_nav()

    content()
    # Beneath the wizard, on every phase: the camera going away is not a phase.
    statusbar.footer(app, on_reconnected=lambda: apply_live_view(app))


def apply_live_view(app: App) -> None:
    """State whether the camera should be in live view for the phase now on screen.

    Only the phases that show a preview ask for it (``screens.LIVE_VIEW_PHASES``). Everything
    else stands the body down, which on a DSLR means the mirror comes back and the EVF stream
    stops: live view is a focusing aid, not the imaging state, and leaving it running while the
    operator reads a table costs battery, heats the sensor and risks the camera sleeping with
    its mirror already up.

    The station does the SDK work on the pump thread; this only records the wish.
    """
    if app.station is not None:
        app.station.want_live_view(screens.wants_live_view(app.session.phase))


def end_run_if_leaving(app: App) -> None:
    """Stop the loop when the wizard walks away from RUN.

    A runner outlives a page refresh on purpose (the phone can reconnect), but it must not
    outlive the *phase*: leaving RUN with the shutter still going would keep capturing while
    the DONE screen says the session is over. The station is handed back to live view in the
    same place, so no screen inherits a frozen preview.
    """
    if app.session.phase is not Phase.RUN:
        return
    from ministar import run as run_loop

    run_loop.stop()
    if app.station is not None and app.station.suspended:
        app.station.resume()


def run(
    *,
    fake: bool = False,
    #: All interfaces by default: the console is used from a phone on the same private
    #: network (Tailscale), and a loopback-only server is unreachable from there. There
    #: is no authentication -- the security boundary is the network, so keep this off
    #: untrusted LANs.
    host: str = "0.0.0.0",
    port: int = 8080,
    native: bool = False,
    show: bool = True,
    #: Both or neither. NiceGUI passes these through to uvicorn and switches its own
    #: websocket protocol to wss, which is what makes geolocation and Wake Lock work.
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    #: The plain-HTTP side: CA download, captive probes and an HTTPS redirect.
    http_port: int | None = None,
    ca_cert: str | None = None,
) -> None:
    global _APP

    if bool(ssl_certfile) != bool(ssl_keyfile):
        raise ValueError("--ssl-certfile and --ssl-keyfile must be given together")
    scheme = "https" if ssl_certfile else "http"

    settings = Settings.load()
    settings.paths.ensure()
    session = SessionState.load(_session_path(settings))
    if resumed := session.prepare_resume():
        print(f"session: {resumed}", flush=True)

    if session.output_root is None:
        # The appliance prefers the stick over the card. If it is not mounted yet at
        # boot, the SETUP screen rescans when the user opens it.
        from ministar.storage import default_output_root

        if (usb := default_output_root()) is not None:
            session.output_root = str(usb)
            print(f"output: default USB {usb}", flush=True)

    fake_rig = RigProfile.fake() if fake else None
    if fake:
        # The fake shows the real sky when a star database is installed, which is what makes
        # its captures solvable. Without one it still runs, with random stars and no solving.
        from ministar.starcatalog import find_catalog

        catalog = find_catalog(settings, cache=settings.paths.data / "star-index.json")
        camera = FakeCamera(optics=fake_rig.optics() if fake_rig else None, catalog=catalog)
        connect = None
        initial_error = (
            "" if catalog is not None else "no ASTAP star database: fake frames cannot be solved"
        )
    else:
        from ministar.camera import make_camera

        # Platform choice: EDSDK on Windows, gphoto2 on Linux (MINISTAR_CAMERA overrides).
        camera = make_camera()
        # Open the session on the pump thread, never here: camera calls only complete on the
        # thread that opened the session (station.py), and blocking the event loop to connect
        # is how a sleepy body used to freeze the console. A failure shows up on the snapshot.
        connect = camera.connect
        initial_error = ""

    station = Station(camera, connect=connect, initial_error=initial_error)
    station.start()
    _APP = App(session=session, settings=settings, station=station, rig=fake_rig)

    landing = None
    if ssl_certfile and http_port:
        from ministar.ui.landing import LandingServer

        ca_path = Path(ca_cert) if ca_cert else Path(ssl_certfile).with_name("ca.crt")
        try:
            landing = LandingServer(
                host, http_port, ca_cert=ca_path if ca_path.is_file() else None
            )
            landing.start()
            print(f"landing page on http://<this-machine-ip>:{http_port}/")
        except OSError as exc:
            print(f"could not start the HTTP landing page on {http_port}: {exc}")

    if host in {"0.0.0.0", "::"}:
        # Say it out loud: binding all interfaces is a deliberate exposure, not a default
        # that quietly happens. There is no login, so the network is the boundary.
        print(
            f"serving on all interfaces, port {port} ({scheme}) -- no authentication; "
            "reach it at"
        )
        print(f"  {scheme}://<this-machine-ip>:{port}/   (Tailscale name or LAN address)")

    tls = {"ssl_certfile": ssl_certfile, "ssl_keyfile": ssl_keyfile} if ssl_certfile else {}
    try:
        ui.run(
            host=host,
            port=port,
            title="ministar",
            dark=True,
            reload=False,
            show=show,
            native=native,
            **tls,
        )
    finally:
        from ministar import run as run_loop

        run_loop.stop()
        station.stop()
        if landing is not None:
            landing.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ministar session wizard")
    parser.add_argument("--fake", action="store_true", help="synthetic stars, no camera")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="interface to bind (default: all of them, for phone access over Tailscale)",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--native", action="store_true", help="pywebview window")
    parser.add_argument("--no-show", action="store_true", help="do not open a browser")
    parser.add_argument("--ssl-certfile", help="TLS leaf certificate (PEM)")
    parser.add_argument("--ssl-keyfile", help="TLS leaf key (PEM)")
    parser.add_argument(
        "--http-port", type=int, help="plain HTTP landing/CA port (only with TLS)"
    )
    parser.add_argument("--ca-cert", help="CA certificate served by the landing page")
    args = parser.parse_args(argv)
    run(
        fake=args.fake,
        host=args.host,
        port=args.port,
        native=args.native,
        show=not args.no_show,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        http_port=args.http_port,
        ca_cert=args.ca_cert,
    )
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    raise SystemExit(main())
