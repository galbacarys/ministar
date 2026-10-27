"""The ASTAP star database decoder.

Most of these tests build a tile byte by byte, so they run anywhere. Two more use the
real D80 files when ASTAP is installed: the manual's worked example for Sirius, and the
documented declination bounds of the polar tile. Those are the checks that caught the
format being right, so they are worth keeping even though they need the 1.3 GB database.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ministar.starcatalog import (
    HEADER_BYTES,
    StarCatalog,
    Stars,
    read_tile,
)

#: ASTAP installs its databases beside the executable. Check the Windows path and, when
#: the tests run from WSL, the mounted drive -- otherwise these checks silently skip on
#: the machine they were written for.
_ASTAP_CANDIDATES = (
    Path(r"C:\Program Files\astap"),
    Path("/mnt/c/Program Files/astap"),
)
ASTAP_DIR = next(
    (path for path in _ASTAP_CANDIDATES if (path / "d80_0101.1476").is_file()), _ASTAP_CANDIDATES[0]
)


def encode_header(dec9_raw: int, mag_raw: int) -> bytes:
    return bytes((0xFF, 0xFF, 0xFF, dec9_raw, mag_raw))


def encode_star(ra_deg: float, dec_deg: float, dec9_raw: int) -> bytes:
    """A 5-byte record, using the manual's scaling relations in reverse."""
    ra_raw = round(ra_deg / 360.0 * (256**3 - 1))
    dec9 = (dec9_raw - 128) * 65536
    low = round(dec_deg / 90.0 * (128 * 256**2 - 1)) - dec9
    return bytes(
        (
            ra_raw & 0xFF,
            (ra_raw >> 8) & 0xFF,
            (ra_raw >> 16) & 0xFF,
            low & 0xFF,
            (low >> 8) & 0xFF,
        )
    )


def build_tile(stars: list[tuple[float, float, float]], *, record_size: int = 5) -> bytes:
    """Header text + record size byte + sections of (header, stars) records."""
    preamble = b"TEST TILE".ljust(HEADER_BYTES, b" ")
    out = bytearray(preamble)
    out.append(record_size)
    # One section per distinct (dec9, mag) pair, which is how the real files are grouped.
    sections: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for ra, dec, mag in stars:
        dec9_raw = round(dec / 90.0 * (128 * 256**2 - 1)) // 65536 + 128
        mag_raw = round(mag * 10 + 16)
        sections.setdefault((dec9_raw, mag_raw), []).append((ra, dec))
    for (dec9_raw, mag_raw), members in sections.items():
        out += encode_header(dec9_raw, mag_raw)
        for ra, dec in members:
            out += encode_star(ra, dec, dec9_raw)
    return bytes(out)


def write(tmp_path: Path, data: bytes, name: str = "d80_0101.1476") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# -- decoding ---------------------------------------------------------------
def test_decodes_positions_and_magnitudes(tmp_path: Path) -> None:
    tile = write(
        tmp_path,
        build_tile(
            [
                (10.0, 40.0, 8.0),
                (10.5, 40.1, 8.0),  # same section as the first: shares mag and dec9
                (11.0, 12.0, 9.5),
            ]
        ),
    )

    stars = read_tile(tile)

    assert len(stars) == 3
    assert stars.ra_deg == pytest.approx([10.0, 10.5, 11.0], abs=1e-3)
    assert stars.dec_deg == pytest.approx([40.0, 40.1, 12.0], abs=1e-3)
    assert stars.mag == pytest.approx([8.0, 8.0, 9.5], abs=0.05)


def test_header_records_are_not_stars(tmp_path: Path) -> None:
    """`FF FF FF` cannot be a real RA, and must never appear in the output."""
    tile = write(tmp_path, build_tile([(1.0, 1.0, 5.0), (2.0, 2.0, 6.0), (3.0, 3.0, 7.0)]))

    stars = read_tile(tile)

    assert len(stars) == 3
    assert (stars.ra_deg == 255.0 * 360.0 / (256**3 - 1)).sum() == 0


def test_the_manual_worked_example_agrees_on_ra_and_not_on_declination(tmp_path: Path) -> None:
    """ASTAP's appendix gives Sirius as C3 06 48 / D7 39 E8 -> -16.7161401 deg.

    That only holds if ``dec9`` is read as **two's complement**, which is what the
    shipped tiles do *not* do: the same appendix's header-record example describes an
    offset of -128, and the real files follow it. Reading the tiles as two's complement
    puts the polar tile at +73 deg instead of inside -90..-87.4 (see the real-file test
    below), so the offsets in this module are the ones the data uses and the manual's
    worked example is left as evidence of the older convention.

    RA, which has no such offset, reproduces the manual exactly.
    """
    preamble = b"TEST".ljust(HEADER_BYTES, b" ")
    payload = bytes((0xC3, 0x06, 0x48, 0xD7, 0x39))
    # The section header supplies dec9; the record carries dec7/dec8 = D7 39.
    tile = write(tmp_path, preamble + b"\x05" + encode_header(0xE8, 0x20) + payload)

    stars = read_tile(tile)

    assert stars.ra_deg[0] / 15.0 == pytest.approx(6.75247662, abs=1e-6)
    # Under two's complement the manual's own arithmetic is reproduced...
    dec9_two_complement = 0xE8 - 256
    manual_dec = (0xD7 + 0x39 * 256 + dec9_two_complement * 65536) * 90 / (128 * 256**2 - 1)
    assert manual_dec == pytest.approx(-16.7161401, abs=1e-6)
    # ...and it is deliberately not what we return for that byte.
    assert stars.dec_deg[0] != pytest.approx(manual_dec, abs=0.1)


def test_a_six_byte_tile_is_refused_with_the_reason(tmp_path: Path) -> None:
    """The V50 family adds a colour byte; decoding it as 5 would shear every record."""
    tile = write(tmp_path, build_tile([(1.0, 1.0, 5.0)], record_size=6), "v50_0101.1476")

    with pytest.raises(ValueError, match="6-byte V50"):
        read_tile(tile)


def test_a_truncated_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="too short"):
        read_tile(write(tmp_path, b"nope"))


def test_brighter_than_filters_in_place() -> None:
    stars = Stars(np.array([1.0, 2.0]), np.array([3.0, 4.0]), np.array([5.0, 11.0]))

    bright = stars.brighter_than(8.0)

    assert len(bright) == 1
    assert bright.mag[0] == 5.0


# -- the index and its queries ---------------------------------------------
def test_tiles_are_indexed_by_what_they_actually_contain(tmp_path: Path) -> None:
    write(tmp_path, build_tile([(10.0, 40.0, 8.0), (12.0, 41.0, 9.0)]), "d80_2001.1476")

    catalog = StarCatalog.load(tmp_path)

    tile = catalog.tiles[0]
    assert tile.ra_min == pytest.approx(10.0, abs=1e-3)
    assert tile.ra_max == pytest.approx(12.0, abs=1e-3)
    assert tile.contains(11.0, 40.5)
    assert not tile.contains(11.0, 50.0)


def test_stars_near_selects_a_true_cone(tmp_path: Path) -> None:
    # Two stars 0.2 deg apart, plus one 5 deg away in the same tile.
    write(
        tmp_path,
        build_tile([(10.0, 40.0, 8.0), (10.0, 40.2, 9.0), (10.0, 45.0, 9.0)]),
        "d80_2001.1476",
    )
    catalog = StarCatalog.load(tmp_path)

    near = catalog.stars_near(10.0, 40.1, 0.5)

    assert len(near) == 2, "the distant star must not smuggle itself in"


def test_stars_near_respects_the_magnitude_limit(tmp_path: Path) -> None:
    write(tmp_path, build_tile([(10.0, 40.0, 8.0), (10.0, 40.05, 13.0)]), "d80_2001.1476")
    catalog = StarCatalog.load(tmp_path)

    assert len(catalog.stars_near(10.0, 40.0, 0.5, mag_limit=10.0)) == 1
    assert len(catalog.stars_near(10.0, 40.0, 0.5, mag_limit=14.0)) == 2


def test_the_index_is_cached_and_invalidated_by_a_new_tile(tmp_path: Path) -> None:
    write(tmp_path, build_tile([(10.0, 40.0, 8.0)]), "d80_2001.1476")
    cache = tmp_path / "index.json"

    first = StarCatalog.load(tmp_path, cache=cache)
    assert cache.is_file()
    assert StarCatalog.load(tmp_path, cache=cache).tiles == first.tiles

    write(tmp_path, build_tile([(20.0, -40.0, 8.0)]), "d80_1001.1476")
    assert len(StarCatalog.load(tmp_path, cache=cache).tiles) == 2, "cache must be rebuilt"


def test_a_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"no \.1476"):
        StarCatalog.load(tmp_path / "nowhere")


# -- against the real database ---------------------------------------------
requires_astap = pytest.mark.skipif(
    not (ASTAP_DIR / "d80_0101.1476").is_file(), reason="ASTAP D80 database not installed"
)


@requires_astap
def test_the_polar_tile_lands_inside_its_documented_ring() -> None:
    """Ring 0-1 is -90.000 to -87.4286 deg: a decoder off by a bit cannot pass this."""
    stars = read_tile(ASTAP_DIR / "d80_0101.1476")

    assert len(stars) > 100_000
    assert stars.dec_deg.min() == pytest.approx(-90.0, abs=0.05)
    assert stars.dec_deg.max() == pytest.approx(-87.4286, abs=0.01)


@requires_astap
def test_sirius_is_where_its_proper_motion_puts_it() -> None:
    """Epoch 2025, not J2000: the offset is motion, not decode error.

    One tile rather than the whole index -- the point is the decoder, and walking 1.3 GB
    in a unit test would be a bad trade.
    """
    stars = read_tile(ASTAP_DIR / "d80_1519.1476")
    separation = np.hypot(
        ((stars.ra_deg - 101.2872 + 180.0) % 360.0 - 180.0) * np.cos(np.radians(stars.dec_deg)),
        stars.dec_deg + 16.7161,
    )
    # Search by brightness, not by proximity: the catalogue is Gaia-derived, so a faint
    # field star can easily sit closer to Sirius' J2000 position than Sirius does.
    nearby = separation < 0.05
    assert nearby.any(), "nothing within 3 arcmin of Sirius"
    brightest = int(np.argmin(np.where(nearby, stars.mag, 99.0)))

    assert stars.mag[brightest] == pytest.approx(-1.5, abs=0.15)
    assert 3600 * float(separation[brightest]) < 60.0
