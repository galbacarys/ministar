"""Filtering and ranking the catalogue for tonight.

Pure logic with no UI, so the table's numbers can be tested directly. The UI only
draws what this returns.

Two deliberate limits, both from brief 10 §1.2:

* **These are advisory.** A filter hides rows from the current view; it never claims
  an object is unobservable. Anything filtered out can be shown again by loosening a
  control.
* **No frame-relative verdicts yet.** "Bigger than your frame" needs a focal length,
  and TARGET runs *before* PARAMETERS in the wizard order (brief 12 §0), so it is not
  known here. Judging size in arcminutes only avoids inventing an ordering-dependent
  answer; the frame fit is evaluated later, where it belongs.

Absent data is not zero. Magnitude is missing for many rows and surface brightness
for a quarter of them; an object with no magnitude is excluded *by a magnitude filter*
and reported as such, never silently treated as infinitely faint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from ministar import sky
from ministar.catalog import Target
from ministar.site import Site

#: Above this, an extended object is a hard ask from a suburban sky (brief 10 §1.2).
FAINT_SURFACE_BRIGHTNESS = 22.5

#: Below this the object is a few pixels across on this rig and reads as a dot.
TINY_ARCMIN = 2.0

#: Above this it will not fit a single frame at any of our useful focal lengths.
HUGE_ARCMIN = 100.0


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


@dataclass(frozen=True)
class Row:
    """One table row: the target plus everything computed for tonight."""

    target: Target
    magnitude: float | None
    magnitude_band: str
    size_arcmin: float | None
    max_altitude_deg: float
    visible_minutes: float
    verdict: str
    rank: int


def verdict_for(
    *,
    visible_minutes: float,
    max_altitude_deg: float,
    size_arcmin: float | None = None,
    surface_brightness: float | None = None,
    magnitude: float | None = None,
) -> str:
    """Plain-language honesty rather than a pass/fail badge (brief 10 §1.2).

    A beginner learns nothing from a hidden object and a lot from one labelled *this
    will be a smudge*.
    """
    if visible_minutes <= 0.0:
        return "not up tonight"
    if max_altitude_deg < 25.0:
        return "grazes the horizon"
    if size_arcmin is not None and size_arcmin > HUGE_ARCMIN:
        return "huge, will not fit one frame"
    if size_arcmin is not None and size_arcmin < TINY_ARCMIN:
        return "tiny, will be a dot"
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
    "faint surface brightness": 2,
    "huge, will not fit one frame": 3,
    "brightness unknown": 4,
    "grazes the horizon": 5,
    "not up tonight": 6,
}


def build_rows(
    targets: list[Target] | tuple[Target, ...],
    *,
    site: Site,
    start: datetime,
    end: datetime,
    filters: Filters | None = None,
) -> list[Row]:
    """Compute every row for tonight, then apply ``filters``.

    Visibility is computed for the whole catalogue in one vectorised pass, because
    the alternative is 13 000 Python loops. The site must be complete; an incomplete
    site raises rather than quietly producing unranked numbers.
    """
    if not site.complete:
        raise ValueError("a complete site (latitude and longitude) is required")

    spec = filters or Filters()
    if not targets:
        return []

    ra = np.array([t.ra_deg for t in targets], dtype=float)
    dec = np.array([t.dec_deg for t in targets], dtype=float)

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
            min_alt_deg=spec.min_altitude_deg,
        )
    )

    rows: list[Row] = []
    needle = spec.search.strip().lower()
    for index, target in enumerate(targets):
        magnitude = target.magnitude
        size = target.size_arcmin

        # No magnitude means it cannot satisfy "brighter than X", so it is excluded
        # rather than treated as infinitely faint. The UI reports how many.
        limit = spec.max_magnitude
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
        if float(minutes[index]) < spec.min_visible_minutes:
            continue

        verdict = verdict_for(
            visible_minutes=float(minutes[index]),
            max_altitude_deg=float(max_altitude[index]),
            size_arcmin=size,
            surface_brightness=target.surface_brightness,
            magnitude=magnitude,
        )
        rows.append(
            Row(
                target=target,
                magnitude=magnitude,
                magnitude_band=target.magnitude_band,
                size_arcmin=size,
                max_altitude_deg=float(max_altitude[index]),
                visible_minutes=float(minutes[index]),
                verdict=verdict,
                rank=_VERDICT_RANK.get(verdict, 9),
            )
        )

    return rows


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
    "HUGE_ARCMIN",
    "TINY_ARCMIN",
    "Filters",
    "Row",
    "build_rows",
    "top",
    "verdict_for",
]
