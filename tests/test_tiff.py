"""The CR2/EXIF reader, against synthetic TIFFs.

A real CR2 is 25 MB and the parser only needs IFD0 and the Exif IFD, so the
fixtures here are built byte by byte: that makes the byte order, the inline-vs-
offset rule and the missing-tag cases explicit instead of implicit in one sample
file. A real frame validates the same code in ``ministar probe`` on hardware.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ministar.io.tiff import (
    RESOLUTION_UNIT_CENTIMETRE,
    RESOLUTION_UNIT_INCH,
    TiffError,
    read_frame_exif,
)

#: A 4.3 µm pitch expressed as pixels per inch, as a body would report it.
PIXELS_PER_INCH = 25400 / 4.3


def build_tiff(
    *,
    endian: str = "<",
    pixels_per_unit: float = PIXELS_PER_INCH,
    unit: int = RESOLUTION_UNIT_INCH,
    focal_mm: float = 200.0,
    pixel_x: int = 5202,
    pixel_y: int = 3464,
    make: str = "Canon",
    model: str = "Canon EOS 60D",
    lens: str = "EF200mm f/2.8L",
    orientation: int = 1,
    with_exif_ifd: bool = True,
) -> bytes:
    """Lay out a minimal TIFF: header, IFD0, Exif IFD, then the data they point at."""

    def rational(value: float) -> bytes:
        return struct.pack(endian + "II", round(value * 10_000), 10_000)

    ifd0_offset = 8
    ifd0_count = 4
    exif_offset = ifd0_offset + 2 + 12 * ifd0_count + 4
    exif_count = 7
    data_offset = exif_offset + 2 + 12 * exif_count + 4

    blob = bytearray()

    def add(raw: bytes) -> int:
        offset = data_offset + len(blob)
        blob.extend(raw)
        return offset

    make_off = add(make.encode() + b"\0")
    model_off = add(model.encode() + b"\0")
    lens_off = add(lens.encode() + b"\0")
    x_res_off = add(rational(pixels_per_unit))
    y_res_off = add(rational(pixels_per_unit))
    focal_off = add(rational(focal_mm))

    ifd0 = [
        (0x010F, 2, len(make) + 1, make_off),  # Make
        (0x0110, 2, len(model) + 1, model_off),  # Model
        (0x0112, 3, 1, orientation),  # Orientation
        (0x8769, 4, 1, exif_offset if with_exif_ifd else 0),  # Exif IFD pointer
    ]
    exif = [
        (0x011A, 5, 1, x_res_off),  # FocalPlaneXResolution
        (0x011B, 5, 1, y_res_off),  # FocalPlaneYResolution
        (0x0128, 3, 1, unit),  # FocalPlaneResolutionUnit
        (0x920A, 5, 1, focal_off),  # FocalLength
        (0xA002, 4, 1, pixel_x),  # PixelXDimension
        (0xA003, 4, 1, pixel_y),  # PixelYDimension
        (0xA434, 2, len(lens) + 1, lens_off),  # LensModel
    ]

    out = bytearray()
    out += b"II" if endian == "<" else b"MM"
    out += struct.pack(endian + "H", 42)
    out += struct.pack(endian + "I", ifd0_offset)

    def inline_field(kind: int, value: int) -> bytes:
        """A value that fits in four bytes, left-justified as TIFF 6.0 requires.

        Left-justified on both byte orders is what makes a big-endian inline SHORT
        different from ``struct.pack(">I", value)``: the bytes go at the *start* of
        the field, so a reader that unpacks the whole 32-bit word sees it shifted.
        """
        width = 2 if kind == 3 else 4
        order = "little" if endian == "<" else "big"
        return value.to_bytes(width, order).ljust(4, b"\0")

    def write_ifd(entries, offset: int) -> None:
        nonlocal out
        out += struct.pack(endian + "H", len(entries))
        for tag, kind, count, value in entries:
            out += struct.pack(endian + "HHI", tag, kind, count)
            if kind in (3, 4):
                out += inline_field(kind, value)
            else:  # ASCII and RATIONAL always live at an offset in these fixtures
                out += struct.pack(endian + "I", value)
        out += struct.pack(endian + "I", 0)  # no next IFD

    write_ifd(ifd0, ifd0_offset)
    write_ifd(exif, exif_offset)
    assert len(out) == data_offset, "fixture layout drifted"
    out += blob
    return bytes(out)


def write(tmp_path: Path, data: bytes, name: str = "frame.cr2") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_reads_the_facts_the_picker_needs(tmp_path: Path) -> None:
    exif = read_frame_exif(write(tmp_path, build_tiff()))

    assert exif.model == "Canon EOS 60D"
    assert exif.make == "Canon"
    assert exif.lens_model == "EF200mm f/2.8L"
    assert exif.focal_length_mm == pytest.approx(200.0)
    assert exif.pixel_x_dimension == 5202
    assert exif.pixel_y_dimension == 3464


def test_pixel_pitch_comes_from_the_resolution_unit(tmp_path: Path) -> None:
    """Inch is the common case, cm happens: both must land on 4.3 µm."""
    inch = read_frame_exif(write(tmp_path, build_tiff(), "inch.cr2"))
    assert inch.pixel_size_um == pytest.approx(4.3, abs=0.01)

    centimetre = read_frame_exif(
        write(
            tmp_path,
            build_tiff(unit=RESOLUTION_UNIT_CENTIMETRE, pixels_per_unit=10000 / 4.3),
            "cm.cr2",
        )
    )
    assert centimetre.pixel_size_um == pytest.approx(4.3, abs=0.01)


def test_sensor_size_is_the_used_area_not_a_guess(tmp_path: Path) -> None:
    exif = read_frame_exif(write(tmp_path, build_tiff()))
    width, height = exif.sensor_size_mm  # type: ignore[misc]

    assert width == pytest.approx(5202 * 4.3 / 1000, abs=0.02)
    assert height == pytest.approx(3464 * 4.3 / 1000, abs=0.02)


def test_big_endian_files_read_the_same(tmp_path: Path) -> None:
    """Nikon writes MM where Canon writes II, and inline shorts pack differently."""
    little = read_frame_exif(write(tmp_path, build_tiff(), "le.cr2"))
    big = read_frame_exif(write(tmp_path, build_tiff(endian=">"), "be.cr2"))

    assert big.pixel_size_um == pytest.approx(little.pixel_size_um, abs=1e-6)  # type: ignore[arg-type]
    assert big.focal_length_mm == little.focal_length_mm
    assert big.orientation == little.orientation


def test_a_body_without_the_resolution_tags_reports_unknown(tmp_path: Path) -> None:
    """Some bodies omit FocalPlane*: the answer is None, never a guessed pitch."""
    data = build_tiff(unit=RESOLUTION_UNIT_INCH, pixels_per_unit=0.0)
    exif = read_frame_exif(write(tmp_path, data))

    assert exif.pixel_size_um is None
    assert exif.sensor_size_mm is None
    assert exif.focal_length_mm is not None, "an unrelated tag must still be read"


def test_orientation_flags_a_stored_rotation(tmp_path: Path) -> None:
    upright = read_frame_exif(write(tmp_path, build_tiff(orientation=1), "up.cr2"))
    rolled = read_frame_exif(write(tmp_path, build_tiff(orientation=6), "rolled.cr2"))

    assert not upright.stored_rotated
    assert rolled.stored_rotated


def test_a_missing_exif_ifd_still_yields_ifd0(tmp_path: Path) -> None:
    exif = read_frame_exif(write(tmp_path, build_tiff(with_exif_ifd=False)))

    assert exif.model == "Canon EOS 60D"
    assert exif.focal_length_mm is None
    assert exif.pixel_size_um is None


def test_a_file_that_is_not_tiff_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "not-a-frame.cr2"
    path.write_bytes(b"\xff\xd8\xff\xe0 this is a JPEG")

    with pytest.raises(TiffError, match="not a TIFF"):
        read_frame_exif(path)


def test_the_eos_60d_placeholder_resolution_is_rejected(tmp_path: Path) -> None:
    """Measured on hardware 2026-10-27: the 60D writes 72 dpi, i.e. no measurement.

    Believing it gives a 352.8 um pitch and a 1.8-metre sensor, so every FOV the
    picker showed would be ~82x too wide. The number must be refused, loudly.
    """
    exif = read_frame_exif(
        write(
            tmp_path,
            build_tiff(pixels_per_unit=72.0, unit=RESOLUTION_UNIT_INCH, pixel_x=5184, pixel_y=3456),
            "eos60d.cr2",
        )
    )

    assert exif.pixel_size_um == pytest.approx(352.78, abs=0.01), "the conversion is literal"
    assert exif.sensor_size_mm is None, "but the derived size must not be"
    assert "placeholder" in exif.geometry_problem()
    assert "plate solve" in exif.geometry_problem()


def test_plausible_geometry_says_nothing(tmp_path: Path) -> None:
    assert read_frame_exif(write(tmp_path, build_tiff())).geometry_problem() == ""


def test_as_dict_carries_the_derived_values_into_the_report(tmp_path: Path) -> None:
    """The probe report is JSON, so the unit conversions must already be in it."""
    report = read_frame_exif(write(tmp_path, build_tiff())).as_dict()

    assert report["pixel_size_um"] == pytest.approx(4.3, abs=0.01)
    assert isinstance(report["sensor_width_mm"], float)
    assert report["sensor_height_mm"] == pytest.approx(14.9, abs=0.05)
