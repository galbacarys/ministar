"""One renderer per session phase.

Honesty rule for this scaffold: a screen renders real data where a backend exists
(PREFLIGHT, FOCUS) and says plainly what is missing where one does not. Nothing is
faked. The README's design stance -- *inform, never block* -- depends on the UI
telling the truth about its own state, so a placeholder that looks functional is
worse than one that admits it is not.

The mapping is keyed by :class:`ministar.session.Phase`, so adding a phase to the
state machine (CALIBRATE and FLATS are still pending) makes it appear in the wizard
without touching the shell.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nicegui import ui

from ministar import preflight as preflight_checks
from ministar.config import Settings
from ministar.session import Phase, SessionState
from ministar.ui import focus as focus_screen
from ministar.ui.station import Station

#: How each preflight status is shown. Hoisted out of the renderer so a test
#: can assert it covers everything preflight emits.
STATUS_GLYPH = {"ok": "OK  ", "warn": "!!  ", "fail": "FAIL", "info": "..  "}


@dataclass
class Context:
    """Everything a screen may read or write."""

    state: SessionState
    settings: Settings
    station: Station | None
    #: Re-render the whole wizard (after a state change).
    refresh: Callable[[], None]


def _not_implemented(what: str, brief: str) -> None:
    ui.label(f"{what} is not implemented yet.").classes("mn-warn")
    ui.label(f"Design: docs/research/{brief}").classes("mn-dim")


def _field(
    ctx: Context,
    label: str,
    attr: str,
    *,
    kind: str = "text",
    step: float | None = None,
) -> None:
    """A labelled input bound to one SessionState attribute."""
    current = getattr(ctx.state, attr)
    placeholder = "" if current is None else str(current)

    def commit(event) -> None:
        raw = event.value
        if kind == "number":
            try:
                value = float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                return
            if value is not None and attr == "iso":
                value = int(value)
        else:
            value = raw or None
        setattr(ctx.state, attr, value)

    row = ui.row().classes("items-center no-wrap").style("gap: 10px")
    with row:
        ui.label(label).classes("mn-body").style("min-width: 170px")
        ui.input(value=placeholder, on_change=commit).classes("mn-body").style("width: 190px")
        if step is not None:
            ui.label(f"(step {step})").classes("mn-dim")


# ---------------------------------------------------------------------------
def render_preflight(ctx: Context) -> None:
    """Real: the same checks the CLI runs."""
    holder = ui.column().classes("w-full")

    def draw() -> None:
        holder.clear()
        with holder:
            checks = preflight_checks.run(ctx.settings)
            blocking = preflight_checks.blocking(checks)
            for check in checks:
                glyph = STATUS_GLYPH[check.status]
                css = "mn-warn" if check.status == "fail" else "mn-body"
                ui.label(f"{glyph} {check.name}").classes(css)
                if check.detail:
                    ui.label(f"       {check.detail}").classes("mn-dim")
            if blocking:
                ui.label(f"{len(blocking)} blocking problem(s)").classes("mn-warn")
            else:
                ui.label("no blocking problems").classes("mn-body")

    ui.button("re-run checks", on_click=draw).classes("mn-action")
    draw()


def render_target(ctx: Context) -> None:
    _not_implemented("Target catalog", "10-target-catalog-and-reframe-envelope.md")
    ui.label("For now, enter the target by hand:").classes("mn-body")
    _field(ctx, "name", "target_name")
    _field(ctx, "RA (deg)", "target_ra_deg", kind="number")
    _field(ctx, "Dec (deg)", "target_dec_deg", kind="number")


def render_parameters(ctx: Context) -> None:
    ui.label("These feed the reframe envelope and the sub-length rule.").classes("mn-body")
    _field(ctx, "focal length (mm)", "focal_length_mm", kind="number")
    _field(ctx, "exposure (s)", "exposure_s", kind="number")
    _field(ctx, "ISO", "iso", kind="number")
    _field(ctx, "reframe interval (s)", "reframe_interval_s", kind="number")
    _not_implemented("Usable-megapixel preview", "10-target-catalog-and-reframe-envelope.md")


def render_focus(ctx: Context) -> None:
    """Real: the verified focus path."""
    if ctx.station is None:
        ui.label("no camera").classes("mn-warn")
        return

    def accept(snapshot) -> None:
        ctx.state.focus_position = snapshot.position
        ctx.state.focus_hfd = snapshot.hfd if snapshot.stars else None
        ctx.refresh()

    focus_screen.render_focus(ctx.station, on_accept=accept)
    if ctx.state.focus_hfd is not None:
        ui.label(
            f"recorded focus: position {ctx.state.focus_position:+d}, "
            f"HFD {ctx.state.focus_hfd:.2f} px"
        ).classes("mn-body")
    else:
        ui.label("no focus recorded yet").classes("mn-dim")


def render_calibrate(ctx: Context) -> None:
    _not_implemented("Head calibration", "13-head-calibration.md")


def render_point(ctx: Context) -> None:
    _not_implemented("Pointing solve and entry corner", "10-target-catalog-and-reframe-envelope.md")
    ui.label("The ASTAP driver works; what is missing is the capture path to feed it.").classes(
        "mn-body"
    )


def render_run(ctx: Context) -> None:
    _not_implemented("Capture and live stacking", "15-stacking-pipeline-and-calibration.md")
    if ctx.station is not None:
        snap = ctx.station.snapshot()
        ui.label(
            f"camera is live: frame {snap.frames}, HFD "
            + (f"{snap.hfd:.2f}" if snap.stars else "n/a")
        ).classes("mn-body")


def render_done(ctx: Context) -> None:
    ui.label("Session finished.").classes("mn-heading")
    ui.label(ctx.state.summary()).classes("mn-metrics")
    ui.button("restart", on_click=lambda: (ctx.state.restart(), ctx.refresh())).classes("mn-action")


RENDERERS: dict[Phase, Callable[[Context], None]] = {
    Phase.PREFLIGHT: render_preflight,
    Phase.TARGET: render_target,
    Phase.PARAMETERS: render_parameters,
    Phase.FOCUS: render_focus,
    Phase.POINT: render_point,
    Phase.RUN: render_run,
    Phase.DONE: render_done,
}
