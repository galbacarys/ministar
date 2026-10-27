"""Spherical astronomy for planning: where a target is, and for how long.

Deliberately small and dependency-light (numpy only) because it is used to fill a
table of 13 000 objects, so these functions are **vectorised**: pass arrays and get
arrays back. Scalars work too.

Two different questions live here and they need different information:

* **How high does it get, and for how long per night?** Depends only on declination
  and site *latitude*. No clock, no longitude.
* **Is it up tonight, and when?** Depends on local sidereal time, so it needs
  **longitude** and a real date. Guessing a longitude silently shifts the answer by
  up to hours, which is why the UI asks for one rather than defaulting.

**No site is assumed anywhere in this module.** Latitude and longitude are required
arguments on every function that needs them, with no defaults and no built-in
location. That is deliberate: the observing site is supplied by the user, and a
wrong latitude silently changes hours-above and a wrong longitude shifts the whole
night window by hours. A plausible default would be indistinguishable from a correct
answer, which is the worst failure mode there is.

Accuracy is more than enough for session planning: mean sidereal time, no nutation
or aberration, geometric altitude (no refraction). Refraction matters near the
horizon, but every threshold we care about starts at 25 degrees.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np

#: J2000.0 epoch as a Julian Date.
J2000 = 2451545.0


def julian_date(when: datetime) -> float:
    """Julian Date for an aware datetime (converted to UTC first)."""
    utc = when.astimezone(UTC)
    year, month = utc.year, utc.month
    day = utc.day + (utc.hour + utc.minute / 60 + utc.second / 3600) / 24
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + b - 1524.5


def gmst_deg(when: datetime) -> float:
    """Greenwich mean sidereal time, in degrees (Meeus, ch. 12)."""
    d = julian_date(when) - J2000
    t = d / 36525.0
    return (
        280.46061837 + 360.98564736629 * d + 0.000387933 * t * t - t * t * t / 38710000.0
    ) % 360.0


def lst_deg(when: datetime, longitude_deg: float) -> float:
    """Local mean sidereal time. East longitude positive."""
    return (gmst_deg(when) + longitude_deg) % 360.0


def altitude_deg(
    ra_deg: np.ndarray | float,
    dec_deg: np.ndarray | float,
    lst: np.ndarray | float,
    latitude_deg: float,
) -> np.ndarray | float:
    """Geometric altitude, broadcasting over hour angle.

    ``sin h = sin(lat) sin(dec) + cos(lat) cos(dec) cos(H)``, with ``H = LST - RA``.
    """
    hour_angle = np.radians(np.subtract(lst, ra_deg))
    phi = math.radians(latitude_deg)
    dec = np.radians(dec_deg)
    sin_alt = math.sin(phi) * np.sin(dec) + math.cos(phi) * np.cos(dec) * np.cos(hour_angle)
    return np.degrees(np.arcsin(np.clip(sin_alt, -1.0, 1.0)))


def max_altitude_deg(dec_deg: np.ndarray | float, latitude_deg: float) -> np.ndarray | float:
    """Culmination altitude: ``90 - |lat - dec|``. Depends on no clock."""
    return 90.0 - np.abs(latitude_deg - np.asarray(dec_deg, dtype=float))


def hours_above(
    dec_deg: np.ndarray | float,
    latitude_deg: float,
    min_alt_deg: float,
) -> np.ndarray | float:
    """Hours per sidereal day spent above ``min_alt_deg``.

    Analytic, so it is exact and free: the target is above the threshold while the
    hour angle satisfies ``|H| <= H0`` with
    ``cos H0 = (sin h0 - sin lat sin dec) / (cos lat cos dec)``.

    Returns 24 for a circumpolar target and 0 for one that never rises. Depends only
    on latitude and declination -- not on date or longitude -- so it is an annual
    figure, not "tonight".
    """
    phi = math.radians(latitude_deg)
    dec = np.radians(np.asarray(dec_deg, dtype=float))
    h0 = math.radians(min_alt_deg)

    denominator = math.cos(phi) * np.cos(dec)
    numerator = math.sin(h0) - math.sin(phi) * np.sin(dec)

    with np.errstate(divide="ignore", invalid="ignore"):
        cos_h0 = np.where(
            np.abs(denominator) < 1e-12, np.sign(numerator) * 2.0, numerator / denominator
        )

    always = cos_h0 <= -1.0
    never = cos_h0 >= 1.0
    h0_deg = np.degrees(np.arccos(np.clip(cos_h0, -1.0, 1.0)))
    hours = 2.0 * h0_deg / 15.0
    result = np.where(always, 24.0, np.where(never, 0.0, hours))
    return float(result) if np.ndim(result) == 0 else result


def night_window(
    local_now: datetime,
    *,
    start_hour: int = 20,
    end_hour: int = 4,
) -> tuple[datetime, datetime]:
    """The coming night in UTC, as ``(start, end)``.

    Defaults to 20:00-04:00 local. Deliberately not sunset-based: that needs solar
    position, and the difference at mid-latitudes is minutes, not hours. The window
    is a planning aid, so it is stated rather than derived.
    """
    local = local_now.astimezone()
    start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if start < local:
        start += timedelta(days=1)
    end = start.replace(hour=end_hour)
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def visible_minutes(
    ra_deg: np.ndarray | float,
    dec_deg: np.ndarray | float,
    *,
    latitude_deg: float,
    longitude_deg: float,
    start: datetime,
    end: datetime,
    min_alt_deg: float = 25.0,
    step_s: int = 300,
) -> np.ndarray | float:
    """Minutes spent above ``min_alt_deg`` between ``start`` and ``end``.

    Samples the window at ``step_s`` (5 min default) and broadcasts over every
    target at once, so 14 000 objects cost one vectorised pass rather than 14 000
    loops.
    """
    total = (end - start).total_seconds()
    if total <= 0:
        raise ValueError("end must be after start")
    offsets = np.arange(0.0, total, float(step_s))
    times = [start + timedelta(seconds=float(s)) for s in offsets]
    lst = np.array([lst_deg(when, longitude_deg) for when in times])

    ra = np.atleast_1d(np.asarray(ra_deg, dtype=float))
    dec = np.atleast_1d(np.asarray(dec_deg, dtype=float))
    altitudes = altitude_deg(ra[:, None], dec[:, None], lst[None, :], latitude_deg)
    above = altitudes >= min_alt_deg

    step_minutes = step_s / 60.0
    minutes = above.sum(axis=1) * step_minutes
    return float(minutes[0]) if np.isscalar(ra_deg) and np.ndim(ra_deg) == 0 else minutes


__all__ = [
    "altitude_deg",
    "gmst_deg",
    "hours_above",
    "julian_date",
    "lst_deg",
    "max_altitude_deg",
    "night_window",
    "visible_minutes",
]
