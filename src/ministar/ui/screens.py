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

import asyncio
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from nicegui import ui
from nicegui.run import io_bound

from ministar import preflight as preflight_checks
from ministar import quality
from ministar import sky as sky_module
from ministar.camera.base import CameraSettings
from ministar.catalog import COMMON_TYPES, DEFAULT_TARGET_TYPES, TYPE_LABELS, catalog
from ministar.config import Settings, default_session_name, safe_folder_name
from ministar.exposure import choose_exposure, rule_of_500_seconds
from ministar.io.tiff import TiffError, read_frame_exif
from ministar.pointing import (
    SolveReport,
    altaz_advice,
    at_entry,
    best_start,
    entry_corner_name,
    entry_tolerance_arcmin,
    frame_advice,
    keep_capture,
    solve_hints,
)
from ministar.reframe.roll import parallactic_angle, roll_advice, roll_inputs
from ministar.rig import RigProfile, reconcile, rig_path
from ministar.rig import load as load_rig
from ministar.rig import save as save_rig
from ministar.session import Phase, SessionState
from ministar.site import Site, site_path
from ministar.site import load as load_site
from ministar.site import save as save_site
from ministar.site import validate as validate_site
from ministar.targets import Filters as TargetFilters
from ministar.targets import Row, compute_visibility, filter_rows, top
from ministar.ui import focus as focus_screen
from ministar.ui import overlay
from ministar.ui.preview import live_image
from ministar.ui.station import Station

#: Rows sent to the browser. Quasar renders client-side, so all ~13 000 would be
#: wasteful; the cap is reported on screen so nothing disappears silently.
TARGET_TABLE_LIMIT = 300

#: Sentinel choices in the object-type dropdown. A multi-select of every code rendered
#: sixteen chips and overflowed the panel, so the control is single-select and offers
#: presets: one chip on screen whatever the catalogue contains.
ALL_TYPES = "(all)"
DEEP_SKY_PRESET = "(deep sky)"


def types_for(choice: str | None) -> frozenset[str] | None:
    """Map a dropdown choice to a type filter. ``None`` means no constraint.

    "(all)" must mean *all*, including the 547 individual stars that the deep-sky
    default hides -- otherwise the option would silently lie about its name.
    """
    if not choice or choice == ALL_TYPES:
        return None
    if choice == DEEP_SKY_PRESET:
        return frozenset(DEFAULT_TARGET_TYPES)
    return frozenset({choice})


#: Sentinels so a numeric column still sorts while meaning "unknown". The column
#: formatters render them as "--".
_UNKNOWN_SIZE = -1.0
_UNKNOWN_MAG = 99.0

TARGET_COLUMNS = [
    {"name": "label", "label": "target", "field": "label", "align": "left", "sortable": True},
    {"name": "type", "label": "type", "field": "type", "sortable": True},
    {
        "name": "size",
        "label": "size (')",
        "field": "size",
        "sortable": True,
        ":format": "v => (v < 0 ? '--' : v)",
    },
    {
        # One scale, not two: the band is folded in by Target.approx_magnitude, and
        # "approx." is there because the B-to-V shift is a typical colour rather than
        # a measurement of this object. See APPROX_MAG_HELP.
        "name": "mag",
        "label": "approx. mag",
        "field": "mag",
        "sortable": True,
        ":format": "v => (v >= 99 ? '--' : v)",
    },
    {
        "name": "sb",
        "label": "surf br",
        "field": "sb",
        "sortable": True,
        ":format": "v => (v < 0 ? '--' : v)",
    },
    {
        # Seconds before the object leaves the frame, at its own size and declination.
        # The headline number of the whole frame-fit idea: minutes are what a person
        # plans an evening in, percentages are not.
        "name": "window",
        "label": "window (s)",
        "field": "window",
        "sortable": True,
        ":format": "v => (v >= 999999 ? '--' : Math.round(v))",
    },
    {"name": "maxalt", "label": "max alt", "field": "maxalt", "sortable": True},
    {"name": "minutes", "label": "tonight (min)", "field": "minutes", "sortable": True},
    {
        "name": "verdict",
        "label": "verdict",
        "field": "verdict",
        "sortable": True,
        "align": "left",
    },
]


#: Column headings explain themselves on hover. These are the two numbers a beginner
#: is most likely to misread -- "V" and "B" look like grades, and a surface brightness
#: looks like a magnitude -- and an explanation that lives in a document is one nobody
#: reads at the telescope.
APPROX_MAG_HELP = (
    "Brightness measured through a colour filter. V (visual, green-yellow) is what the "
    "eye and a camera see, but only 30% of the catalogue has it; the rest only ever got "
    "a blue B measurement, which reads about 0.7 mag fainter for the same object, so "
    "those values are converted to match. Expect half a magnitude either way. "
    "Lower number = brighter."
)
SURFACE_BRIGHTNESS_HELP = (
    "How concentrated the light is, in magnitudes per square arcsecond -- not how much "
    "of it there is. A large faint galaxy and a small bright planetary nebula can share "
    "a total magnitude and differ here. Lower = brighter; above about 22 is a hard ask "
    "from a suburban sky. Blank means the catalogue has no value."
)
WINDOW_HELP = (
    "How long this object stays in the frame before you must re-aim, given how much of "
    "the frame it fills and how fast the sky moves where it sits. Near the celestial "
    "pole the same object gives hours; near the equator it gives minutes. This assumes "
    "the drift runs along the long side of the frame -- rotating the camera makes that "
    "true. Blank means no rig profile is set."
)

#: Verdicts that mean "this will not work well tonight", used to dim the row rather
#: than hide it: still selectable, visibly de-emphasised.
DIM_VERDICTS = frozenset({"bigger than your frame", "short reframe window"})

#: Sentinel for "no limit": JSON cannot carry the ``inf`` that a pole target earns.
_NO_LIMIT_WINDOW = 999999.0

#: Asked of the user, not of the sky: the slider has no meaning without the question it
#: answers, and "window (s)" in the table is not the same thing as a willingness.
WINDOW_QUESTION = "How frequently are you willing to reframe?"

#: Heading help keyed by column name, so a test can check every entry names a real
#: column and that the two misreadable ones are covered.
COLUMN_HELP = {
    "mag": APPROX_MAG_HELP,
    "sb": SURFACE_BRIGHTNESS_HELP,
    "window": WINDOW_HELP,
}


def _header_slot(label: str, tooltip: str) -> str:
    """A header cell: the label, a small (?), and its explanation on hover.

    ``q-th :props="props"`` is exactly what Quasar's own header cell renders, so
    binding the slot props to it keeps the column sortable, keeps the sort arrow, and
    keeps the keyboard and aria-sort behaviour. Hand-rolling the ``th`` loses all
    three silently -- the table still looks right and stops sorting.
    """
    return (
        '<q-th :props="props">'
        f"<span>{label}</span>"
        '<q-icon name="help_outline" size="13px" class="q-ml-xs" style="opacity: 0.55" />'
        f'<q-tooltip style="font-size: 12px; max-width: 320px">{tooltip}</q-tooltip>'
        "</q-th>"
    )


def window_hint_for(optics, seconds: float) -> str:
    """The size a window implies, at a *stated* declination.

    A global control cannot know every row's declination, so it names the one it used
    (dec 45, a representative mid-latitude target) and the table carries the real
    per-row number regardless. Without a rig there is no size to quote, and saying so
    is better than quoting a default.

    The window itself is not repeated: the slider handle already reads in minutes.
    """
    if optics is None:
        return "size unknown: no rig profile"
    size = optics.hint_size_arcmin(min_window_s=seconds)
    return f"up to {size:.0f}' at dec 45"


def window_slider_max_minutes(optics) -> int:
    """How far the minimum-window slider can go, in whole minutes.

    Even a point source has a ceiling -- the frame takes that long to cross -- so the
    slider stops there instead of offering settings the sky cannot honour. Without a
    rig there is nothing to clamp to and the full hour stands.
    """
    if optics is None:
        return 60
    reachable = optics.window_seconds(fill=0.0, dec_deg=0.0) / 60.0
    return max(1, min(60, int(reachable)))


def interval_slider_max_minutes(
    ceiling_seconds: float | None, zero_seconds: float | None = None
) -> int:
    """How far the PARAMETERS slider may go, in whole minutes.

    Two ceilings, whichever binds first. The target's own window is the tight one, but
    even with no target selected the frame collapses to nothing once the drift exceeds
    it -- brief 10 §2.1b: "the slider must be clamped at the zero point". Past that the
    readout would be 0.0 MP for every setting, which is not a setting.

    A floor of 1 minute, never 0: zero would silently mean "no interval chosen" in
    session state, which is a different thing from "the shortest interval".
    """
    limits = [60.0]
    for value in (ceiling_seconds, zero_seconds):
        if value is not None:
            limits.append(value / 60.0)
    return max(1, min(int(min(limits)), 60))


def session_directory(ctx: Context) -> Path:
    """Where this session's captures go.

    The automatic name is materialised here and kept in session state, so stopping and
    restarting a run writes into the same folder instead of starting a new session every
    time the shutter is resumed. Clearing the name on SETUP asks for a new one.
    """
    raw_root = os.path.expandvars(str(ctx.state.output_root or ctx.settings.paths.output))
    root = Path(raw_root).expanduser()
    raw = (ctx.state.session_name or "").strip()
    if ctx.state.session_name_auto:
        # Blank, or a name this app generated on an earlier RUN.
        name = raw or default_session_name(ctx.state.target_code or ctx.state.target_name or "")
    else:
        # The user typed this one: make it a single safe component. If nothing usable
        # remains, fall back to automatic rather than writing outside the root.
        name = safe_folder_name(raw)
        if not name:
            name = default_session_name(ctx.state.target_code or ctx.state.target_name or "")
            ctx.state.session_name_auto = True
    ctx.state.session_name = name
    return root / name


def select_row(ctx: Context, row: dict) -> None:
    """Record a clicked table row in session state, and unblock NEXT.

    Split out of the click handler because the second half was *missing* and the
    symptom was reported from the phone: selecting a target filled in the state, but
    the shell had already computed that "next" was not allowed and nothing told it to
    look again. A screen may change state mid-render, so it has to say so.
    """
    ctx.state.target_name = str(row.get("label") or "")
    new_code = str(row.get("code") or "")
    # An automatic session name belongs to the target that produced it. A different
    # target should not write into the previous one's folder.
    if ctx.state.session_name_auto and new_code != ctx.state.target_code:
        ctx.state.session_name = None
    ctx.state.target_code = new_code
    ctx.state.target_ra_deg = float(row["ra"])
    ctx.state.target_dec_deg = float(row["dec"])
    # Extent travels with the selection so PARAMETERS can clamp its slider without
    # loading the whole catalogue again.
    ctx.state.target_major_arcmin = row.get("major")
    ctx.state.target_minor_arcmin = row.get("minor")
    if ctx.nav is not None:
        ctx.nav()


def _table_row(index: int, row: Row) -> dict[str, object]:
    """Flatten a :class:`Row` into the dict the table wants."""
    return {
        "key": f"{index}-{row.target.name}",
        "label": row.target.label,
        "code": (f"M{row.target.messier}" if row.target.messier is not None else row.target.name),
        "type": TYPE_LABELS.get(row.target.type, row.target.type),
        "size": _UNKNOWN_SIZE if row.size_arcmin is None else round(row.size_arcmin, 2),
        "mag": _UNKNOWN_MAG if row.magnitude is None else round(row.magnitude, 2),
        "sb": (
            _UNKNOWN_SIZE
            if row.target.surface_brightness is None
            else round(row.target.surface_brightness, 2)
        ),
        "maxalt": round(row.max_altitude_deg, 1),
        "minutes": round(row.visible_minutes, 0),
        "window": (
            _NO_LIMIT_WINDOW
            if row.window_seconds is None or row.window_seconds == float("inf")
            else round(row.window_seconds, 0)
        ),
        "verdict": row.verdict,
        # Consumed client-side by :table-row-class-fn -- see render_target.
        "dim": row.verdict in DIM_VERDICTS,
        "ra": round(row.target.ra_deg, 5),
        "dec": round(row.target.dec_deg, 5),
        # Carried so PARAMETERS can clamp its slider without re-reading the catalogue.
        "major": row.target.major_arcmin,
        "minor": row.target.minor_arcmin,
    }


#: Browser geolocation, resolved through a promise. Fails on a non-secure context
#: (a LAN address over plain http), which is why manual entry must always work.
GEOLOCATE_JS = """
return await new Promise((resolve) => {
  if (!navigator.geolocation) { resolve(null); return; }
  navigator.geolocation.getCurrentPosition(
    (p) => resolve({lat: p.coords.latitude, lon: p.coords.longitude}),
    () => resolve(null),
    {timeout: 10000, maximumAge: 600000}
  );
});
"""

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
    #: Re-evaluate the shell's own chrome -- the blocker list and whether "next" is
    #: allowed -- without re-rendering this screen. Needed because a screen can change
    #: session state mid-render (selecting a target, say) and the button would otherwise
    #: keep the answer it computed before the click.
    nav: Callable[[], None] | None = None
    #: A profile that overrides the one on disk for this session -- fake-camera mode uses
    #: it so the screens can be tried at a real focal length without touching the
    #: measured profile in ``data/rig.json``.
    rig: RigProfile | None = None


#: Phases whose screen shows the camera's live view.
#:
#: Short on purpose. Live view is a *focusing aid*, not the imaging state: while it runs a DSLR
#: holds its mirror up, cycles the shutter, heats the sensor and drains the battery. So a phase
#: appears here only if it puts a preview in front of the operator, and the wizard stands the
#: body down on the way out of every other one.
#:
#: RUN is absent for the same reason: the loop owns the camera there, and the run itself images
#: with live view off. The reframe sub-cycle is the exception and turns it back on for its own
#: duration, because the operator has to aim by eye -- that is handled on the RUN page, where
#: the state is actually known.
LIVE_VIEW_PHASES: frozenset[Phase] = frozenset({Phase.FOCUS, Phase.POINT})

#: Exposure for a plate-solve capture, in seconds.
#:
#: A pointing capture is not an imaging sub and must not inherit whatever the last "Tv"
#: happened to be. On a dark field a short frame gives ASTAP too few stars to match; on a
#: fixed tripod a long one smears them. One second at the session ISO is enough at 100 mm
#: f/2.8, and trailing stays under a couple of pixels. A constant rather than the
#: PARAMETERS value on purpose: a 30 s sub must not turn every solve into a 30 s wait.
SOLVE_EXPOSURE_S = 1.0
SOLVE_ISO_DEFAULT = 800


def wants_live_view(phase: Phase) -> bool:
    """Whether the screen for ``phase`` shows live view.

    A pure function so the shell can ask without a station, and so a test can assert that no
    other phase quietly leaves the mirror up.
    """
    return phase in LIVE_VIEW_PHASES


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


def _go(ctx: Context, phase: Phase) -> None:
    """Jump to another phase. Used for "go and fix this" links."""
    ctx.state.phase = phase
    ctx.refresh()


def render_setup(ctx: Context) -> None:
    """Rig readiness: site, camera configuration, head profile.

    This is the in-the-field counterpart to PREFLIGHT. Location lives here because
    it is per-session rig readiness, not a property of the software installation.
    """
    path = site_path(ctx.settings.paths.data)
    site = load_site(path)
    if site.complete and not ctx.state.site_ready:
        ctx.state.site_ready = True

    ui.label("Observing site").classes("mn-heading")
    ui.label(site.describe()).classes("mn-body" if site.complete else "mn-warn")
    ui.label("Needed to rank targets. Nothing is assumed: both coordinates must be set.").classes(
        "mn-dim"
    )

    latitude = ui.input(
        "latitude (N positive)",
        value="" if site.latitude_deg is None else f"{site.latitude_deg:.4f}",
    ).classes("mn-body")
    longitude = ui.input(
        "longitude (E positive)",
        value="" if site.longitude_deg is None else f"{site.longitude_deg:.4f}",
    ).classes("mn-body")
    label = ui.input("name (optional)", value=site.name).classes("mn-body")
    message = ui.label("").classes("mn-warn")

    async def find_me() -> None:
        try:
            coords = await ui.run_javascript(GEOLOCATE_JS, timeout=20)
        except Exception as exc:
            message.text = f"could not read location: {type(exc).__name__}: {exc}"
            return
        if not isinstance(coords, dict) or "lat" not in coords:
            message.text = (
                "browser refused geolocation - it needs a secure context. "
                "127.0.0.1 works; a LAN address over plain http does not. Enter manually."
            )
            return
        latitude.value = f"{float(coords['lat']):.5f}"
        longitude.value = f"{float(coords['lon']):.5f}"
        message.text = "read from browser - press save"

    def save_clicked() -> None:
        problem = validate_site(latitude.value, longitude.value)
        if problem is not None:
            message.text = problem
            return
        updated = Site(
            latitude_deg=float(latitude.value) if (latitude.value or "").strip() else None,
            longitude_deg=float(longitude.value) if (longitude.value or "").strip() else None,
            name=(label.value or "").strip(),
        )
        save_site(path, updated)
        ctx.state.site_ready = updated.complete
        message.text = "saved: " + updated.describe()
        ctx.refresh()

    with ui.row().classes("items-center").style("gap: 10px"):
        ui.button("find me", on_click=find_me).classes("mn-action")
        ui.button("save", on_click=save_clicked).classes("mn-action")

    ui.label("Camera configuration").classes("mn-heading")
    ui.label("Confirm these on the body: most cannot be read over USB (08 §7.2b).").classes(
        "mn-dim"
    )
    for check in preflight_checks.CAMERA_CHECKS:
        ui.label(f"[ ] {check}").classes("mn-body")

    ui.label("Rig profile").classes("mn-heading")
    rig_store = rig_path(ctx.settings.paths.data)
    profile = rig_for(ctx)
    if profile is None:
        ui.label(
            "no profile yet: TARGET shows sizes in arcminutes and cannot judge the frame"
        ).classes("mn-warn")
    else:
        ui.label(profile.describe()).classes("mn-body")
        ui.label(
            f"recorded {profile.recorded or 'unknown'}   camera {profile.camera or '-'}"
            f"   lens {profile.lens or '-'}"
        ).classes("mn-dim")
    ui.label(
        "Read from a real frame: the plate scale comes out of its EXIF. A solve "
        "upgrades it later, and an EXIF value never overwrites a solved one."
    ).classes("mn-dim")

    rig_message = ui.label("").classes("mn-warn")

    async def read_rig() -> None:
        """Take one test frame and learn the rig from its EXIF.

        Async, and the camera work in a worker thread, because a capture is seconds long and
        this runs on the event loop that also feeds every other client. A blocking call here
        froze the whole console -- the page stopped answering while the shutter was open.

        The capture goes through ``station.capture_to`` so the pump is not downloading a
        live-view frame at the same time: two threads in one Canon session wedged both of
        them inside the SDK, and the visible symptom was that the focus screen never got a
        preview -- the event loop that would have rendered it was parked in EDSDK.
        """
        station = ctx.station
        camera = getattr(station, "camera", None) if station is not None else None
        if station is None or camera is None or not hasattr(camera, "capture_to"):
            rig_message.text = "camera capture is not available in this session"
            return

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        frame = ctx.settings.paths.frames / f"rigtest_{stamp}.cr2"

        def work() -> tuple[str, bool]:
            """Capture and read it, off the event loop. Returns (message, saved)."""
            station.capture_to(frame)
            exif = read_frame_exif(frame)
            candidate = RigProfile.from_frame_exif(exif, recorded=datetime.now().date().isoformat())
            current = load_rig(rig_store)
            merged = reconcile(current, candidate)
            if merged == current:
                # Nothing changed: same focal length, and the stored geometry is at least
                # as good. Say so plainly -- a silent no-op reads as a broken button.
                return (
                    f"kept the {current.source} profile at the same focal length: "
                    f"{current.describe()}",
                    False,
                )
            save_rig(rig_store, merged)
            if current is not None and not current.focal_matches(merged.focal_length_mm):
                # Name what was replaced: the whole complaint this fixes is a stale lens
                # config surviving silently, so replacing it silently would be half a fix.
                return (
                    f"saved: {merged.describe()} "
                    f"(replaced the {current.focal_length_mm:g} mm {current.source} profile)",
                    True,
                )
            if current is not None and merged.lens != current.lens:
                return f"saved: {merged.describe()} (lens now {merged.lens})", True
            return f"saved: {merged.describe()}", True

        # One shutter release per press: the button is disabled for the duration rather than
        # letting a second click queue a second exposure behind the first.
        rig_button.disable()
        rig_message.text = "taking a test frame..."
        try:
            message, saved = await io_bound(work)
        except (TiffError, RuntimeError, TimeoutError, OSError) as exc:
            rig_message.text = f"could not read the frame: {type(exc).__name__}: {exc}"
            return
        except ValueError as exc:
            rig_message.text = f"frame cannot define the rig: {exc}"
            return
        finally:
            rig_button.enable()
        rig_message.text = message
        if saved:
            ctx.refresh()

    with ui.row().classes("items-center").style("gap: 10px"):
        rig_button = ui.button("read from a test frame", on_click=read_rig).classes("mn-action")

    ui.label("Session files").classes("mn-heading")
    ui.label(
        "Captures go under this folder, in a folder named for the session. Leave the name "
        "blank and it becomes <date>-<time>-<target> when RUN starts."
    ).classes("mn-dim")

    def set_output_root(event) -> None:
        text = str(event.value or "").strip()
        ctx.state.output_root = text or None

    def set_session_name(event) -> None:
        text = str(event.value or "").strip()
        ctx.state.session_name = text or None
        ctx.state.session_name_auto = not text

    ui.input(
        "output folder",
        value=str(ctx.state.output_root or ctx.settings.paths.output),
        on_change=set_output_root,
    ).classes("mn-body").style("width: 540px")
    ui.input(
        "session folder (blank = automatic)",
        value=ctx.state.session_name or "",
        placeholder="auto: 2026_11_07-22-15-M31",
        on_change=set_session_name,
    ).classes("mn-body").style("width: 540px")

    ui.label("Tripod head").classes("mn-heading")
    _not_implemented("Head calibration profiles", "13-head-calibration.md")


def _night_for(local_now: datetime, site: Site) -> tuple[datetime, datetime, str]:
    """Astronomical darkness at the site, or a stated fallback.

    Computed from the real date and the site's coordinates rather than assuming
    20:00-04:00, because the assumption is wrong by hours at some times of year.
    """
    window = sky_module.astronomical_night(
        local_now,
        latitude_deg=site.latitude_deg,  # type: ignore[arg-type]
        longitude_deg=site.longitude_deg,  # type: ignore[arg-type]
    )
    if window is not None:
        return window[0], window[1], ""
    fallback_start, fallback_end = sky_module.night_window(local_now)
    return (
        fallback_start,
        fallback_end,
        "no astronomical darkness at this site tonight (the sun never reaches -18 deg); "
        "using an assumed 20:00-04:00 local window",
    )


def render_target(ctx: Context) -> None:
    """The target picker: real catalogue, real filters, tonight's visibility."""
    site = load_site(site_path(ctx.settings.paths.data))
    if not site.complete:
        ui.label("No observing site set.").classes("mn-warn")
        ui.label("Targets cannot be ranked without a latitude and longitude.").classes("mn-body")
        ui.button("go to SETUP", on_click=lambda: _go(ctx, Phase.SETUP)).classes("mn-action")
        return

    targets = catalog()
    if not targets:
        ui.label("Catalogue is empty: third_party/openngc is missing.").classes("mn-warn")
        return

    profile = rig_for(ctx)
    optics = profile.optics() if profile is not None else None

    window_start, window_end, window_note = _night_for(datetime.now().astimezone(), site)
    min_altitude = 25.0

    ui.label(f"site {site.describe()}").classes("mn-body")
    ui.label(
        f"astronomical night {window_start:%H:%M}-{window_end:%H:%M} UTC   |   "
        f"{len(targets)} objects in catalogue"
    ).classes("mn-dim")
    if window_note:
        ui.label(window_note).classes("mn-warn")
    if profile is None:
        ui.label(
            "no rig profile: sizes stay in arcminutes and the frame fit is unknown "
            "(SETUP can read one from a test frame)"
        ).classes("mn-warn")
    else:
        ui.label(f"frame {profile.describe()}").classes("mn-dim")

    with ui.row().classes("items-end").style("gap: 12px; flex-wrap: wrap"):
        magnitude = ui.number("max magnitude", value=10.0, min=0.0, max=20.0, step=0.5).classes(
            "mn-body"
        )
        visible = ui.number("min visible (min)", value=60.0, min=0.0, max=600.0, step=10).classes(
            "mn-body"
        )
        smallest = ui.number("min size (')", value=0.0, min=0.0, max=300.0, step=1).classes(
            "mn-body"
        )
        largest = ui.number("max size (')", value=300.0, min=0.0, max=600.0, step=10).classes(
            "mn-body"
        )
        search = ui.input("search").classes("mn-body")

    # The primary control, in the unit an evening is planned in. The default is the
    # floor rather than a judgement: raising it is the user's call, and the summary line
    # reports what was excluded instead of hiding it.
    # Minutes, not seconds: the handle's label must read "1" for one minute, and 60 for
    # an hour. The conversion to seconds happens once, where the filter is built.
    with ui.row().classes("items-center").style("gap: 12px; flex-wrap: wrap"):
        ui.label(WINDOW_QUESTION).classes("mn-body")
        window_slider = (
            ui.slider(min=1, max=window_slider_max_minutes(optics), step=1, value=1)
            .props("label-always")
            .classes("mn-slider")
            .style("width: 320px")
        )
        window_hint = ui.label("").classes("mn-dim")

    # Human labels: the raw codes ('G', 'Cl+N', '*Ass') are unreadable in a list.
    # Defaults exclude individual stars, of which the catalogue has 547 -- they would
    # otherwise fill the top of a magnitude-sorted list with Sirius and Rigel.
    type_options = {
        ALL_TYPES: "all types",
        DEEP_SKY_PRESET: "deep sky (recommended)",
        **{code: TYPE_LABELS.get(code, code) for code in sorted(COMMON_TYPES)},
    }
    types = (
        ui.select(type_options, value=DEEP_SKY_PRESET, label="object type")
        .classes("mn-body")
        .style("width: 300px")
    )

    summary = ui.label("").classes("mn-dim")
    selected_line = ui.label("no object selected - click a row").classes("mn-body")

    table = (
        ui.table(
            columns=TARGET_COLUMNS,
            rows=[],
            row_key="key",
            pagination=25,
        )
        .classes("mn-body")
        .style("width: 100%; cursor: pointer")
    )
    # Dim, do not hide. Quasar applies a class per row through this function prop, which
    # keeps rowClick, sorting and selection intact -- a body slot would silently stop all
    # three (and would quietly break selection at the telescope).
    table.props(":table-row-class-fn=\"row => row.dim ? 'mn-row-dim' : ''\"")

    # Each explained heading is a slot on the table itself, registered before the rows
    # are ever assigned. One hover, no extra chrome, no second row of small print.
    for name, help_text in COLUMN_HELP.items():
        label = next(column["label"] for column in TARGET_COLUMNS if column["name"] == name)
        table.add_slot(f"header-cell-{name}", _header_slot(str(label), help_text))

    # The expensive pass, once. Sliders only re-run the cheap filter below.
    visibility = compute_visibility(
        targets, site=site, start=window_start, end=window_end, min_altitude_deg=min_altitude
    )

    def current_filters() -> TargetFilters:
        return TargetFilters(
            max_magnitude=magnitude.value if magnitude.value else None,
            min_size_arcmin=smallest.value or None,
            # The max-size box starts above HUGE_ARCMIN, i.e. "no upper limit".
            max_size_arcmin=largest.value if largest.value and largest.value < 600 else None,
            types=types_for(types.value),
            search=search.value or "",
            min_visible_minutes=float(visible.value or 0.0),
            min_altitude_deg=min_altitude,
            # The slider is in minutes; the filter is in seconds.
            min_window_seconds=float(window_slider.value or 1.0) * 60.0,
        )

    def refresh_rows() -> None:
        rows = filter_rows(targets, visibility, current_filters(), optics=optics)
        shown = top(rows, TARGET_TABLE_LIMIT)
        table.rows = [_table_row(index, row) for index, row in enumerate(shown)]
        table.update()
        summary.text = (
            f"{len(shown)} shown of {len(rows)} matching "
            f"(cap {TARGET_TABLE_LIMIT}; loosen a filter to see more)"
        )
        window_hint.text = window_hint_for(optics, float(window_slider.value or 1.0) * 60.0)

    def on_row_click(event) -> None:
        # Quasar emits rowClick with (evt, row, index). Pick the dict by shape rather
        # than position, so the handler does not depend on argument order.
        row = next(
            (
                argument
                for argument in (getattr(event, "args", None) or [])
                if isinstance(argument, dict) and "ra" in argument
            ),
            None,
        )
        if row is None:
            return
        select_row(ctx, row)
        selected_line.text = (
            f"selected: {ctx.state.target_name}   "
            f"RA {ctx.state.target_ra_deg:.3f}   Dec {ctx.state.target_dec_deg:+.3f}"
        )

    table.on("rowClick", on_row_click)
    for widget in (magnitude, visible, window_slider, smallest, largest, search, types):
        widget.on_value_change(lambda _event: refresh_rows())

    refresh_rows()


def target_radius_arcmin(ctx: Context) -> float:
    """Half the target's long axis: how far its edge reaches from its centre.

    Shared by the "aim for the run" button *and* the advice, because a mismatch here is both
    invisible and self-contradictory: aiming without it parks the object's centre on the frame
    edge, and the advice then correctly reports that the object -- half of it -- is outside.
    """
    major = ctx.state.target_major_arcmin
    return 0.5 * float(major) if major else 0.0


def rig_for(ctx: Context) -> RigProfile | None:
    """This session's rig: the override if one was supplied, else what was measured."""
    return ctx.rig if ctx.rig is not None else load_rig(rig_path(ctx.settings.paths.data))


def geometry_svg(width_px: int, height_px: int, keep_width_px: float, keep_height_px: float) -> str:
    """Two nested rectangles: the frame, and what survives the drift.

    Drawn in frame pixel units via ``viewBox`` so the aspect ratio is the sensor's, not
    the container's -- a stretched frame would misrepresent how much is being lost. The
    retained rectangle is centred, which is where the sky actually sits between reframes.
    """
    keep_w = max(0.0, min(float(width_px), keep_width_px))
    keep_h = max(0.0, min(float(height_px), keep_height_px))
    x = (width_px - keep_w) / 2.0
    y = (height_px - keep_h) / 2.0
    return (
        f'<svg viewBox="0 0 {width_px} {height_px}" width="100%" '
        f'style="max-width: 340px; height: auto; display: block">'
        f'<rect x="0" y="0" width="{width_px}" height="{height_px}" '
        f'fill="#2a0a0c" stroke="#7a241c" stroke-width="2"/>'
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{keep_w:.1f}" height="{keep_h:.1f}" '
        f'fill="#a33226" fill-opacity="0.55" stroke="#ff5a4d" stroke-width="2"/>'
        "</svg>"
    )


def render_parameters(ctx: Context) -> None:
    """What the evening costs: exposure, ISO, how often to reframe, and what is left.

    Nothing here is a free choice. The focal length is measured, not asked for; the
    exposure is the longest sub the trailing budget allows, floored to a legal shutter
    speed; the interval is capped by the target's own size; and the megapixel readout is
    the consequence of the interval rather than a separate setting.
    """
    profile = rig_for(ctx)
    optics = profile.optics() if profile is not None else None
    dec = ctx.state.target_dec_deg

    ui.label(
        f"focal length {profile.focal_length_mm:g} mm   "
        f"(measured from a test frame, {profile.source})"
        if profile is not None and profile.focal_length_mm
        else "no measured focal length: SETUP reads one from a test frame"
    ).classes("mn-body" if profile is not None and profile.focal_length_mm else "mn-warn")
    if profile is not None:
        ctx.state.focal_length_mm = profile.focal_length_mm
        ui.label(f"frame {profile.describe()}").classes("mn-dim")

    # -- exposure: the trailing ceiling, floored to a legal Tv --------------
    if optics is not None and dec is not None:
        choice = choose_exposure(optics, dec)
        ctx.state.exposure_s = choice.seconds
        ui.label(
            f"exposure {choice.label}   (2 px of trailing allows {choice.ceiling_s:.2f} s; "
            f"this frame trails {choice.trail_px:.1f} px)"
        ).classes("mn-body")
        # The rule of 500 is the number a photographer arrives with. Show what it would do
        # rather than argue with it: on this rig it is more than twice the trail budget.
        loose = rule_of_500_seconds(optics)
        loose_trail = optics.drift_px(dec_deg=dec, seconds=loose)
        ui.label(
            f"the 500-rule would say {loose:.1f} s -- {loose_trail:.1f} px of trail, "
            f"stars about {loose_trail / 1.75:.1f}x their at-focus size"
        ).classes("mn-dim")
    else:
        ui.label("exposure needs a rig profile and a target's declination").classes("mn-warn")

    # -- ISO: only the values the body says it has --------------------------
    camera = getattr(ctx.station, "camera", None) if ctx.station is not None else None
    reporter = getattr(camera, "iso_options", None)
    values = tuple(reporter()) if callable(reporter) else ()
    if values:
        current = ctx.state.iso if ctx.state.iso in values else values[len(values) // 2]
        ctx.state.iso = current
        ui.select(
            {value: str(value) for value in values},
            value=current,
            label="ISO",
            on_change=lambda event: setattr(ctx.state, "iso", int(event.value)),
        ).classes("mn-body")
    else:
        ui.label(
            "this body cannot report its ISO values yet (EdsGetPropertyDesc pending, 08 gap 1)"
        ).classes("mn-warn")

    # -- the interval, capped by the target's own window --------------------
    ceiling: float | None = None
    if optics is not None and ctx.state.target_major_arcmin is not None and dec is not None:
        ceiling = optics.window_seconds_for(
            major_arcmin=ctx.state.target_major_arcmin,
            minor_arcmin=ctx.state.target_minor_arcmin,
            dec_deg=dec,
        )

    # Zero usable pixels: the drift has eaten the whole frame.
    zero_s = (
        optics.window_seconds(fill=0.0, dec_deg=dec)
        if optics is not None and dec is not None
        else None
    )
    max_min = interval_slider_max_minutes(ceiling, zero_s)
    current_min = float(ctx.state.reframe_interval_s or 300.0) / 60.0
    value = int(min(max(1.0, round(current_min)), max_min))

    ui.label(
        f"{ctx.state.target_name or 'the target'} allows up to {ceiling / 60:.0f} min "
        f"({ceiling:.0f} s) between reframes"
        if ceiling is not None
        else "select a target to see how long it allows between reframes"
    ).classes("mn-body")

    hint = ui.label("").classes("mn-dim")

    @ui.refreshable
    def retained() -> None:
        """How much frame is left at the chosen interval -- the cost, made visible."""
        seconds = float(ctx.state.reframe_interval_s or 300.0)
        if optics is None or dec is None:
            ui.label("usable megapixels need a rig profile and a target").classes("mn-warn")
            return
        usable = optics.usable_megapixels(dec_deg=dec, seconds=seconds)
        full = optics.full_megapixels()
        drift = optics.drift_px(dec_deg=dec, seconds=seconds)
        ui.html(
            geometry_svg(
                optics.width_px,
                optics.height_px,
                optics.width_px - drift,
                optics.height_px,
            )
        )
        kept = 100.0 * usable / full if full else 0.0
        ui.label(
            f"{usable:.1f} of {full:.1f} MP usable   ({kept:.0f}% of the frame, "
            f"{drift:.0f} px of drift)"
        ).classes("mn-metrics")
        if optics.rotation_dominates(dec_deg=dec, seconds=seconds):
            # Not a warning: the number is measured, this explains why it is lower than a
            # drift-only reading would suggest. Stacking trims the turning edges anyway.
            ui.label(
                "the sky turns here rather than drifts: the count includes field rotation, "
                "and stacking will trim the outer edges"
            ).classes("mn-dim")
        ctx.state.usable_mp = usable

    def describe(minutes: float) -> str:
        return f"taking a frame every {minutes:.0f} min ({minutes * 60:.0f} s)"

    def changed(event) -> None:
        minutes = float(event.value)
        ctx.state.reframe_interval_s = minutes * 60.0
        hint.text = describe(minutes)
        retained.refresh()

    ui.slider(min=1, max=max_min, step=1, value=value, on_change=changed).props(
        "label-always"
    ).classes("mn-slider").style("width: 340px")
    if zero_s is not None and max_min * 60 >= zero_s:
        ui.label(
            f"slider limit {max_min} min: past that the drift has eaten the whole frame"
        ).classes("mn-dim")
    else:
        ui.label(f"slider limit {max_min} min, set by the target").classes("mn-dim")
    hint.text = describe(float(value))
    # Record the default too, so leaving the screen does not leave the session claiming
    # no interval was chosen.
    ctx.state.reframe_interval_s = float(value) * 60.0

    retained()


def render_focus(ctx: Context) -> None:
    """Real: the verified focus path."""
    if ctx.station is None:
        ui.label("no camera").classes("mn-warn")
        return
    # This screen shows the live view, so the mirror may come up now. (It stays down until
    # a screen asks: see Station.want_live_view.)
    ctx.station.want_live_view(True)

    def accept(snapshot) -> None:
        ctx.state.focus_position = snapshot.position
        ctx.state.focus_hfd = snapshot.hfd if snapshot.stars else None
        # Also in arcseconds, measured from the live view's own pixel width: later, a frame is
        # compared against this one, and a binned live view's pixels are not a still's
        # (quality.hfd_arcsec).
        rig = rig_for(ctx)
        ctx.state.focus_hfd_arcsec = None
        if ctx.state.focus_hfd is not None and rig is not None and snapshot.width:
            optics = rig.optics()
            ctx.state.focus_hfd_arcsec = quality.hfd_arcsec(
                ctx.state.focus_hfd,
                width_px=snapshot.width,
                width_arcsec=optics.width_arcsec,
            )
        ctx.refresh()

    focus_screen.render_focus(
        ctx.station,
        on_accept=accept,
        # Every sweep sample, written as it is measured: a field run's log is the only
        # record of what the fit actually saw.
        log_path=ctx.settings.paths.logs / "focus.jsonl",
    )
    if ctx.state.focus_hfd is not None:
        arcsec = ctx.state.focus_hfd_arcsec
        ui.label(
            f"recorded focus: position {ctx.state.focus_position:+d}, "
            f"HFD {ctx.state.focus_hfd:.2f} px"
            + (f' ({arcsec:.1f}")' if arcsec is not None else "")
        ).classes("mn-body")
    else:
        ui.label("no focus recorded yet").classes("mn-dim")


def render_calibrate(ctx: Context) -> None:
    _not_implemented("Head calibration", "13-head-calibration.md")


#: How long to wait for a moving mount to settle before a capture. Long enough for a hand to
#: come off a knob, short enough that a bump does not look like a hang.
SETTLE_TIMEOUT_S = 4.0

#: How long the field must hold still before a re-solve is worth spending.
#:
#: One second was too short: a hand leaves a 3-way head with a little residual motion, and
#: the published frame is an average of several camera frames, so a one-second window can
#: judge the *end* of a nudge as still. Two seconds is four published frames at the default
#: averaging, which is enough to see that the hand is really off.
AUTO_SOLVE_SETTLE_S = 2.0

#: Default grace after new arrows before the next automatic solve may fire.
#:
#: The operator can change this on the page: pointing is a human skill, and someone who can
#: put a target in the frame in two seconds should not be made to wait ten. This is the
#: minimum time from *showing the instruction* to spending another capture.
AUTO_RECHECK_S = 5.0

#: A real nudge needs this many consecutive published frames above the noise limit.
#:
#: One frame can be a JPEG artefact or a noise peak; two is a second of deliberate motion
#: at the default averaging, and a hand on a knob easily produces that.
AUTO_ARM_FRAMES = 2

#: Frame-to-frame change that is a hand on the head, not noise.
#:
#: A real nudge moves whole cells; the measured static floor is around 1 level per frame.
#: This is a floor, not a replacement for the field's own limit: whichever is larger wins.
#: It must stay above the noise a reset tracker sees after a solve, or turning the feature
#: on becomes a solve loop.
NUDGE_ARM_LEVELS = 2.5


def _aimed_now(ctx: Context, station) -> tuple[float, float] | None:
    """Where the camera is actually pointed, from the camera or from the last solve.

    A real body cannot report its own pointing; it reports it only through a plate solve.
    Before the solve exists there is nothing to draw, which is honest. After it exists the
    solution *is* the measurement, and refusing to use it because the camera has no
    ``pointing`` attribute is why the arrows and marks stayed missing on real hardware.
    """
    camera = getattr(station, "camera", None) if station is not None else None
    aimed = getattr(camera, "pointing", None)
    if aimed is not None:
        return aimed
    if ctx.state.solved and ctx.state.pointing_ra_deg is not None:
        return (ctx.state.pointing_ra_deg, ctx.state.pointing_dec_deg)
    return None


def roll_page_state(ctx: Context, station) -> tuple[float, float] | None:
    """The parallactic angle and the camera roll, or ``None`` when they are unknown.

    ``q`` needs the site, the target and the time; the roll needs either a solve or a camera
    that can report it. Nothing here guesses: without the pieces the page says what is
    missing, because a CW/CCW arrow that is wrong is worse than no arrow.
    """
    if ctx.state.target_ra_deg is None or ctx.state.target_dec_deg is None:
        return None
    site = load_site(site_path(ctx.settings.paths.data))
    if not site.complete:
        return None
    camera = getattr(station, "camera", None) if station is not None else None
    roll = getattr(camera, "roll_deg", None)
    # A solve measures the frame's position angle directly, which is the same quantity the
    # fake camera calls its roll. Requiring a camera that *reports* a roll excluded every
    # real body and left the page blank after a perfectly good solve.
    solved_pa = ctx.state.pointing_pa_deg if ctx.state.solved else None
    if roll is None and solved_pa is None:
        return None
    # Use where the camera *is* when it can say, not where we meant to point: the
    # parallactic angle depends on hour angle, so a mount that has been nudged has a
    # different roll advice than the target implies.
    aimed = _aimed_now(ctx, station)
    ra_deg, dec_deg = (
        aimed if aimed is not None else (ctx.state.target_ra_deg, ctx.state.target_dec_deg)
    )
    lst = sky_module.lst_deg(datetime.now().astimezone(), site.longitude_deg)
    hour_angle = (lst - ra_deg + 180.0) % 360.0 - 180.0
    q = parallactic_angle(site.latitude_deg, dec_deg, hour_angle)
    inputs = roll_inputs(
        solved_pa_deg=solved_pa,
        camera_roll_deg=None if roll is None else float(roll),
        camera_reports_pa=bool(getattr(camera, "reports_position_angle", False)),
        parallactic_deg=float(q),
    )
    return None if inputs is None else (float(inputs[0]), float(inputs[1]))


def auto_solve_ready_at(
    *, instruction_at: float, last_motion_at: float, recheck_s: float, settle_s: float
) -> float:
    """When the next automatic solve is allowed, given the two independent waits.

    The instruction grace and the settle overlap rather than queue: if the operator stops
    moving before the grace ends, the solve happens at the grace; if they are still working,
    it happens ``settle_s`` after they stop. The earlier of the two would capture a
    half-finished aim; the sum would punish someone who is already done.
    """
    return max(instruction_at + recheck_s, last_motion_at + settle_s)


def altaz_line(ctx: Context, ra_deg: float, dec_deg: float) -> str | None:
    """The correction in altitude/azimuth, for the hand on a pan/tilt head.

    The picture arrow is in image space, which answers "where does the target go on the
    screen" but not "which knob". This adds the same offset as ground-frame angles.
    """
    if ctx.state.target_ra_deg is None or ctx.state.target_dec_deg is None:
        return None
    site = load_site(site_path(ctx.settings.paths.data))
    if not site.complete:
        return None
    lst = sky_module.lst_deg(datetime.now().astimezone(), site.longitude_deg)
    advice = altaz_advice(
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        target_ra_deg=ctx.state.target_ra_deg,
        target_dec_deg=ctx.state.target_dec_deg,
        lst_deg=lst,
        latitude_deg=site.latitude_deg,
    )
    return advice.describe()


def render_point(ctx: Context) -> None:
    """POINT: the roll advice, the solving, and the readout that ties them together.

    **Roll** comes first because it is a physical knob and worth up to 1.5x between reframes
    (brief 10 §2.1c): the parallactic drift should run along the frame's long side, and a
    CW/CCW indicator is how the eye does that.

    **Solve** then anchors the WCS everything downstream needs -- the envelope, the reframe
    arrows, and the roll readout itself on a real camera, which cannot report its clamp angle.
    A solve that describes a *crop* is flagged rather than trusted (brief 07 §1).

    Both were once separate stages behind a "roll is aligned, solve" button. That button was
    friction without information: the page has no way to tell whether the roll looks aligned,
    so it was asking the user to assert something they could only have learned from the solve
    it was gating. Everything is visible at once now.
    """
    ui.label("Pointing").classes("mn-heading")
    station = ctx.station
    if station is not None:
        # POINT is the first screen that needs a preview. Starting live view here, not at
        # connect time, is what stops a launch from flipping the mirror.
        station.want_live_view(True)
    # One element: the live view is drawn *inside* the pointing SVG, so the marks cannot drift
    # from the stars. sanitize=False because the content is ours (a data URI and an SVG), and
    # the default DOMPurify pass is free to strip SVG it does not recognise.
    overlay_html = ui.html("", sanitize=False).classes("mn-overlay")
    ui.label(
        "circle = the target · reticle = where its run should start · dashed line = its path\n"
        "arrow = where the target must move; move the mount the opposite way"
    ).classes("mn-dim")
    # One stillness tracker for the page: the tick feeds it, the readout shows it, the settle
    # pause waits on it, and _solve refuses to spend a capture while it says "moving".
    stillness = quality.Stillness()
    mount_label = ui.label("").classes("mn-dim")

    # -- roll ---------------------------------------------------------------
    with ui.column().classes("w-full items-center"):
        ui.label("1. camera roll").classes("mn-heading")
        advice_line = ui.label("").classes("mn-heading")
        detail = ui.label("").classes("mn-body")
        missing = ui.label("").classes("mn-warn")
        with ui.row().classes("items-center justify-center").style("gap: 24px"):
            ccw_label = ui.label("<< CCW").classes("mn-metrics")
            cw_label = ui.label("CW >>").classes("mn-metrics")
        # Live aiming advice, on every camera. A real body cannot report its pointing until a
        # solve, so this stays empty before the first one; afterwards the tick keeps it current.
        aim_advice = ui.label("").classes("mn-body")

        camera = getattr(station, "camera", None) if station is not None else None
        site = load_site(site_path(ctx.settings.paths.data))
        if getattr(camera, "use_site", None) is not None and site.complete:
            # An altitude is a number without a sky until the fake knows where it stands.
            camera.use_site(site)
        if getattr(camera, "nudge_altaz", None) is not None:
            # Fake mount only: a real rig is moved by the user's hands, and buttons that
            # command nothing would be a lie about hardware. Altitude and azimuth, because
            # that is what a head on a tripod is adjusted in -- and because a degree of
            # azimuth is a different amount of sky depending on how high you are pointing.
            ui.label("aim the fake telescope").classes("mn-dim")
            pointing_label = ui.label("").classes("mn-body")
            step = (
                ui.select(
                    {"0.0833": "5'", "0.5": "30'", "2.0": "2 deg"},
                    value="0.0833",
                    label="step",
                )
                .classes("mn-body")
                .style("width: 120px")
            )

            def nudge(d_alt: float, d_az: float) -> None:
                step_deg = float(step.value or "0.0833")
                camera.nudge_altaz(d_alt_deg=step_deg * d_alt, d_az_deg=step_deg * d_az)
                tick()  # a click must never look dead

            with ui.row().classes("items-center justify-center").style("gap: 6px"):
                ui.button("Alt -", on_click=lambda: nudge(-1, 0)).classes("mn-drive")
                ui.button("Alt +", on_click=lambda: nudge(1, 0)).classes("mn-drive")
                ui.button("Az -", on_click=lambda: nudge(0, -1)).classes("mn-drive")
                ui.button("Az +", on_click=lambda: nudge(0, 1)).classes("mn-drive")

            def aim_at_target() -> None:
                if ctx.state.target_ra_deg is not None and ctx.state.target_dec_deg is not None:
                    camera.set_pointing(ctx.state.target_ra_deg, ctx.state.target_dec_deg)
                    tick()

            def aim_for_run() -> None:
                """Start the target at the edge it entered from: the whole frame of drift."""
                rig = rig_for(ctx)
                if rig is None:
                    return
                if ctx.state.target_ra_deg is None or ctx.state.target_dec_deg is None:
                    return
                camera.set_pointing(
                    *best_start(
                        target_ra_deg=ctx.state.target_ra_deg,
                        target_dec_deg=ctx.state.target_dec_deg,
                        optics=rig.optics(),
                        pa_y_deg=float(camera.roll_deg),
                        target_radius_arcmin=target_radius_arcmin(ctx),
                        interval_s=ctx.state.reframe_interval_s,
                    )
                )
                tick()

            with ui.row().classes("items-center justify-center").style("gap: 6px"):
                ui.button("aim at the target", on_click=aim_at_target).classes("mn-action")
                ui.button("aim for the run", on_click=aim_for_run).classes("mn-action")

            def toggle_drift(event) -> None:
                camera.set_drift(bool(event.value))
                tick()

            ui.checkbox(
                "let the sky move",
                value=bool(getattr(camera, "drift", True)),
                on_change=toggle_drift,
            ).classes("mn-body")
        else:
            pointing_label = None

        if getattr(camera, "set_roll", None) is not None:
            # Fake camera only: a real user turns the clamp, so there is no control to offer.
            ui.label("fake camera roll").classes("mn-dim")
            roll_slider = (
                ui.slider(min=-180, max=180, step=1, value=int(camera.roll_deg))
                .props("label-always")
                .classes("mn-slider")
                .style("width: 340px")
            )
            roll_slider.on_value_change(lambda event: camera.set_roll(float(event.value)))

    # -- solve --------------------------------------------------------------
    with ui.column().classes("w-full items-center"):
        ui.label("2. solve").classes("mn-heading")
        solve_facts = ui.label("").classes("mn-metrics")
        solve_advice = ui.label("").classes("mn-body")
        solve_notes = ui.label("").classes("mn-dim")
        solve_warnings = ui.label("").classes("mn-warn")
        solve_note = ui.label("").classes("mn-dim")
        settle_label = ui.label("").classes("mn-warn")

        def held_off() -> bool:
            return quality.should_hold_off(ctx.state.solve_failures)

        def show_buttons() -> None:
            """One decision at a time: solve, or retry once the solver has clearly given up."""
            solve_button.set_visibility(not held_off())
            retry_button.set_visibility(held_off())
            hold_label.text = (
                "two solves in a row found nothing: nothing more goes to the solver until "
                "you ask it to retry"
                if held_off()
                else ""
            )

        solve_busy = {"value": False}
        # Arriving on POINT arms nothing: the operator has not nudged yet, and solving an
        # unaimed frame is a wasted capture. A nudge after the arrows is what arms it.
        auto_armed = {"value": False}
        auto_hits = {"value": 0}
        last_motion_at = {"value": time.monotonic()}
        instruction_at = {"value": time.monotonic()}

        async def run_solve(*, skip_settle: bool = False) -> None:
            if solve_busy["value"]:
                return
            solve_busy["value"] = True
            try:
                # A hand on the mount produces smeared frames that solve badly or not at all,
                # so wait for the settle window before spending one. Bounded: if it never
                # settles, say so rather than hang.
                if not skip_settle and not ignore_motion.value:
                    deadline = time.monotonic() + SETTLE_TIMEOUT_S
                    while (
                        not stillness.steady_for(AUTO_SOLVE_SETTLE_S)
                        and time.monotonic() < deadline
                    ):
                        settle_label.text = (
                            f"{stillness.describe()} -- waiting for the mount to settle"
                        )
                        await asyncio.sleep(0.2)
                settle_label.text = ""
                await _run_and_show()
            finally:
                solve_busy["value"] = False
                # The result is on screen now: this starts the operator's action window.
                instruction_at["value"] = time.monotonic()
                # Whatever happened, a new nudge must be seen before the next automatic run.
                auto_armed["value"] = False
                auto_hits["value"] = 0

        async def _run_and_show() -> None:
            # Off the event loop: a capture plus a blind solve is up to nine seconds, and the
            # page would be frozen for all of it -- including the live view it is showing.
            solved = await io_bound(
                partial(_solve, ctx, stillness, ignore_motion=bool(ignore_motion.value))
            )
            report, note = solved if solved is not None else (None, "solve was cancelled")
            solve_note.text = note
            show_buttons()
            # On the event loop, unlike the old refresh inside _solve: this only re-evaluates
            # the Next button (RUN needs a solve), and leaves the result labels in place.
            ctx.nav()
            if report is not None:
                solve_facts.text = "\n".join(report.facts)
                solve_notes.text = "\n".join(report.notes)
                solve_warnings.text = "\n".join(report.warnings)
                rig = rig_for(ctx)
                if rig is not None and ctx.state.pointing_ra_deg is not None:
                    lines = list(
                        frame_advice(
                            ra_deg=ctx.state.pointing_ra_deg,
                            dec_deg=ctx.state.pointing_dec_deg,
                            target_ra_deg=ctx.state.target_ra_deg,
                            target_dec_deg=ctx.state.target_dec_deg,
                            optics=rig.optics(),
                            pa_y_deg=ctx.state.pointing_pa_deg or 0.0,
                            target_radius_arcmin=target_radius_arcmin(ctx),
                            interval_s=ctx.state.reframe_interval_s,
                        ).describe()
                    )
                    hint = altaz_line(ctx, ctx.state.pointing_ra_deg, ctx.state.pointing_dec_deg)
                    if hint is not None:
                        lines.append(hint)
                    solve_advice.text = "\n".join(lines)

        async def retry_solve() -> None:
            ctx.state.solve_failures = 0
            await run_solve()

        def arm_auto(on: bool) -> None:
            """Turning the switch on starts a fresh instruction window; it does not solve."""
            if on:
                instruction_at["value"] = time.monotonic()
                auto_armed["value"] = False
                auto_hits["value"] = 0

        async def auto_solve_tick() -> None:
            """Solve after the operator has had time to act, and the hand is off the head.

            The order matters: new arrows, a grace period the operator controls, a real
            nudge, then a settle. The old loop captured on the first quiet second, which
            raced a hand that was still adjusting. No nudge means no solve -- re-solving an
            unchanged aim only costs a capture and interrupts the preview.
            """
            if not auto_solve.value or solve_busy["value"] or station is None:
                return
            snapshot = station.snapshot()
            now = time.monotonic()
            rate = float(snapshot.motion)
            limit = max(stillness.threshold, NUDGE_ARM_LEVELS)

            # Two consecutive frames above the field's own noise: one can be an artefact,
            # and a hand on a knob easily produces several.
            if rate >= limit:
                auto_hits["value"] += 1
                last_motion_at["value"] = now
                if auto_hits["value"] >= AUTO_ARM_FRAMES and not auto_armed["value"]:
                    auto_armed["value"] = True
                    print(
                        f"auto-solve: armed (motion {rate:.1f} >= {limit:.1f})",
                        flush=True,
                    )
            else:
                auto_hits["value"] = 0

            if not auto_armed["value"]:
                if rate >= 0:
                    # Visible proof that the scan is alive, and the number it is judging.
                    settle_label.text = (
                        f"auto: nudge the mount to re-solve (motion {rate:.1f}, "
                        f"arms above {limit:.1f})"
                    )
                return

            grace = max(1.0, float(recheck_slider.value or AUTO_RECHECK_S))
            ready_at = auto_solve_ready_at(
                instruction_at=instruction_at["value"],
                last_motion_at=last_motion_at["value"],
                recheck_s=grace,
                settle_s=AUTO_SOLVE_SETTLE_S,
            )
            if now < ready_at:
                remaining = ready_at - now
                if now - last_motion_at["value"] < AUTO_SOLVE_SETTLE_S:
                    settle_label.text = f"auto: waiting for the mount to settle ({remaining:.0f}s)"
                else:
                    settle_label.text = f"auto: re-checking in {remaining:.0f}s"
                return
            if not stillness.steady_for(AUTO_SOLVE_SETTLE_S, now):
                settle_label.text = "auto: settling..."
                return
            auto_armed["value"] = False
            auto_hits["value"] = 0
            print("auto-solve: settled, solving", flush=True)
            settle_label.text = "settled: re-solving..."
            await run_solve(skip_settle=True)
            settle_label.text = ""

        with ui.row().classes("items-center justify-center").style("gap: 8px"):
            solve_button = ui.button("capture and solve", on_click=run_solve).classes("mn-action")
            retry_button = ui.button("retry", on_click=retry_solve).classes("mn-retry")
            retry_button.set_visibility(False)
            ignore_motion = ui.switch("ignore the movement check").classes("mn-body")
        with ui.row().classes("items-center justify-center").style("gap: 8px"):
            auto_solve = ui.switch(
                "re-solve when the mount settles",
                value=True,
                on_change=lambda event: arm_auto(bool(event.value)),
            ).classes("mn-body")
            ui.label("recheck after").classes("mn-dim")
            recheck_slider = (
                ui.slider(min=2, max=20, step=1, value=AUTO_RECHECK_S)
                .props("label-always")
                .classes("mn-slider")
                .style("width: 220px")
            )
            ui.label("s").classes("mn-dim")
        ui.timer(0.5, auto_solve_tick)
        ui.label(
            "the movement check compares the picture with itself frame to frame. It is a "
            "guess: a starfield on a monitor, a dimming screen or a cloud edge can look like "
            "a nudge. Turn it off to capture anyway -- a smeared frame will simply not solve."
        ).classes("mn-dim")
        hold_label = ui.label("").classes("mn-warn")
        show_buttons()

    def aim_fake() -> None:
        """Point the fake telescope where the session is pointed, so the view is the target."""
        camera = getattr(station, "camera", None) if station is not None else None
        setter = getattr(camera, "set_pointing", None)
        if setter is None or camera.pointing is not None:
            return
        rig = rig_for(ctx)
        if rig is None or ctx.state.target_ra_deg is None or ctx.state.target_dec_deg is None:
            return
        setter(
            *best_start(
                target_ra_deg=ctx.state.target_ra_deg,
                target_dec_deg=ctx.state.target_dec_deg,
                optics=rig.optics(),
                pa_y_deg=float(getattr(camera, "roll_deg", 0.0)),
                target_radius_arcmin=target_radius_arcmin(ctx),
                interval_s=ctx.state.reframe_interval_s,
            )
        )

    def draw_overlay(snapshot, aimed, roll_deg, direction) -> None:
        """The same advice as the text, drawn on the live view: match the circle to the reticle."""
        rig = rig_for(ctx)
        if snapshot is None or not snapshot.width or not snapshot.height:
            overlay_html.set_content("")
            return
        if (
            aimed is None
            or roll_deg is None
            or rig is None
            or ctx.state.target_ra_deg is None
            or ctx.state.target_dec_deg is None
        ):
            # No geometry yet (no solve, no site, no target): show the frame, draw nothing on it.
            overlay_html.set_content(
                overlay.frame_svg(int(snapshot.width), int(snapshot.height), snapshot.data_uri)
            )
            return
        size = (int(snapshot.width), int(snapshot.height))
        painting = overlay.build(
            width=size[0],
            height=size[1],
            pointing=aimed,
            roll_deg=float(roll_deg),
            optics=rig.optics(),
            target_ra_deg=ctx.state.target_ra_deg,
            target_dec_deg=ctx.state.target_dec_deg,
            target_radius_arcmin=target_radius_arcmin(ctx),
            roll_direction=direction,
            interval_s=ctx.state.reframe_interval_s,
            hint=altaz_line(ctx, aimed[0], aimed[1]),
        )
        overlay_html.set_content(painting.svg(snapshot.data_uri))

    def tick() -> None:
        aim_fake()
        snapshot = station.snapshot() if station is not None else None
        if snapshot is not None:
            stillness.observe(snapshot, time.monotonic())
            mount_label.text = stillness.describe()
        state = roll_page_state(ctx, station)
        aimed_now = _aimed_now(ctx, station)
        if state is None:
            # The picture is still worth showing without the marks.
            draw_overlay(snapshot, None, None, None)
            advice_line.text = ""
            aim_advice.text = ""
            detail.text = ""
            ccw_label.classes(replace="mn-dim")
            cw_label.classes(replace="mn-dim")
            missing.text = (
                "select a target, set the site, and use a camera that reports its roll "
                "(the fake one does; a real body needs a solve first)"
            )
            return
        missing.text = ""
        if pointing_label is not None:
            mount_camera = getattr(station, "camera", None)
            aimed = getattr(mount_camera, "pointing", None)
            mount = getattr(mount_camera, "altaz", None)
            if aimed is not None:
                where = (
                    f"alt {mount[0]:.1f} deg  az {mount[1]:.1f} deg   " if mount is not None else ""
                )
                motion = ""
                rate_px = getattr(mount_camera, "drift_px_per_s", lambda: None)()
                rate_as = getattr(mount_camera, "drift_arcsec_per_s", lambda: None)()
                if getattr(mount_camera, "drift", False) and rate_px:
                    motion = f'   sky moving west {rate_as:.1f}"/s ({rate_px:.2f} px/s)'
                elif rate_px is not None:
                    motion = "   sky held still"
                pointing_label.text = (
                    f"telescope at {where}RA {aimed[0] / 15.0:.3f} h  Dec {aimed[1]:+.3f} deg"
                    f"{motion}"
                )
        q, roll = state
        advice = roll_advice(psi_deg=roll, parallactic_deg=q)
        advice_line.text = advice.describe().upper()
        detail.text = (
            f"roll {roll:+.0f} deg   parallactic {q:+.0f} deg   "
            f"drift {advice.angle_from_long_axis:.0f} deg off the long side"
        )
        ccw_label.classes(replace="mn-heading" if advice.direction == "ccw" else "mn-dim")
        cw_label.classes(replace="mn-heading" if advice.direction == "cw" else "mn-dim")
        draw_overlay(snapshot, aimed_now, roll, advice.direction)

        if aim_advice is not None:
            aimed = _aimed_now(ctx, station)
            rig = rig_for(ctx)
            if (
                aimed is not None
                and rig is not None
                and ctx.state.target_ra_deg is not None
                and ctx.state.target_dec_deg is not None
            ):
                lines = list(
                    frame_advice(
                        ra_deg=aimed[0],
                        dec_deg=aimed[1],
                        target_ra_deg=ctx.state.target_ra_deg,
                        target_dec_deg=ctx.state.target_dec_deg,
                        optics=rig.optics(),
                        pa_y_deg=roll,
                        target_radius_arcmin=target_radius_arcmin(ctx),
                        interval_s=ctx.state.reframe_interval_s,
                    ).describe()
                )
                hint = altaz_line(ctx, aimed[0], aimed[1])
                if hint is not None:
                    lines.append(hint)
                aim_advice.text = "\n".join(lines)

        # A completed solve fills the facts in; the tick does not re-run the solver.
        if ctx.state.solved and not solve_facts.text:
            solve_facts.text = (
                f"RA {ctx.state.pointing_ra_deg / 15.0:.4f} h   "
                f"Dec {ctx.state.pointing_dec_deg:+.4f} deg"
            )

    ui.timer(0.25, tick)


def _solve(
    ctx: Context, stillness=None, *, ignore_motion: bool = False
) -> tuple[SolveReport | None, str]:
    """Capture, convert to FITS, solve, and record the result in the session.

    ASTAP cannot read RAW (01 §9), so the capture is converted on the way through -- the same
    path the run loop will use for every frame.
    """
    from ministar import quality
    from ministar.focus.metrics import measure_hfd
    from ministar.io.fits import read_fits
    from ministar.pointing import describe
    from ministar.raw import convert_to_fits
    from ministar.solver import solve_with_fallback

    station = ctx.station
    camera = getattr(station, "camera", None) if station is not None else None
    if station is None or camera is None or getattr(camera, "capture_to", None) is None:
        return None, "this camera cannot capture in this session (the fake one cannot)"
    if (
        stillness is not None
        and not ignore_motion
        and not stillness.steady_for(AUTO_SOLVE_SETTLE_S)
    ):
        return None, "live view has not held still long enough -- wait, then solve again"
    if ctx.settings.astap is None:
        return None, "astap_cli.exe not found -- run preflight"

    # Note: the fake is aimed by the POINT page (buttons, or the run entry point), never
    # here. Re-aiming on every solve would silently undo the nudge the user just made -- and
    # the nudge is the measurement.

    # Set the solve exposure explicitly. The dials at POINT are whatever the body was last
    # left at, which may be a live-view-friendly fraction of a second -- and ASTAP needs
    # stars, not a preview. Restored immediately after the shutter so the operator's own
    # setting (and the run's, applied later) is not silently rewritten by a solve.
    solve_iso = int(ctx.state.iso or SOLVE_ISO_DEFAULT)
    restore = None
    changed = False
    if getattr(camera, "apply", None) is not None:
        # Read before writing, and refuse to write if the old values cannot be restored:
        # the failure that matters is leaving the body at the solve exposure, where live
        # view is grainy and a longer simulated exposure makes every frame look different.
        try:
            restore = station.get_settings()
        except Exception as exc:
            return None, (
                "cannot read the camera's current ISO and shutter, so a solve cannot put "
                f"them back: {type(exc).__name__}: {exc}"
            )
        if restore.iso <= 0 or restore.shutter_seconds <= 0:
            return None, (
                "cannot restore the camera after a solve: ISO or shutter speed is not "
                "readable (set ISO manually and leave the mode dial on M)"
            )
        try:
            station.apply(CameraSettings(iso=solve_iso, shutter_seconds=SOLVE_EXPOSURE_S))
            changed = True
        except Exception as exc:
            return None, f"could not set the solve exposure: {type(exc).__name__}: {exc}"
        print(
            f"solve: ISO {restore.iso} {restore.shutter_seconds:g}s -> "
            f"ISO {solve_iso} {SOLVE_EXPOSURE_S:g}s",
            flush=True,
        )

    opened_at = time.monotonic()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = ".fit" if getattr(camera, "disposable_frames", False) else ".cr2"
    raw = ctx.settings.paths.frames / f"point_{stamp}{suffix}"
    try:
        try:
            station.capture_to(raw)
        finally:
            if changed and restore is not None:
                try:
                    station.apply(restore)
                except Exception as exc:
                    print(
                        f"solve: could not restore the camera: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                else:
                    print("solve: camera settings restored", flush=True)
                # The exposure change -- and the restore -- is a scene change, not mount
                # motion. Without this the operator sees "mount moving" for the whole
                # stillness window after a solve, and the preview's frozen stretch is
                # still calibrated for the solve frame.
                if stillness is not None:
                    stillness.reset()
                restretch = getattr(station, "restretch", None)
                if restretch is not None:
                    restretch()
        if raw.suffix.lower() in {".fit", ".fits"}:
            converted = raw  # the fake writes what ASTAP reads directly
        elif ctx.settings.dcraw is not None:
            converted = convert_to_fits(raw, dcraw=ctx.settings.dcraw)
        else:
            converted = raw
        profile = rig_for(ctx)

        # Judge the frame before spending a solve on it. Measured on a centre crop because the
        # whole frame costs 0.80 s -- as much as a hinted solve (11 §"quality gate").
        focus_note = None
        if profile is not None and converted.suffix.lower() in {".fit", ".fits"}:
            try:
                image, _ = read_fits(converted)
                hfd_px, _stars = measure_hfd(quality.centre_crop(image))
                optics = profile.optics()
                width = int(image.shape[1])
                focus_note = quality.focus_verdict(
                    quality.hfd_arcsec(
                        hfd_px, width_px=width, width_arcsec=width * optics.plate_scale_arcsec
                    ),
                    ctx.state.focus_hfd_arcsec,
                )
            except Exception as exc:  # a diagnostic must never take the solve down
                focus_note = f"could not measure focus: {type(exc).__name__}: {exc}"

        hints = None
        if profile is not None:
            optics = profile.optics()
            # The first solve is blind but for the field of view: the target is where we
            # meant to point, not where we are. Later solves hint from the last *solution*,
            # which is measured (01 §5.1).
            previous = None
            elapsed = 0.0
            if ctx.state.solved and ctx.state.pointing_ra_deg is not None:
                previous = (ctx.state.pointing_ra_deg, ctx.state.pointing_dec_deg)
                if ctx.state.pointing_solved_at is not None:
                    gap = datetime.now(UTC) - ctx.state.pointing_solved_at
                    elapsed = max(0.0, gap.total_seconds())
            hints = solve_hints(
                fov_height_deg=optics.height_arcmin / 60.0,
                previous=previous,
                elapsed_s=elapsed,
                dec_deg=ctx.state.pointing_dec_deg,
            )
        # Hinted, because the sky has moved predictably since the last solve; blind as the
        # net, because a bumped mount moves unpredictably and a missed hint looks just like a
        # bad image (solve_with_fallback).
        result, used_blind = solve_with_fallback(
            converted,
            ctx.settings.astap,
            out_base=ctx.settings.paths.scratch / f"point_{stamp}",
            hints=hints,
        )
        # A pointing capture is a check, not an imaging frame. The camera's own file is
        # always deleted (a 20 MB RAW nothing will read again); the converted file survives
        # only if the solve failed, where it is the evidence. The .ini in scratch always
        # stays -- 254 bytes quoting the solver's verdict.
        if converted is not raw:
            raw.unlink(missing_ok=True)
        if not keep_capture(solved=result.solved, converted_is_raw=converted is raw):
            converted.unlink(missing_ok=True)
    except Exception as exc:  # a failed capture must not take the page down
        ctx.state.solve_failures += 1
        return None, f"solve failed: {type(exc).__name__}: {exc}"

    if not result.solved or result.solution is None:
        ctx.state.solve_failures += 1
        reason = result.error or result.error_meaning or "unknown"
        return None, f"not solved: {reason}"

    # Was the mount moving while the shutter was open? The pump's own history answers this
    # even though the UI thread was blocked: a smeared frame is not worth a solver's time, and
    # "you moved" is a better sentence than "no solution found".
    motion_note = None
    if station is not None and hasattr(station, "motion_since"):
        worst = station.motion_since(opened_at)
        # Against this field's own noise floor, which the page's tracker has been measuring.
        # A fixed number cannot serve both a quiet sensor and a noisy one
        # (quality.motion_threshold).
        floor = stillness.noise_floor if stillness is not None else -1.0
        moved = quality.motion_verdict(worst, noise_floor=floor)
        if moved is not None and quality.motion_blocked(
            worst, ignore=ignore_motion, noise_floor=floor
        ):
            if converted is not raw:
                raw.unlink(missing_ok=True)
            converted.unlink(missing_ok=True)
            return None, f"nothing solved: {moved}"
        if moved is not None:
            # The check is off by the operator's choice: keep the result and say what was seen,
            # because a solve that lands on a smeared frame still tells them where they point.
            motion_note = f"kept despite a movement reading: {moved}"

    solution = result.solution
    ctx.state.solve_failures = 0
    ctx.state.solved = True
    ctx.state.pointing_ra_deg, ctx.state.pointing_dec_deg = solution.crval
    ctx.state.pointing_pa_deg = solution.position_angle_deg
    ctx.state.pointing_scale_arcsec = solution.pixel_scale_arcsec
    ctx.state.pointing_solved_at = datetime.now(UTC)
    expected = None
    if profile is not None:
        expected = profile.optics().width_arcmin / 60.0
    report = describe(
        solution,
        width_px=int(getattr(camera, "last_width", 0) or 0) or (profile.width_px if profile else 0),
        expected_width_deg=expected,
    )
    # Do NOT refresh the page here. This runs on the io_bound worker, and a refresh
    # deletes the labels the caller is about to write `note` into -- NiceGUI then drops
    # those writes with "element deleted", which is why a *successful* solve looked like
    # nothing happened. The overlay and advice are updated by the caller and the tick;
    # the nav guard is refreshed there too, on the event loop.
    note = f"solved from {converted.name}"
    if used_blind:
        note += "  (blind: the hint missed, so the mount may have moved)"
    if focus_note is not None:
        note += f"\n{focus_note}"
    if motion_note is not None:
        note += f"\n{motion_note}"
    return report, note


def clock_text(seconds: float) -> str:
    """Duration as ``m:ss`` (or ``h:mm``), which is how a run is planned."""
    total = round(max(0.0, float(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def run_metrics_text(snapshot) -> str:
    """The HUD of brief 15 §6.4, as text.

    Pure and separate from the renderer so a test can check that every number the brief
    asks for is actually present -- frames accepted and rejected *with reasons*, integration
    time, coverage, the shrinking valid area, HFD, and the reframe countdown -- without a
    browser or a camera.
    """
    stats = snapshot.stats
    if stats is None:
        return "no frames yet"
    rejected = ", ".join(f"{reason} x{count}" for reason, count in stats.reasons)
    coverage = stats.coverage
    hfd = f"{stats.hfd_px:.1f} px" if math.isfinite(stats.hfd_px) else "--"
    lines = [
        f"integration  {clock_text(stats.integration_s)}   {stats.accepted} accepted frames",
        f"frames       {stats.accepted + stats.rejected} captured"
        f"   {stats.accepted} stacked   {stats.rejected} rejected"
        + (f"  ({rejected})" if rejected else ""),
        f"coverage     {coverage.fraction * 100:.0f}% seen"
        f"   {coverage.fully * 100:.0f}% in every frame",
    ]
    if stats.crop is not None:
        _x, _y, width, height = stats.crop
        # The preview is cropped to this rectangle, so it is what the user is looking at --
        # and why it was chosen matters: a centred crop can be smaller than the usable area.
        if stats.crop_rule == "target":
            why = "centred on the target"
        elif snapshot.target_known:
            # Falling back is the right call at the edge, but it must not look like a bug:
            # say what would fix it.
            why = "fully covered -- aim the target nearer the middle to centre it"
        else:
            why = "fully covered"
        lines.append(f"shown        {width} x {height} px  ({why})")
    lines += [
        f"preview      {stats.width} x {stats.height}   bin {stats.bin_factor}",
        f"field        {stats.rotation_deg:+.2f} deg since the first frame",
        f"focus        {hfd} HFD   {stats.stars} stars",
        f"defects      {stats.defect_fraction * 100:.1f}% of covered pixels rejected",
    ]
    if snapshot.reframe_due_in_s is not None:
        if snapshot.reframe_due_in_s >= 0:
            lines.append(f"reframe      in {clock_text(snapshot.reframe_due_in_s)}")
        else:
            lines.append(
                "reframe      past due: the target is leaving the frame -- stop, re-solve, "
                "and start again from the entry corner"
            )
    if snapshot.recoveries:
        lines.append(f"recovered    {snapshot.recoveries} bump(s) by re-solving the frame")
    if snapshot.kept_frames:
        lines.append(f"files        {snapshot.kept_frames} kept in {snapshot.frames_dir}")
    return "\n".join(lines)


def render_run(ctx: Context) -> None:
    """Capture, live stack, and every number the loop is spending (15 §6, 12 §6).

    The page owns no loop state: the runner is a process-wide singleton (``ministar.run``),
    and this renderer only starts, stops and draws it. That is what lets a page refresh --
    or a phone reconnecting -- show the run that is already happening.
    """
    from ministar import run as run_loop
    from ministar.run import RunConfig
    from ministar.stack.transform import FieldModel

    ui.label("Run").classes("mn-heading")
    station = ctx.station
    rig = rig_for(ctx)
    optics = rig.optics() if rig is not None else None

    image = live_image(style="max-width: 880px; width: 100%")
    image.set_visibility(False)
    metrics = ui.label("").classes("mn-metrics")
    warning = ui.label("").classes("mn-warn")
    hint = ui.label("").classes("mn-dim")
    # Materialise the automatic name now, so the path shown is the one the run will use.
    ui.label(f"captures: {session_directory(ctx)}").classes("mn-dim")

    progress = (
        ui.linear_progress(value=0.0, show_value=False).props("rounded").classes("mn-progress")
    )
    progress.set_visibility(False)

    # -- reframe ------------------------------------------------------------
    # The sub-cycle of 12 §6, built out of the POINT page rather than a second copy of it:
    # the live view, the entry marker, the solve and the fake-mount controls are exactly
    # what a re-point needs, so the box embeds that renderer when the loop stops for one.
    reframe_box = ui.column().classes("w-full items-center").style("gap: 6px; margin-top: 8px")
    reframe_box.set_visibility(False)
    point_host = None
    point_built = {"point": False}
    with reframe_box:
        reframe_instruction = ui.label("").classes("mn-heading")
        reframe_detail = ui.label("").classes("mn-metrics")
        ui.button("continue now", on_click=lambda: continue_now()).classes("mn-action")
        point_host = ui.column().classes("w-full items-center")

    def reframe_aim():
        """The mount's current pointing, as ``(ra, dec, pa, plate_scale)`` -- or ``None``.

        A fake (or any camera that reports where it looks) answers immediately, which is what
        lets the demo resume without pressing solve. A real body cannot, so the answer comes
        from a solve taken *after* the reframe began; one taken before the mount was touched
        describes the old aim and would confirm a move that never happened.
        """
        runner = run_loop.active()
        camera = getattr(station, "camera", None) if station is not None else None
        live = getattr(camera, "pointing", None)
        roll = getattr(camera, "roll_deg", None)
        if live is not None and roll is not None and optics is not None:
            return float(live[0]), float(live[1]), float(roll), optics.plate_scale_arcsec
        since = runner.snapshot().reframe_since if runner is not None else None
        solved_at = ctx.state.pointing_solved_at
        if (
            ctx.state.pointing_ra_deg is not None
            and ctx.state.pointing_dec_deg is not None
            and solved_at is not None
            and (since is None or solved_at >= since)
        ):
            scale = ctx.state.pointing_scale_arcsec or (
                optics.plate_scale_arcsec if optics is not None else None
            )
            return (
                float(ctx.state.pointing_ra_deg),
                float(ctx.state.pointing_dec_deg),
                float(ctx.state.pointing_pa_deg or 0.0),
                scale,
            )
        return None

    def aim_error(aim) -> float | None:
        if (
            aim is None
            or optics is None
            or ctx.state.target_ra_deg is None
            or ctx.state.target_dec_deg is None
        ):
            return None
        advice = frame_advice(
            ra_deg=aim[0],
            dec_deg=aim[1],
            target_ra_deg=ctx.state.target_ra_deg,
            target_dec_deg=ctx.state.target_dec_deg,
            optics=optics,
            pa_y_deg=aim[2],
            target_radius_arcmin=target_radius_arcmin(ctx),
            interval_s=ctx.state.reframe_interval_s,
        )
        return math.hypot(advice.move_east_arcmin, advice.move_north_arcmin)

    def at_entry_now(aim) -> bool:
        if (
            aim is None
            or optics is None
            or ctx.state.target_ra_deg is None
            or ctx.state.target_dec_deg is None
        ):
            return False
        return at_entry(
            ra_deg=aim[0],
            dec_deg=aim[1],
            target_ra_deg=ctx.state.target_ra_deg,
            target_dec_deg=ctx.state.target_dec_deg,
            optics=optics,
            pa_y_deg=aim[2],
            target_radius_arcmin=target_radius_arcmin(ctx),
            interval_s=ctx.state.reframe_interval_s,
        )

    def build_point() -> None:
        if point_built["point"] or point_host is None:
            return
        with point_host:
            render_point(ctx)
        point_built["point"] = True

    def drop_point() -> None:
        if point_host is not None and point_built["point"]:
            point_host.clear()
        point_built["point"] = False

    def continue_now() -> None:
        runner = run_loop.active()
        if runner is None:
            return
        if station is not None:
            # Suspend *before* the loop resumes: the pump must not be grabbing previews between
            # frames of a run, and a frame grabbed through a shutter release is 02 §1.7's warning.
            station.suspend()
            station.want_live_view(False)
        aim = reframe_aim()
        if aim is None:
            runner.resume()
        else:
            runner.resume_reframe(
                ra_deg=aim[0],
                dec_deg=aim[1],
                pa_y_deg=aim[2],
                plate_scale_arcsec=aim[3],
            )
        drop_point()

    with ui.row().classes("items-center justify-center").style("gap: 16px"):
        resolution = (
            ui.select(
                {"2": "4.5 MP  (bin 2)", "4": "1.1 MP  (bin 4)"},
                value=str(ctx.state.stack_bin),
                label="stack resolution",
            )
            .classes("mn-body")
            .style("width: 220px")
        )

        def set_resolution(event) -> None:
            ctx.state.stack_bin = int(event.value)

        resolution.on_value_change(set_resolution)
        restretch = ui.button(
            "restretch", on_click=lambda: run_loop.active() and run_loop.active().restretch()
        ).classes("mn-action")

    def resolve_again(path, when, model):
        """Re-solve the frame the model disagreed with, and hand back its solution.

        A hinted search around the last good centre, with a radius chosen for a *hand* on
        the mount rather than for drift; ``solve_with_fallback`` retries blind, so a bigger
        move costs a few seconds rather than a lost session.
        """
        if ctx.settings.astap is None or optics is None:
            return None
        from ministar.solver import SolveHints, solve_with_fallback

        hints = SolveHints(
            fov_height_deg=optics.height_arcmin / 60.0,
            ra_hours=model.ra_deg / 15.0,
            south_pole_distance_deg=model.dec_deg + 90.0,
            radius_deg=2.0,
        )
        result, _blind = solve_with_fallback(
            path,
            ctx.settings.astap,
            out_base=ctx.settings.paths.scratch / "run_resolve",
            hints=hints,
        )
        return result.solution if result.solved else None

    def start_run() -> None:
        if station is None or getattr(station, "camera", None) is None:
            hint.text = "no camera in this session"
            return
        if rig is None or ctx.state.pointing_ra_deg is None or ctx.state.pointing_pa_deg is None:
            hint.text = "solve on POINT first: the drift model needs a measured centre and roll"
            return
        if not ctx.state.exposure_s:
            hint.text = "choose an exposure on PARAMETERS first"
            return
        model = FieldModel(
            ra_deg=float(ctx.state.pointing_ra_deg),
            dec_deg=float(ctx.state.pointing_dec_deg),
            pa_y_deg=float(ctx.state.pointing_pa_deg),
            plate_scale_arcsec=optics.plate_scale_arcsec,
            # Only the solve's epoch matters here; the dimensions are replaced by the first
            # frame's, which is the only authoritative source for what dcraw wrote.
            width_px=optics.width_px,
            height_px=optics.height_px,
            epoch=ctx.state.pointing_solved_at or datetime.now(UTC),
        )
        hint.text = ""
        station.suspend()
        run_loop.start(
            capture=station.capture_to,
            # The station, not the camera: the runner sets ISO/Tv from its own thread, and an
            # SDK call from a thread that does not own the session never returns (station.py).
            camera=station,
            model=model,
            config=RunConfig(
                exposure_s=float(ctx.state.exposure_s),
                iso=int(ctx.state.iso or 800),
                reframe_interval_s=ctx.state.reframe_interval_s,
                bin_factor=int(ctx.state.stack_bin),
                keep_frames=not station.disposable_frames,
                target_name=ctx.state.target_name or "",
                target_ra_deg=ctx.state.target_ra_deg,
                target_dec_deg=ctx.state.target_dec_deg,
            ),
            frames_dir=session_directory(ctx),
            scratch_dir=ctx.settings.paths.scratch,
            dcraw=ctx.settings.dcraw,
            resolve=resolve_again,
        )

    def pause_or_resume() -> None:
        runner = run_loop.active()
        if runner is None:
            return
        if runner.snapshot().state == "paused":
            runner.resume()
        else:
            runner.pause()

    def stop_run() -> None:
        runner = run_loop.active()
        if runner is not None:
            runner.stop()
        if station is not None and station.suspended:
            station.resume()

    with ui.row().classes("items-center justify-center").style("gap: 8px"):
        start = ui.button("start", on_click=start_run).classes("mn-action")
        pause = ui.button("pause", on_click=pause_or_resume).classes("mn-action")
        stop = ui.button("stop", on_click=stop_run).classes("mn-action")

    def tick() -> None:
        runner = run_loop.active()
        if runner is None:
            metrics.text = run_metrics_text(run_loop.RunSnapshot())
            warning.text = ""
            image.set_visibility(False)
            start.set_enabled(True)
            pause.set_enabled(False)
            stop.set_enabled(False)
            restretch.set_enabled(False)
            resolution.set_enabled(True)
            return
        snapshot = runner.snapshot()
        if snapshot.data_uri:
            image.set_source(snapshot.data_uri)
            image.set_visibility(True)
        metrics.text = run_metrics_text(snapshot)
        warning.text = snapshot.error or snapshot.warning
        progress.set_visibility(snapshot.reframe_due_in_s is not None)
        progress.value = snapshot.reframe_progress
        running = snapshot.state == "running"
        paused = snapshot.state == "paused"
        reframing = snapshot.state == "reframe"
        reframe_box.set_visibility(reframing)
        if station is not None:
            # A run images with live view off; the reframe is a pause in which the operator aims
            # by eye, so it is the one part of RUN that wants a picture (and the mirror up).
            station.want_live_view(reframing)
        if reframing:
            if station is not None and station.suspended:
                # The loop paused instead of stopping, so the camera is free: resume the pump or
                # the POINT renderer below would draw a preview that never arrives.
                station.resume()
            build_point()
            aim = reframe_aim()
            error = aim_error(aim)
            corner = (
                entry_corner_name(
                    target_ra_deg=ctx.state.target_ra_deg,
                    target_dec_deg=ctx.state.target_dec_deg,
                    optics=optics,
                    pa_y_deg=aim[2] if aim is not None else (ctx.state.pointing_pa_deg or 0.0),
                    target_radius_arcmin=target_radius_arcmin(ctx),
                    interval_s=ctx.state.reframe_interval_s,
                )
                if (
                    optics is not None
                    and ctx.state.target_ra_deg is not None
                    and ctx.state.target_dec_deg is not None
                )
                else "entry"
            )
            reframe_instruction.text = f"re-point: put the target on the {corner} marker"
            if error is None:
                reframe_detail.text = (
                    "solve to confirm the new pointing -- the resume waits for a fresh solve"
                )
            else:
                tolerance = entry_tolerance_arcmin(optics) if optics is not None else 0.0
                reframe_detail.text = (
                    f"aim error {error:.0f}' of {tolerance:.0f}'"
                    f"   holds {runner.reframe_dwell.held_s:.0f}s"
                    f" of {runner.reframe_dwell.window_s:.0f}s before it resumes"
                )
            if runner.reframe_dwell.update(time.monotonic(), at_entry_now(aim)):
                continue_now()
        else:
            runner.reframe_dwell.reset()
            drop_point()
        if snapshot.state in {"stopped", "error"} and station is not None and station.suspended:
            # The loop can end without the page asking (an error, or the app closing), and a
            # station left suspended would freeze live view for every later screen.
            station.resume()
        start.set_enabled(not (running or paused or reframing))
        pause.set_enabled(running or paused)
        pause.text = "resume" if paused else "pause"
        stop.set_enabled(running or paused or reframing)
        restretch.set_enabled(bool(snapshot.stats and snapshot.stats.accepted))
        resolution.set_enabled(not (running or paused or reframing))

    tick()
    ui.timer(0.5, tick)


def render_done(ctx: Context) -> None:
    ui.label("Session finished.").classes("mn-heading")
    ui.label(ctx.state.summary()).classes("mn-metrics")
    ui.button("restart", on_click=lambda: (ctx.state.restart(), ctx.refresh())).classes("mn-action")


RENDERERS: dict[Phase, Callable[[Context], None]] = {
    Phase.PREFLIGHT: render_preflight,
    Phase.SETUP: render_setup,
    Phase.TARGET: render_target,
    Phase.PARAMETERS: render_parameters,
    Phase.FOCUS: render_focus,
    Phase.POINT: render_point,
    Phase.RUN: render_run,
    Phase.DONE: render_done,
}
