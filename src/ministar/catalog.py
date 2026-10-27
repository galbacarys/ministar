"""The deep-sky target catalogue, read from OpenNGC's CSV.

Reads the vendored CSV **directly** -- no build step, no derived JSON. Measured at
59 ms for 14 034 rows, so a conversion would buy a maintained artifact in exchange
for nothing, and a modified copy would additionally become a CC-BY-SA derivative
requiring a "changes were made" notice.

Data quirks that matter (see ``third_party/openngc/README.md``):

* ``Type`` includes artefacts, not only object types. ``Dup`` (652 rows) duplicates
  and ``NonEx`` (10) non-existent objects are dropped here; everything else is kept
  and left to the type filter, because ``inform, never block``.
* The 10 ``NonEx`` rows are exactly the ones with empty ``RA``/``Dec``, so dropping
  them also fixes coordinate parsing rather than needing a special case.
* ``V-Mag`` exists for only 30% of rows, ``B-Mag`` for 81%. :attr:`Target.magnitude`
  is the raw catalogue value in whichever band exists, and
  :attr:`Target.approx_magnitude` puts both onto one approximate visual scale, so a
  magnitude filter cannot lie by ~0.7 mag. Anything a user sees or filters on uses
  the latter.
* A missing size or surface brightness is *legitimate*, not zero.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Vendored data, resolved relative to the repository root. Override with
#: ``MINISTAR_CATALOG`` when running from a packaged build, where this path will
#: not exist (a packaging task, not a runtime one).
CATALOG_DIR = Path(__file__).resolve().parents[2] / "third_party" / "openngc"

#: Classes that make sense as *imaging targets*, used as the default filter.
#:
#: The catalogue contains 547 individual stars (`*`), so a magnitude-sorted list
#: opens with Sirius, Rigel and Alnilam. Those are useful to a focus routine and
#: useless as targets, so they are filtered out by default -- still selectable, per
#: "inform, never block". Stellar associations and asterisms are kept because they
#: are legitimate wide-field subjects (M24 is an `*Ass`).
DEFAULT_TARGET_TYPES = (
    "G",
    "GPair",
    "GTrpl",
    "GGroup",
    "OCl",
    "GCl",
    "Cl+N",
    "PN",
    "HII",
    "SNR",
    "Neb",
    "RfN",
    "EmN",
    "DrkN",
    "Ass",
    "*Ass",
)

#: Human-readable names for OpenNGC's type codes.
#:
#: The raw codes are terse to the point of being unreadable in a list ('G', 'Cl+N',
#: '*Ass'). A picker that says "Galaxy" is the difference between someone using the
#: filter and abandoning it.
TYPE_LABELS = {
    "G": "Galaxy",
    "GPair": "Galaxy pair",
    "GTrpl": "Galaxy triplet",
    "GGroup": "Galaxy group",
    "OCl": "Open cluster",
    "GCl": "Globular cluster",
    "Cl+N": "Cluster + nebulosity",
    "PN": "Planetary nebula",
    "HII": "HII region",
    "SNR": "Supernova remnant",
    "Neb": "Nebula",
    "RfN": "Reflection nebula",
    "EmN": "Emission nebula",
    "DrkN": "Dark nebula",
    "Ass": "Stellar association",
    "*Ass": "Asterism",
    "*": "Star",
}

#: Artefacts rather than objects.
EXCLUDED_TYPES = frozenset({"Dup", "NonEx"})

#: Shift from the blue band to the visual band, in magnitudes.
#:
#: Magnitudes are measured through a colour filter, so the same object has a different
#: number in each band: over the 4 072 rows that carry both, the median ``B - V`` is
#: 0.71 mag (mean 0.64), i.e. a B-only row reads about 0.7 mag fainter than the same
#: object would in V. Subtracting this puts a B-only row on an approximate V scale so
#: one filter cut means the same thing for every row.
#:
#: It is a *typical* colour, not a measurement: an individual galaxy can sit half a
#: magnitude either side. That is why the UI calls the result "approx. mag" and says
#: so on hover, rather than presenting a converted number as catalogue data.
BLUE_TO_VISUAL_MAG = 0.71

#: Object classes offered as filters. Everything present in the file is listed so
#: the UI can show the real set rather than a guess.
COMMON_TYPES = (
    "G",
    "GPair",
    "GTrpl",
    "GGroup",
    "OCl",
    "GCl",
    "Cl+N",
    "PN",
    "HII",
    "SNR",
    "Neb",
    "RfN",
    "EmN",
    "DrkN",
    "Ass",
    "*Ass",
    "*",
)


@dataclass(frozen=True)
class Target:
    """One catalogue row, in the units the rest of the app wants."""

    name: str
    type: str
    ra_deg: float
    dec_deg: float
    constellation: str = ""
    major_arcmin: float | None = None
    minor_arcmin: float | None = None
    b_mag: float | None = None
    v_mag: float | None = None
    surface_brightness: float | None = None
    messier: int | None = None
    common_name: str = ""

    @property
    def magnitude(self) -> float | None:
        """The raw catalogue value: V if available, else B.

        Mixes two scales, so prefer :attr:`approx_magnitude` for anything a user
        reads or filters on. :attr:`magnitude_band` says which scale this is.
        """
        return self.v_mag if self.v_mag is not None else self.b_mag

    @property
    def approx_magnitude(self) -> float | None:
        """One approximate visual magnitude, whichever band the catalogue recorded.

        V is used as measured; a B-only row is corrected by
        :data:`BLUE_TO_VISUAL_MAG`. This is the number the target table shows and the
        magnitude filter cuts on, so the two can never disagree.
        """
        if self.v_mag is not None:
            return self.v_mag
        if self.b_mag is not None:
            return round(self.b_mag - BLUE_TO_VISUAL_MAG, 2)
        return None

    @property
    def magnitude_band(self) -> str:
        if self.v_mag is not None:
            return "V"
        if self.b_mag is not None:
            return "B"
        return ""

    @property
    def size_arcmin(self) -> float | None:
        """Major axis if known, else minor, else ``None``."""
        return self.major_arcmin if self.major_arcmin is not None else self.minor_arcmin

    @property
    def label(self) -> str:
        """How to show it: Messier number first, because that is what people know."""
        parts = []
        if self.messier is not None:
            parts.append(f"M{self.messier}")
        parts.append(self.name)
        if self.common_name:
            parts.append(self.common_name)
        return "  ".join(parts)


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    number = _float(value)
    return None if number is None else int(number)


def parse_ra(text: str) -> float:
    """``'00:08:27.05'`` (hours) to degrees."""
    hours, minutes, seconds = (float(part) for part in text.strip().split(":"))
    return (hours + minutes / 60.0 + seconds / 3600.0) * 15.0


def parse_dec(text: str) -> float:
    """``'+27:43:03.6'`` to degrees."""
    stripped = text.strip()
    sign = -1.0 if stripped.startswith("-") else 1.0
    degrees, minutes, seconds = (float(part) for part in stripped.lstrip("+-").split(":"))
    return sign * (degrees + minutes / 60.0 + seconds / 3600.0)


def _row_to_target(row: dict[str, str]) -> Target | None:
    name = (row.get("Name") or "").strip()
    obj_type = (row.get("Type") or "").strip()
    if not name or obj_type in EXCLUDED_TYPES:
        return None
    try:
        ra = parse_ra(row.get("RA") or "")
        dec = parse_dec(row.get("Dec") or "")
    except (AttributeError, ValueError):
        return None  # unparseable coordinates; skip rather than crash

    return Target(
        name=name,
        type=obj_type,
        ra_deg=ra,
        dec_deg=dec,
        constellation=(row.get("Const") or "").strip(),
        major_arcmin=_float(row.get("MajAx")),
        minor_arcmin=_float(row.get("MinAx")),
        b_mag=_float(row.get("B-Mag")),
        v_mag=_float(row.get("V-Mag")),
        surface_brightness=_float(row.get("SurfBr")),
        messier=_int(row.get("M")),
        common_name=(row.get("Common names") or "").strip(),
    )


def load_catalog(directory: Path | None = None) -> list[Target]:
    """Read both CSVs and return every object that is not a data artefact."""
    source = directory or Path(os.environ.get("MINISTAR_CATALOG", CATALOG_DIR))
    targets: list[Target] = []
    for filename in ("NGC.csv", "addendum.csv"):
        path = source / filename
        if not path.is_file():
            continue
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter=";"):
                target = _row_to_target(row)
                if target is not None:
                    targets.append(target)
    return targets


@lru_cache(maxsize=1)
def catalog() -> tuple[Target, ...]:
    """The catalogue, loaded once per process."""
    return tuple(load_catalog())


__all__ = [
    "BLUE_TO_VISUAL_MAG",
    "CATALOG_DIR",
    "COMMON_TYPES",
    "DEFAULT_TARGET_TYPES",
    "EXCLUDED_TYPES",
    "TYPE_LABELS",
    "Target",
    "catalog",
    "load_catalog",
    "parse_dec",
    "parse_ra",
]
