"""The observing site: latitude and longitude, supplied by the user.

**Nothing here has a default.** There is no built-in latitude, and an incomplete
site stays incomplete rather than falling back to something plausible. A wrong
latitude silently changes how long a target is up; a wrong longitude shifts the
whole night window by hours. Either one is indistinguishable from a correct answer
once it has been baked into a table, so the only safe representation of "unknown" is
"unknown".

This is a PREFLIGHT concern rather than a TARGET concern: discovering at a dark site
that no coordinates are set is exactly the thing preflight is meant to catch before
you drive there (brief 12 §1). The target screen reads the site; it does not own it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: File name inside ``Paths.data``.
SITE_FILENAME = "site.json"


@dataclass(frozen=True)
class Site:
    """An observing location. ``None`` means genuinely unknown."""

    latitude_deg: float | None = None
    longitude_deg: float | None = None
    name: str = ""

    @property
    def complete(self) -> bool:
        """True only when both coordinates are known."""
        return self.latitude_deg is not None and self.longitude_deg is not None

    def describe(self) -> str:
        if not self.complete:
            missing = []
            if self.latitude_deg is None:
                missing.append("latitude")
            if self.longitude_deg is None:
                missing.append("longitude")
            return "site not set (missing " + " and ".join(missing) + ")"
        where = f"{self.name}: " if self.name else ""
        return f"{where}{self.latitude_deg:+.3f}, {self.longitude_deg:+.3f}"

    def as_dict(self) -> dict[str, object]:
        return {
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "name": self.name,
        }


def validate(latitude: str | float | None, longitude: str | float | None) -> str | None:
    """Return an error message, or ``None`` when the pair is acceptable.

    Accepts strings so the UI can validate exactly what the user typed.
    """
    values = {}
    for label, raw in (("latitude", latitude), ("longitude", longitude)):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        try:
            values[label] = float(raw)
        except (TypeError, ValueError):
            return f"{label} is not a number: {raw!r}"

    if "latitude" in values and not -90.0 <= values["latitude"] <= 90.0:
        return "latitude must be between -90 and +90"
    if "longitude" in values and not -180.0 <= values["longitude"] <= 180.0:
        return "longitude must be between -180 and +180"
    return None


def load(path: Path) -> Site:
    """Read a site file. A missing or unreadable file yields an empty site."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Site()

    def number(key: str) -> float | None:
        value = data.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    site = Site(
        latitude_deg=number("latitude_deg"),
        longitude_deg=number("longitude_deg"),
        name=str(data.get("name") or ""),
    )
    # Never surface a half-valid site from a hand-edited file.
    if validate(site.latitude_deg, site.longitude_deg) is not None:
        return Site()
    return site


def save(path: Path, site: Site) -> None:
    """Write the site atomically, so a crash cannot leave a truncated file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(site.as_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def site_path(data_directory: Path) -> Path:
    """Where the site lives, given a ``Paths.data`` directory."""
    return Path(data_directory) / SITE_FILENAME


__all__ = ["SITE_FILENAME", "Site", "load", "save", "site_path", "validate"]
