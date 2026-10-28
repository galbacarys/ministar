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
from datetime import datetime

from nicegui import ui

from ministar import preflight as preflight_checks
from ministar import sky as sky_module
from ministar.catalog import COMMON_TYPES, DEFAULT_TARGET_TYPES, TYPE_LABELS, catalog
from ministar.config import Settings
from ministar.exposure import choose_exposure
from ministar.io.tiff import TiffError, read_frame_exif
from ministar.pointing import SolveReport
from ministar.reframe.roll import parallactic_angle, roll_advice
from ministar.rig import RigProfile, better_than, rig_path
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


def select_row(ctx: Context, row: dict) -> None:
    """Record a clicked table row in session state, and unblock NEXT.

    Split out of the click handler because the second half was *missing* and the
    symptom was reported from the phone: selecting a target filled in the state, but
    the shell had already computed that "next" was not allowed and nothing told it to
    look again. A screen may change state mid-render, so it has to say so.
    """
    ctx.state.target_name = str(row.get("label") or "")
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

    def read_rig() -> None:
        camera = getattr(ctx.station, "camera", None) if ctx.station is not None else None
        if camera is None or not hasattr(camera, "capture_to"):
            rig_message.text = "camera capture is not available in this session"
            return
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        frame = ctx.settings.paths.frames / f"rigtest_{stamp}.cr2"
        try:
            camera.capture_to(frame)
            exif = read_frame_exif(frame)
            candidate = RigProfile.from_frame_exif(exif, recorded=datetime.now().date().isoformat())
        except (TiffError, RuntimeError, TimeoutError, OSError) as exc:
            rig_message.text = f"could not read the frame: {type(exc).__name__}: {exc}"
            return
        except ValueError as exc:
            rig_message.text = f"frame cannot define the rig: {exc}"
            return
        current = load_rig(rig_store)
        if not better_than(candidate, current):
            rig_message.text = f"kept the {current.source} profile: {current.describe()}"
            return
        save_rig(rig_store, candidate)
        rig_message.text = f"saved: {candidate.describe()}"
        ctx.refresh()

    with ui.row().classes("items-center").style("gap: 10px"):
        ui.button("read from a test frame", on_click=read_rig).classes("mn-action")

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
    if roll is None:
        return None
    lst = sky_module.lst_deg(datetime.now().astimezone(), site.longitude_deg)
    hour_angle = (lst - ctx.state.target_ra_deg + 180.0) % 360.0 - 180.0
    q = parallactic_angle(site.latitude_deg, ctx.state.target_dec_deg, hour_angle)
    return float(q), float(roll)


def render_point(ctx: Context) -> None:
    """POINT, in two stages.

    **1. Roll.** An untracked rig drags the sky along the celestial parallel, and whether that
    runs along the frame's long side decides how much of the frame survives a run -- worth up
    to 1.5x the time between reframes for a five-second action (brief 10 §2.1c). The eye does
    this, guided by a CW/CCW indicator, because it is a physical knob.

    **2. Solve.** A captured frame is converted to FITS and solved, which anchors the WCS that
    everything downstream needs: the envelope, the reframe arrows, and the roll readout itself
    on a real camera (which cannot report its clamp angle). A solve that describes a *crop* is
    flagged rather than trusted (brief 07 §1).
    """
    ui.label("Pointing").classes("mn-heading")
    station = ctx.station
    preview = ui.image("").classes("mn-preview")

    roll_stage = ui.column().classes("w-full items-center")
    solve_stage = ui.column().classes("w-full items-center")
    solve_stage.set_visibility(False)

    # -- stage 1: roll ------------------------------------------------------
    with roll_stage:
        ui.label("1. camera roll").classes("mn-heading")
        advice_line = ui.label("").classes("mn-heading")
        detail = ui.label("").classes("mn-body")
        missing = ui.label("").classes("mn-warn")
        with ui.row().classes("items-center justify-center").style("gap: 24px"):
            ccw_label = ui.label("<< CCW").classes("mn-metrics")
            cw_label = ui.label("CW >>").classes("mn-metrics")

        camera = getattr(station, "camera", None) if station is not None else None
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

    # -- stage 2: solve -----------------------------------------------------
    with solve_stage:
        ui.label("2. solve").classes("mn-heading")
        solve_facts = ui.label("").classes("mn-metrics")
        solve_notes = ui.label("").classes("mn-dim")
        solve_warnings = ui.label("").classes("mn-warn")
        solve_note = ui.label("").classes("mn-dim")

        def run_solve() -> None:
            report, note = _solve(ctx)
            solve_note.text = note
            if report is not None:
                solve_facts.text = "\n".join(report.facts)
                solve_notes.text = "\n".join(report.notes)
                solve_warnings.text = "\n".join(report.warnings)

        ui.button("capture and solve", on_click=run_solve).classes("mn-action")

    showing = {"roll": True}

    def goto_solve() -> None:
        showing["roll"] = False
        roll_stage.set_visibility(False)
        solve_stage.set_visibility(True)

    ui.button("roll is aligned, solve", on_click=goto_solve).classes("mn-action")

    def tick() -> None:
        snapshot = station.snapshot() if station is not None else None
        if snapshot is not None and snapshot.data_uri:
            preview.source = snapshot.data_uri
            preview.update()
        if not showing["roll"]:
            return

        state = roll_page_state(ctx, station)
        if state is None:
            advice_line.text = ""
            detail.text = ""
            ccw_label.classes(replace="mn-dim")
            cw_label.classes(replace="mn-dim")
            missing.text = (
                "select a target, set the site, and use a camera that reports its roll "
                "(the fake one does; a real body needs a solve first)"
            )
            return
        missing.text = ""
        q, roll = state
        advice = roll_advice(psi_deg=roll, parallactic_deg=q)
        advice_line.text = advice.describe().upper()
        detail.text = (
            f"roll {roll:+.0f} deg   parallactic {q:+.0f} deg   "
            f"drift {advice.angle_from_long_axis:.0f} deg off the long side"
        )
        ccw_label.classes(replace="mn-heading" if advice.direction == "ccw" else "mn-dim")
        cw_label.classes(replace="mn-heading" if advice.direction == "cw" else "mn-dim")

        # A completed solve fills the facts in; the tick does not re-run the solver.
        if ctx.state.solved and not solve_facts.text:
            solve_facts.text = (
                f"RA {ctx.state.pointing_ra_deg / 15.0:.4f} h   "
                f"Dec {ctx.state.pointing_dec_deg:+.4f} deg"
            )

    ui.timer(0.25, tick)


def _solve(ctx: Context) -> tuple[SolveReport | None, str]:
    """Capture, convert to FITS, solve, and record the result in the session.

    ASTAP cannot read RAW (01 §9), so the capture is converted on the way through -- the same
    path the run loop will use for every frame.
    """
    from ministar.pointing import describe
    from ministar.raw import convert_to_fits
    from ministar.solver import SolveHints, solve

    camera = getattr(ctx.station, "camera", None) if ctx.station is not None else None
    if camera is None or getattr(camera, "capture_to", None) is None:
        return None, "this camera cannot capture in this session (the fake one cannot)"
    if ctx.settings.astap is None:
        return None, "astap_cli.exe not found -- run preflight"

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    raw = ctx.settings.paths.frames / f"point_{stamp}.cr2"
    try:
        camera.capture_to(raw)
        converted = (
            convert_to_fits(raw, dcraw=ctx.settings.dcraw)
            if ctx.settings.dcraw is not None
            else raw
        )
        profile = rig_for(ctx)
        hints = None
        if profile is not None:
            optics = profile.optics()
            hints = SolveHints(fov_height_deg=optics.height_arcmin / 60.0)
        result = solve(
            converted,
            ctx.settings.astap,
            out_base=ctx.settings.paths.scratch / f"point_{stamp}",
            hints=hints,
        )
    except Exception as exc:  # a failed capture must not take the page down
        return None, f"solve failed: {type(exc).__name__}: {exc}"

    if not result.solved or result.solution is None:
        reason = result.error or result.error_meaning or "unknown"
        return None, f"not solved: {reason}"

    solution = result.solution
    ctx.state.solved = True
    ctx.state.pointing_ra_deg, ctx.state.pointing_dec_deg = solution.crval
    expected = None
    if profile is not None:
        expected = profile.optics().width_arcmin / 60.0
    report = describe(
        solution,
        width_px=int(getattr(camera, "last_width", 0) or 0) or (profile.width_px if profile else 0),
        expected_width_deg=expected,
    )
    ctx.refresh()
    return report, f"solved from {converted.name}"


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
    Phase.SETUP: render_setup,
    Phase.TARGET: render_target,
    Phase.PARAMETERS: render_parameters,
    Phase.FOCUS: render_focus,
    Phase.POINT: render_point,
    Phase.RUN: render_run,
    Phase.DONE: render_done,
}
