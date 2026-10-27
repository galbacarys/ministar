"""Filtering and ranking the catalogue for tonight.

Pure logic with no UI, so the table's numbers can be tested directly. The UI only
draws what this returns.

Two deliberate limits, both from brief 10 §1.2:

* **These are advisory.** A filter hides rows from the current view; it never claims
  an object is unobservable. Anything filtered out can be shown again by loosening a
  control.
* **Frame-relative verdicts need an :class:`~ministar.optics.Optics`.** "Bigger than
  your frame" needs a plate scale, and TARGET runs *before* PARAMETERS (brief 12 §0) --
  but the scale is a property of the *lens*, not of the parameters chosen later, so it
  is available in time. Pass ``optics=`` and rows carry the fraction of frame the
  object fills and the seconds before a reframe is needed. Without it (no rig profile
  yet) those columns are absent and the size verdict falls back to a plain arcminute
  threshold, which the UI says out loud rather than presenting as measured.

Absent data is not zero. Magnitude is missing for many rows and surface brightness
for a quarter of them; an object with no magnitude is excluded *by a magnitude filter*
and reported as such, never silently treated as infinitely faint.

Magnitude here is the *approximate visual* one (``Target.approx_magnitude``), not the
raw catalogue band, so one cut means the same thing for a V-measured row and a B-only
one. Filtering and displaying the same number is what keeps the column honest.

The work is split in two on purpose:

* :func:`compute_visibility` is the expensive part -- one vectorised pass over the
  catalogue and every sampled time step.
* :func:`filter_rows` is cheap, and is what a UI calls when a slider moves.

Without that split, dragging a magnitude slider would re-run thirteen thousand sky
positions per frame.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from ministar import sky
from ministar.catalog import Target
from ministar.optics import DEFAULT_MIN_WINDOW_S, TINY_PX, Optics
from ministar.site import Site

#: Above this, an extended object is a hard ask from a suburban sky (brief 10 §1.2).
FAINT_SURFACE_BRIGHTNESS = 22.5

#: Fallback dot threshold, used only when no :class:`Optics` is known. Superseded by
#: :data:`ministar.optics.TINY_PX`, because 2 arcmin is 7 px at 50 mm and 54 px at
#: 400 mm -- wrong at one end or the other whatever lens is fitted.
TINY_ARCMIN_FALLBACK = 2.0

#: "Huge" no longer has a threshold of its own: it is ``fill > 1``, i.e. bigger than the
#: frame that is actually fitted, which needs no constant at all.


@dataclass(frozen=True)
class Filters:
    """What the user asked to see. ``None`` means "no constraint"."""

    max_magnitude: float | None = None
    max_surface_brightness: float | None = None
    min_size_arcmin: float | None = None
    max_size_arcmin: float | None = None
    types: frozenset[str] | None = None
    search: str = ""
    min_visible_minutes: float = 0.0
    min_altitude_deg: float = 25.0
    #: Keep only rows that allow at least this long between reframes. 0 disables it,
    #: and it is ignored when no optics are known (nothing to compute it from).
    min_window_seconds: float = 0.0


@dataclass(frozen=True)
class Row:
    """One table row: the target plus everything computed for tonight."""

    target: Target
    #: Approximate visual magnitude (``Target.approx_magnitude``); the band is
    #: already folded in, so no row is 0.7 mag off its neighbour's scale.
    magnitude: float | None
    size_arcmin: float | None
    max_altitude_deg: float
    visible_minutes: float
    verdict: str
    rank: int
    #: Fraction of the frame the object takes, best orientation. ``None`` without optics.
    fill: float | None = None
    #: Seconds before it would leave the frame, drift along the long axis. ``None``
    #: without optics; ``inf`` near the pole, where it does not move.
    window_seconds: float | None = None
    #: Pixels across the object's long axis. ``None`` without optics.
    size_px: float | None = None


@dataclass(frozen=True)
class Visibility:
    """Precomputed sky figures for every target, aligned with the input order."""

    minutes: np.ndarray
    max_altitude: np.ndarray

    def __len__(self) -> int:
        return int(self.minutes.shape[0])


def verdict_for(
    *,
    visible_minutes: float,
    max_altitude_deg: float,
    size_arcmin: float | None = None,
    surface_brightness: float | None = None,
    magnitude: float | None = None,
    fill: float | None = None,
    window_seconds: float | None = None,
    size_px: float | None = None,
    min_window_seconds: float = DEFAULT_MIN_WINDOW_S,
) -> str:
    """Plain-language honesty rather than a pass/fail badge (brief 10 §1.2).

    A beginner learns nothing from a hidden object and a lot from one labelled *this
    will be a smudge*. With optics the judgement is frame-relative -- the same object
    is comfortable at 50 mm and impossible at 400 mm -- and the numbers behind it
    travel on the row, so the table can show 54 s where this says "short reframe
    window" without the two disagreeing.
    """
    if visible_minutes <= 0.0:
        return "not up tonight"
    if max_altitude_deg < 25.0:
        return "grazes the horizon"

    if fill is not None and fill > 1.0:
        return "bigger than your frame"
    if size_px is not None:
        if size_px < TINY_PX:
            return "tiny, will be a dot"
    elif size_arcmin is not None and size_arcmin < TINY_ARCMIN_FALLBACK:
        # No plate scale: the old fixed threshold, and the UI says it is a fallback.
        return "tiny, will be a dot"

    if window_seconds is not None and window_seconds < min_window_seconds:
        return "short reframe window"
    if surface_brightness is not None and surface_brightness > FAINT_SURFACE_BRIGHTNESS:
        return "faint surface brightness"
    if magnitude is None and surface_brightness is None:
        return "brightness unknown"
    return "good candidate"


#: Lower sorts first when truncating. Ordering only decides which rows survive the
#: row cap; the user can re-sort the table freely afterwards.
_VERDICT_RANK = {
    "good candidate": 0,
    "tiny, will be a dot": 1,
    "short reframe window": 2,
    "faint surface brightness": 3,
    "bigger than your frame": 4,
    "brightness unknown": 5,
    "grazes the horizon": 6,
    "not up tonight": 7,
}


def compute_visibility(
    targets: list[Target] | tuple[Target, ...],
    *,
    site: Site,
    start: datetime,
    end: datetime,
    min_altitude_deg: float = 25.0,
) -> Visibility:
    """The expensive pass: altitude figures for the whole catalogue.

    Raises rather than guessing when the site is incomplete, because an unset latitude
    has no defensible substitute.
    """
    if not site.complete:
        raise ValueError("a complete site (latitude and longitude) is required")
    if not targets:
        empty = np.zeros(0, dtype=float)
        return Visibility(minutes=empty, max_altitude=empty.copy())

    ra = np.array([target.ra_deg for target in targets], dtype=float)
    dec = np.array([target.dec_deg for target in targets], dtype=float)

    max_altitude = np.atleast_1d(
        sky.max_altitude_deg(dec, latitude_deg=site.latitude_deg)  # type: ignore[arg-type]
    )
    minutes = np.atleast_1d(
        sky.visible_minutes(
            ra,
            dec,
            latitude_deg=site.latitude_deg,  # type: ignore[arg-type]
            longitude_deg=site.longitude_deg,  # type: ignore[arg-type]
            start=start,
            end=end,
            min_alt_deg=min_altitude_deg,
        )
    )
    return Visibility(minutes=minutes, max_altitude=max_altitude)


def filter_rows(
    targets: list[Target] | tuple[Target, ...],
    visibility: Visibility,
    filters: Filters | None = None,
    *,
    optics: Optics | None = None,
) -> list[Row]:
    """Apply the user's filters to precomputed visibility. Cheap enough to re-run.

    ``optics`` turns arcminutes into frame fractions, pixels and seconds. It is
    optional so the picker still works before a rig profile exists; without it the
    frame-relative columns are simply absent rather than filled with guesses.
    """
    spec = filters or Filters()
    if len(visibility) != len(targets):
        raise ValueError("visibility does not match the target list")

    rows: list[Row] = []
    needle = spec.search.strip().lower()
    limit = spec.max_magnitude

    for index, target in enumerate(targets):
        magnitude = target.approx_magnitude
        size = target.size_arcmin

        # No magnitude means it cannot satisfy "brighter than X", so it is excluded
        # rather than treated as infinitely faint. The UI reports how many.
        if limit is not None and (magnitude is None or magnitude > limit):
            continue
        if (
            spec.max_surface_brightness is not None
            and target.surface_brightness is not None
            and target.surface_brightness > spec.max_surface_brightness
        ):
            continue
        if spec.min_size_arcmin is not None and (size is None or size < spec.min_size_arcmin):
            continue
        if spec.max_size_arcmin is not None and (size is None or size > spec.max_size_arcmin):
            continue
        if spec.types is not None and target.type not in spec.types:
            continue
        if needle and needle not in target.name.lower() and needle not in target.label.lower():
            continue
        minutes = float(visibility.minutes[index])
        if minutes < spec.min_visible_minutes:
            continue

        # Frame-relative figures, only when both a rig and a size are known. A missing
        # minor axis is left to Optics.fill(), which assumes circular (conservative).
        fill: float | None = None
        window: float | None = None
        size_px: float | None = None
        if optics is not None:
            major = target.major_arcmin if target.major_arcmin is not None else size
            if major is not None:
                fill = optics.fill(major, target.minor_arcmin)
                size_px = optics.object_size_px(major)
                window = optics.window_seconds(fill=fill, dec_deg=target.dec_deg)
        if (
            spec.min_window_seconds > 0.0
            and window is not None
            and window < spec.min_window_seconds
        ):
            continue

        verdict = verdict_for(
            visible_minutes=minutes,
            max_altitude_deg=float(visibility.max_altitude[index]),
            size_arcmin=size,
            surface_brightness=target.surface_brightness,
            magnitude=magnitude,
            fill=fill,
            window_seconds=window,
            size_px=size_px,
            min_window_seconds=(
                spec.min_window_seconds if spec.min_window_seconds > 0 else DEFAULT_MIN_WINDOW_S
            ),
        )
        rows.append(
            Row(
                target=target,
                magnitude=magnitude,
                size_arcmin=size,
                max_altitude_deg=float(visibility.max_altitude[index]),
                visible_minutes=minutes,
                verdict=verdict,
                rank=_VERDICT_RANK.get(verdict, 9),
                fill=fill,
                window_seconds=window,
                size_px=size_px,
            )
        )
    return rows


def build_rows(
    targets: list[Target] | tuple[Target, ...],
    *,
    site: Site,
    start: datetime,
    end: datetime,
    filters: Filters | None = None,
    optics: Optics | None = None,
) -> list[Row]:
    """Convenience wrapper: compute visibility, then filter. One night, one call."""
    spec = filters or Filters()
    visibility = compute_visibility(
        targets, site=site, start=start, end=end, min_altitude_deg=spec.min_altitude_deg
    )
    return filter_rows(targets, visibility, spec, optics=optics)


def top(rows: list[Row], limit: int) -> list[Row]:
    """The best ``limit`` rows, for truncating what reaches the browser.

    Ranking is a truncation order, not a judgement: best verdict first, then
    brightest, then the longest visibility window.
    """
    return sorted(
        rows,
        key=lambda row: (
            row.rank,
            row.magnitude if row.magnitude is not None else 99.0,
            -row.visible_minutes,
        ),
    )[:limit]


__all__ = [
    "FAINT_SURFACE_BRIGHTNESS",
    "TINY_ARCMIN_FALLBACK",
    "Filters",
    "Row",
    "Visibility",
    "build_rows",
    "compute_visibility",
    "filter_rows",
    "top",
    "verdict_for",
]
