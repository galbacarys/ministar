"""Real star positions, read from ASTAP's own database.

The fake telescope should show the sky a camera would see, not random dots: then a
synthetic frame can be handed to ASTAP and the *solver* verifies our pointing and roll,
which is the only way to check those before a clear night. Rendering from ASTAP's own
catalogue makes that check self-consistent -- the stars in the frame are the stars ASTAP
will match against.

Format (ASTAP manual, appendix 3, confirmed against the installed D80 tiles)
--------------------------------------------------------------------------
Files are ``d80_<ring><cell>.1476``. The first 109 bytes are an ASCII description and
byte 109 (0-based) is the record size, which is 5 for the D80/G05/D05/D20/D50 families:

* A **star record** is ``ra7, ra8, ra9, dec7, dec8`` -- little-endian byte pairs of
  24-bit RA and of the low 16 bits of Dec.
* A **header record** marks a section: ``FF FF FF dec9 magnitude``. ``FF FF FF`` cannot be
  a real RA, so it is unambiguous. ``dec9`` is the high byte of Dec (offset by -128) and
  ``magnitude`` is ``(value - 16) / 10``, so both are shared by every star in the section
  -- which is why the format is 5 bytes rather than 7.

Decoding::

    ra_deg = (ra7 + 256*ra8 + 65536*ra9) * 360 / (256**3 - 1)
    dec    = (dec7 + 256*dec8 + 65536*(dec9 - 128)) * 90 / (128*256**2 - 1)
    mag    = (magnitude_byte - 16) / 10

Verified twice: the manual's worked example for Sirius (``C3 06 48`` / ``D7 39 E8``)
decodes to 6.75247662 h and -16.7161401 deg, and the star found in the real tile nearest
that position sits 33.8 arcsec away from J2000 Sirius -- which is its proper motion to the
database's epoch 2025, not an error. The first tile's declinations land exactly inside the
documented ring 0-1 bounds (-90.000 to -87.429).

Positions are **epoch 2025**, not J2000. For rendering a frame that is irrelevant (the
offset is under an arcminute), but do not compare these numbers with J2000 catalogues and
conclude the decoder is wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: ASCII preamble length; byte 109 holds the record size.
HEADER_BYTES = 109
RECORD_5 = 5
#: Dec is shared with the section header, which stores it offset by this much.
DEC9_OFFSET = 128
#: Magnitude is stored as tenths, offset so it stays positive.
MAG_OFFSET = 16.0

_RA_SCALE = 360.0 / (256**3 - 1)
_DEC_SCALE = 90.0 / (128 * 256**2 - 1)

#: Index cache, so the 1.3 GB of tiles is walked once rather than per query.
INDEX_FILENAME = "star-index.json"


@dataclass(frozen=True)
class Stars:
    """A set of catalogue stars, in degrees and magnitudes."""

    ra_deg: np.ndarray
    dec_deg: np.ndarray
    mag: np.ndarray

    def __len__(self) -> int:
        return int(self.ra_deg.shape[0])

    def brighter_than(self, limit: float) -> Stars:
        keep = self.mag <= limit
        return Stars(self.ra_deg[keep], self.dec_deg[keep], self.mag[keep])


@dataclass(frozen=True)
class Tile:
    """One database file and the part of the sky it covers."""

    path: Path
    ra_min: float
    ra_max: float
    dec_min: float
    dec_max: float
    count: int

    def contains(self, ra_deg: float, dec_deg: float, margin_deg: float = 0.0) -> bool:
        if not self.dec_min - margin_deg <= dec_deg <= self.dec_max + margin_deg:
            return False
        width = self.ra_max - self.ra_min
        # A tile may straddle RA 0; compare through the shorter way round in that case.
        if width >= 359.0:
            return True
        offset = (ra_deg - self.ra_min) % 360.0
        return offset <= width + margin_deg or offset >= 360.0 - margin_deg

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path.name,
            "ra_min": self.ra_min,
            "ra_max": self.ra_max,
            "dec_min": self.dec_min,
            "dec_max": self.dec_max,
            "count": self.count,
        }


def read_tile(path: Path | str) -> Stars:
    """Decode one ``.1476`` file. Vectorised: ~20 ms for 166 000 stars."""
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size < HEADER_BYTES + RECORD_5:
        raise ValueError(f"{path}: too short to be a star database tile")

    record_size = int(raw[HEADER_BYTES])
    if record_size != RECORD_5:
        raise ValueError(
            f"{path}: record size {record_size}; only the 5-byte 1476 format is supported "
            "(the 6-byte V50 files carry an extra colour index)"
        )

    records = raw[HEADER_BYTES + 1 :].reshape(-1, record_size)
    ra7, ra8, ra9 = (records[:, i].astype(np.uint32) for i in range(3))
    dec7, dec8 = (records[:, i].astype(np.int64) for i in (3, 4))

    # Sections: every star inherits the dec9 and magnitude of the header before it.
    header = (ra7 == 0xFF) & (ra8 == 0xFF) & (ra9 == 0xFF)
    section = np.where(header, np.arange(records.shape[0]), 0)
    np.maximum.accumulate(section, out=section)
    dec9 = records[section, 3].astype(np.int64) - DEC9_OFFSET
    mag = (records[section, 4].astype(np.float64) - MAG_OFFSET) / 10.0

    # The manual's RA relation yields *hours*; multiply by 15 for degrees, which cancels
    # the 24 exactly -- so the scale below is degrees directly.
    ra_deg = (ra7 + ra8 * 256 + ra9 * 65536).astype(np.float64) * _RA_SCALE
    dec_deg = (dec7 + dec8 * 256 + dec9 * 65536).astype(np.float64) * _DEC_SCALE

    keep = ~header
    return Stars(ra_deg[keep] % 360.0, dec_deg[keep], mag[keep])


def _index_one(path: Path) -> Tile:
    stars = read_tile(path)
    if len(stars) == 0:
        return Tile(path, 0.0, 360.0, 0.0, 0.0, 0)
    return Tile(
        path=path,
        ra_min=float(stars.ra_deg.min()),
        ra_max=float(stars.ra_deg.max()),
        dec_min=float(stars.dec_deg.min()),
        dec_max=float(stars.dec_deg.max()),
        count=len(stars),
    )


@dataclass(frozen=True)
class StarCatalog:
    """A directory of ASTAP tiles, indexed by the sky each one actually covers."""

    directory: Path
    tiles: tuple[Tile, ...]

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, directory: Path | str, *, cache: Path | None = None) -> StarCatalog:
        """Index the tiles in ``directory``, using ``cache`` when it is still valid.

        The index is built from the tile *contents* rather than from their names: the
        naming scheme (rings of 5.142857 deg, RA cells per ring) is documented but
        fiddly, and a bounding box measured from the stars cannot disagree with the data
        it describes.
        """
        folder = Path(directory)
        paths = sorted(folder.glob("*.1476"))
        if not paths:
            raise FileNotFoundError(f"no .1476 tiles in {folder}")
        if cache is not None and cache.is_file():
            cached = _read_cache(cache, folder)
            if cached is not None:
                return cls(directory=folder, tiles=cached)
        tiles = tuple(_index_one(path) for path in paths)
        if cache is not None:
            _write_cache(cache, folder, tiles)
        return cls(directory=folder, tiles=tiles)

    # -- queries ------------------------------------------------------------
    def tiles_near(self, ra_deg: float, dec_deg: float, radius_deg: float = 0.0) -> list[Tile]:
        margin = max(0.0, radius_deg)
        return [tile for tile in self.tiles if tile.contains(ra_deg, dec_deg, margin)]

    def stars_near(
        self, ra_deg: float, dec_deg: float, radius_deg: float, *, mag_limit: float = 12.0
    ) -> Stars:
        """Every catalogue star within ``radius_deg`` of a point, brighter than ``mag_limit``.

        Reads only the tiles the region touches, which for a camera field is one or two.
        """
        parts: list[Stars] = []
        for tile in self.tiles_near(ra_deg, dec_deg, radius_deg):
            stars = read_tile(tile.path)
            # True angular separation, so a wide RA cell cannot smuggle in far stars.
            cos_dec = np.cos(np.radians(stars.dec_deg))
            dra = (stars.ra_deg - ra_deg + 180.0) % 360.0 - 180.0
            sep = np.hypot(dra * cos_dec, stars.dec_deg - dec_deg)
            keep = (sep <= radius_deg) & (stars.mag <= mag_limit)
            parts.append(Stars(stars.ra_deg[keep], stars.dec_deg[keep], stars.mag[keep]))
        if not parts:
            empty = np.zeros(0)
            return Stars(empty, empty.copy(), empty.copy())
        return Stars(
            np.concatenate([part.ra_deg for part in parts]),
            np.concatenate([part.dec_deg for part in parts]),
            np.concatenate([part.mag for part in parts]),
        )


def _read_cache(path: Path, folder: Path) -> tuple[Tile, ...] | None:
    """The cached index, if it describes exactly the tiles currently on disk."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("directory") != str(folder):
        return None
    names = sorted(name.name for name in folder.glob("*.1476"))
    if data.get("files") != names:
        return None
    tiles = []
    for entry in data.get("tiles", []):
        try:
            tiles.append(
                Tile(
                    path=folder / str(entry["path"]),
                    ra_min=float(entry["ra_min"]),
                    ra_max=float(entry["ra_max"]),
                    dec_min=float(entry["dec_min"]),
                    dec_max=float(entry["dec_max"]),
                    count=int(entry["count"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return tuple(tiles)


def _write_cache(path: Path, folder: Path, tiles: tuple[Tile, ...]) -> None:
    payload = {
        "directory": str(folder),
        "files": sorted(tile.path.name for tile in tiles),
        "tiles": [tile.as_dict() for tile in tiles],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def find_catalog(settings, *, cache: Path | None = None) -> StarCatalog | None:
    """The database beside ASTAP, if it is installed and readable."""
    astap = getattr(settings, "astap", None)
    if astap is None:
        return None
    try:
        return StarCatalog.load(astap.parent, cache=cache)
    except (OSError, ValueError):
        return None


__all__ = [
    "HEADER_BYTES",
    "INDEX_FILENAME",
    "RECORD_5",
    "StarCatalog",
    "Stars",
    "Tile",
    "find_catalog",
    "read_tile",
]
