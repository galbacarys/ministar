"""The CR2/EXIF reader, against synthetic TIFFs and the numbers real bodies report.

A real CR2 is 25 MB and the parser only needs IFD0 and the Exif IFD, so most fixtures
here are built byte by byte: that makes the byte order, the inline-vs-offset rule and
the missing-tag cases explicit instead of implicit in one sample file.

The constants are not invented. Each brand case below is a value read out of a real
file on 2026-10-27:

* Canon EOS 60D CR2: ``0xA20E = 5184000/905`` (inch), and ``0x011A`` **also** = 72
* Fujifilm X-E5 RAF: ``0xA20E = 1879`` in **centimetres**, inside a proprietary container
* Nikon 1 AW1 NEF and Sony RX100M7A ARW: ``0xA20E`` **absent**; only the print 0x011A
  (300 and 350 dpi) is present, which is exactly the trap this reader exists to avoid
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


def build_tiff(
    *,
    endian: str = "<",
    #: The TIFF *print* resolution (0x011A) -- always present in real files, never useful.
    print_dpi: float = 72.0,
    #: The sensor's resolution (0xA20E) in pixels per `unit`. None omits the tags.
    focal_plane_ppi: float | None = 25400 / 4.3,
    focal_plane_ppi_y: float | None = None,
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

    def exact(numerator: int, denominator: int) -> bytes:
        return struct.pack(endian + "II", numerator, denominator)

    ifd0_tags = 7  # Make, Model, Orientation, ExifIFD, XRes, YRes, ResUnit
    exif_tags = 7 if focal_plane_ppi is not None else 4
    ifd0_offset = 8
    exif_offset = ifd0_offset + 2 + 12 * ifd0_tags + 4
    data_offset = exif_offset + 2 + 12 * exif_tags + 4

    blob = bytearray()

    def add(raw: bytes) -> int:
        offset = data_offset + len(blob)
        blob.extend(raw)
        return offset

    make_off = add(make.encode() + b"\0")
    model_off = add(model.encode() + b"\0")
    lens_off = add(lens.encode() + b"\0")
    print_x_off = add(exact(round(print_dpi), 1))
    print_y_off = add(exact(round(print_dpi), 1))

    ifd0 = [
        (0x010F, 2, len(make) + 1, make_off),  # Make
        (0x0110, 2, len(model) + 1, model_off),  # Model
        (0x0112, 3, 1, orientation),  # Orientation
        (0x8769, 4, 1, exif_offset if with_exif_ifd else 0),  # Exif IFD pointer
        (0x011A, 5, 1, print_x_off),  # XResolution   <- the trap
        (0x011B, 5, 1, print_y_off),  # YResolution
        (0x0128, 3, 1, unit),  # ResolutionUnit
    ]

    exif = [
        (0x920A, 5, 1, add(rational(focal_mm))),  # FocalLength
        (0xA002, 4, 1, pixel_x),  # PixelXDimension
        (0xA003, 4, 1, pixel_y),  # PixelYDimension
        (0xA434, 2, len(lens) + 1, lens_off),  # LensModel
    ]
    if focal_plane_ppi is not None:
        exif += [
            (0xA20E, 5, 1, add(rational(focal_plane_ppi))),  # FocalPlaneXResolution
            (0xA20F, 5, 1, add(rational(focal_plane_ppi_y or focal_plane_ppi))),
            (0xA210, 3, 1, unit),  # FocalPlaneResolutionUnit
        ]
    exif.sort()

    def inline_field(kind: int, value: int) -> bytes:
        """A value that fits in four bytes, left-justified as TIFF 6.0 requires.

        Left-justified on both byte orders is what makes a big-endian inline SHORT
        different from ``struct.pack(">I", value)``: the bytes go at the *start* of
        the field, so a reader that unpacks the whole 32-bit word sees it shifted.
        """
        width = 2 if kind == 3 else 4
        order = "little" if endian == "<" else "big"
        return value.to_bytes(width, order).ljust(4, b"\0")

    out = bytearray()
    out += b"II" if endian == "<" else b"MM"
    out += struct.pack(endian + "H", 42)
    out += struct.pack(endian + "I", ifd0_offset)

    def write_ifd(entries) -> None:
        out.extend(struct.pack(endian + "H", len(entries)))
        for tag, kind, count, value in entries:
            out.extend(struct.pack(endian + "HHI", tag, kind, count))
            if kind in (3, 4):
                out.extend(inline_field(kind, value))
            else:  # ASCII and RATIONAL always live at an offset in these fixtures
                out.extend(struct.pack(endian + "I", value))
        out.extend(struct.pack(endian + "I", 0))  # no next IFD

    write_ifd(ifd0)
    write_ifd(exif)
    assert len(out) == data_offset, f"fixture layout drifted ({len(out)} != {data_offset})"
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


def test_the_print_resolution_is_never_mistaken_for_the_sensor(tmp_path: Path) -> None:
    """The bug this module exists for: 0x011A is XResolution, not a pixel pitch.

    Reading it produced a 352.8 um pitch and a 1.8-metre sensor from an EOS 60D. With
    no focal-plane tags the honest answer is "unknown", not 72 dpi.
    """
    exif = read_frame_exif(write(tmp_path, build_tiff(focal_plane_ppi=None)))

    assert exif.focal_plane_x_resolution is None
    assert exif.pixel_size_um is None
    assert exif.sensor_size_mm is None
    assert "no usable focal-plane resolution" in exif.geometry_problem()


def test_pixel_pitch_comes_from_the_resolution_unit(tmp_path: Path) -> None:
    """Inch is the common case, cm happens: both must land on 4.3 µm."""
    inch = read_frame_exif(write(tmp_path, build_tiff(), "inch.cr2"))
    assert inch.pixel_size_um == pytest.approx(4.3, abs=0.01)

    centimetre = read_frame_exif(
        write(
            tmp_path,
            build_tiff(unit=RESOLUTION_UNIT_CENTIMETRE, focal_plane_ppi=10000 / 4.3),
            "cm.cr2",
        )
    )
    assert centimetre.pixel_size_um == pytest.approx(4.3, abs=0.01)


def test_the_eos_60d_numbers_are_usable(tmp_path: Path) -> None:
    """Real values: 5184000/905 and 3456000/595 pixels per inch, 5184x3456."""
    exif = read_frame_exif(
        write(
            tmp_path,
            build_tiff(
                focal_plane_ppi=5184000 / 905,
                focal_plane_ppi_y=3456000 / 595,
                pixel_x=5184,
                pixel_y=3456,
                focal_mm=24.0,
            ),
            "eos60d.cr2",
        )
    )

    assert exif.pixel_size_um == pytest.approx(4.434, abs=0.01)
    assert exif.geometry_problem() == ""
    width, height = exif.sensor_size_mm  # type: ignore[misc]
    assert width == pytest.approx(22.99, abs=0.05)
    assert height == pytest.approx(15.11, abs=0.05)


def test_a_fujifilm_raf_resolution_in_centimetres(tmp_path: Path) -> None:
    """The X-E5 writes 1879 per cm, i.e. 5.32 um -- and inside a non-TIFF container."""
    exif = read_frame_exif(
        write(
            tmp_path,
            build_tiff(
                focal_plane_ppi=1879.0,
                unit=RESOLUTION_UNIT_CENTIMETRE,
                pixel_x=4416,
                pixel_y=2944,
                make="FUJIFILM",
                model="X-E5",
            ),
            "x-e5.raf",
        )
    )

    assert exif.pixel_size_um == pytest.approx(5.322, abs=0.01)
    assert exif.geometry_problem() == ""


def test_big_endian_files_read_the_same(tmp_path: Path) -> None:
    """Nikon writes MM as readily as II, and inline shorts pack differently."""
    little = read_frame_exif(write(tmp_path, build_tiff(), "le.cr2"))
    big = read_frame_exif(write(tmp_path, build_tiff(endian=">"), "be.cr2"))

    assert big.pixel_size_um == pytest.approx(little.pixel_size_um, abs=1e-6)  # type: ignore[arg-type]
    assert big.focal_length_mm == little.focal_length_mm
    assert big.orientation == little.orientation


def test_a_bogus_focal_plane_value_is_refused(tmp_path: Path) -> None:
    """72 in the *right* tag is still nonsense: 352 um is not a sensor pitch."""
    exif = read_frame_exif(write(tmp_path, build_tiff(focal_plane_ppi=72.0)))

    assert exif.pixel_size_um == pytest.approx(352.78, abs=0.01), "the conversion is literal"
    assert exif.sensor_size_mm is None
    assert "not a physical sensor pitch" in exif.geometry_problem()


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


def test_an_exif_blob_inside_a_proprietary_container_is_found(tmp_path: Path) -> None:
    """Fuji's RAF is `FUJIFILMCCD-RAW...` with an Exif+NUL+TIFF payload a little way in."""
    tiff = build_tiff(make="FUJIFILM", model="X-E5")
    raf = b"FUJIFILMCCD-RAW 0201FF4137440\0" + b"\0" * 100 + b"Exif\0\0" + tiff

    exif = read_frame_exif(write(tmp_path, raf, "container.raf"))

    assert exif.model == "X-E5"
    assert exif.pixel_size_um == pytest.approx(4.3, abs=0.01)


def test_a_jpeg_app1_segment_is_found(tmp_path: Path) -> None:
    """Thumbnails and previews wrap the same TIFF in a JPEG APP1 segment."""
    tiff = build_tiff()
    length = struct.pack(">H", len(tiff) + 8)
    jpeg = b"\xff\xd8\xff\xe1" + length + b"Exif\0\0" + tiff + b"\xff\xd9"

    exif = read_frame_exif(write(tmp_path, jpeg, "preview.jpg"))

    assert exif.model == "Canon EOS 60D"


def test_a_file_with_no_exif_at_all_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "not-a-frame.cr2"
    path.write_bytes(b"\x00\x01\x02\x03 nothing to see here")

    with pytest.raises(TiffError, match="no TIFF header"):
        read_frame_exif(path)


def test_as_dict_carries_the_derived_values_into_the_report(tmp_path: Path) -> None:
    """The probe report is JSON, so the unit conversions must already be in it."""
    report = read_frame_exif(write(tmp_path, build_tiff())).as_dict()

    assert report["pixel_size_um"] == pytest.approx(4.3, abs=0.01)
    assert isinstance(report["sensor_width_mm"], float)
    assert report["sensor_height_mm"] == pytest.approx(14.9, abs=0.05)
