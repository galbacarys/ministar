"""RAW -> FITS: the conversion, without dcraw or a camera.

dcraw is injected as a `runner`, exactly as the camera's FFI is: the decisions that can
be wrong -- which flags, where the TIFF lands, what goes in the header, whether the
intermediate is cleaned up -- are all testable anywhere. The real dcraw runs in
`ministar to-fits` on the Windows side.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from ministar.io.fits import read_fits
from ministar.io.tiff import read_frame_exif, read_tiff_array
from ministar.raw import DCRAW_ARGS, bayer_pattern, convert_to_fits


def build_image_tiff(
    data: np.ndarray, *, strips: int = 1, endian: str = "<", compression: int = 1
) -> bytes:
    """An uncompressed TIFF of 16-bit samples, the shape dcraw -T writes."""

    height, width = data.shape
    rows_per_strip = height if strips == 1 else max(1, height // strips)
    strip_bytes = []
    for index in range(strips):
        first = index * rows_per_strip
        last = min(height, first + rows_per_strip)
        if first >= last:
            break
        strip_bytes.append(data[first:last].astype(f"{endian}u2").tobytes())
    count = len(strip_bytes)

    tags = 11
    ifd_offset = 8
    arrays_offset = ifd_offset + 2 + 12 * tags + 4
    offsets_size = 8 * count if count > 1 else 0
    payload_start = arrays_offset + offsets_size

    offsets = []
    cursor = payload_start
    for blob in strip_bytes:
        offsets.append(cursor)
        cursor += len(blob)
    counts = [len(blob) for blob in strip_bytes]

    entries = [
        (0x0100, 4, 1, width),
        (0x0101, 4, 1, height),
        (0x0102, 3, 1, 16),
        (0x0103, 3, 1, compression),
        (0x0106, 3, 1, 1),
        (0x0111, 4, count, offsets[0] if count == 1 else arrays_offset),
        (0x0115, 3, 1, 1),
        (0x0116, 4, 1, rows_per_strip),
        (0x0117, 4, count, counts[0] if count == 1 else arrays_offset + 4 * count),
        (0x011C, 3, 1, 1),
        (0x0153, 3, 1, 1),
    ]

    out = bytearray(b"II" if endian == "<" else b"MM")
    out += struct.pack(endian + "H", 42)
    out += struct.pack(endian + "I", ifd_offset)
    out += struct.pack(endian + "H", len(entries))
    for tag, kind, n, value in entries:
        out += struct.pack(endian + "HHI", tag, kind, n)
        width_bytes = 2 if kind == 3 else 4
        order = "little" if endian == "<" else "big"
        out += value.to_bytes(width_bytes, order).ljust(4, b"\0")
    out += struct.pack(endian + "I", 0)
    if count > 1:
        out += struct.pack(endian + "I" * count, *offsets)
        out += struct.pack(endian + "I" * count, *counts)
    assert len(out) == payload_start, "fixture layout drifted"
    for blob in strip_bytes:
        out += blob
    return bytes(out)


class FakeDcraw:
    """Stands in for the dcraw subprocess: writes the TIFF, reports the CFA pattern."""

    def __init__(self, image: np.ndarray, *, pattern: str = "RGGB", fail: bool = False) -> None:
        self.image = image
        self.pattern = pattern
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, capture_output=False, text=False):
        self.calls.append([str(a) for a in args])
        if self.fail:
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "bad raw"})()
        if "-i" in args:
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": f"Filename: x\nFilter pattern: {self.pattern}\n",
                    "stderr": "",
                },
            )()
        # The real dcraw appends ".tiff" to the input file name.
        raw = Path(args[-1])
        (raw.parent / f"{raw.name}.tiff").write_bytes(build_image_tiff(self.image))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()


@pytest.fixture
def raw_file(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_0001.CR2"
    path.write_bytes(b"not really a raw, dcraw is faked")
    return path


def test_converts_to_fits_beside_the_raw(raw_file: Path, tmp_path: Path) -> None:
    image = np.arange(24, dtype=np.uint16).reshape(4, 6) * 1000
    target = convert_to_fits(raw_file, dcraw=tmp_path / "dcraw.exe", runner=FakeDcraw(image))

    assert target == raw_file.with_suffix(".fit")
    array, header = read_fits(target)
    assert np.array_equal(array, image)
    assert header.get("BITPIX") == "16"
    assert header.get("BZERO") == "32768", "unsigned sensor data must survive the write"


def test_the_flags_keep_the_frame_undebayered_and_linear(raw_file: Path, tmp_path: Path) -> None:
    """-D is the whole point: a demosaiced frame is not what a stacker wants."""
    fake = FakeDcraw(np.zeros((2, 2), dtype=np.uint16))
    convert_to_fits(raw_file, dcraw=tmp_path / "dcraw.exe", runner=fake)

    convert_call = next(call for call in fake.calls if "-i" not in call)
    assert convert_call[1 : 1 + len(DCRAW_ARGS)] == list(DCRAW_ARGS)
    assert "-D" in convert_call and "-4" in convert_call and "-T" in convert_call


def test_the_intermediate_tiff_is_removed(raw_file: Path, tmp_path: Path) -> None:
    convert_to_fits(
        raw_file, dcraw=tmp_path / "dcraw.exe", runner=FakeDcraw(np.zeros((2, 2), np.uint16))
    )

    leftovers = list(tmp_path.glob("*.tiff"))
    assert leftovers == [], f"conversion left {leftovers} behind"


def test_headers_come_from_the_raw_when_it_has_exif(tmp_path: Path) -> None:
    """A CR2 with EXIF yields cards; the fixture here has none, so they are absent."""
    raw = tmp_path / "IMG_0002.CR2"
    raw.write_bytes(b"no exif here")

    target = convert_to_fits(
        raw, dcraw=tmp_path / "dcraw.exe", runner=FakeDcraw(np.ones((2, 2), np.uint16))
    )

    _, header = read_fits(target)
    assert header.get("IMAGETYP") == "LIGHT"
    assert header.get("ORIGIN") == "ministar"
    assert header.get("BAYERPAT") == "RGGB"
    assert header.get("INSTRUME") is None, "no EXIF means no camera claim"


def test_a_failing_converter_raises_instead_of_writing_junk(raw_file: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exited 1: bad raw"):
        convert_to_fits(
            raw_file,
            dcraw=tmp_path / "dcraw.exe",
            runner=FakeDcraw(np.zeros((2, 2), np.uint16), fail=True),
        )


def test_a_missing_raw_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        convert_to_fits(
            tmp_path / "nope.CR2",
            dcraw=tmp_path / "dcraw.exe",
            runner=FakeDcraw(np.zeros((2, 2), np.uint16)),
        )


def test_bayer_pattern_is_read_from_dcraw(tmp_path: Path) -> None:
    raw = tmp_path / "x.CR2"
    raw.write_bytes(b"x")

    assert (
        bayer_pattern(
            dcraw=tmp_path / "dcraw.exe",
            raw=raw,
            runner=FakeDcraw(np.zeros((2, 2), np.uint16), pattern="BGGR"),
        )
        == "BGGR"
    )


# -- the TIFF reader the conversion depends on ------------------------------
def test_reads_a_single_strip_16_bit_tiff(tmp_path: Path) -> None:
    image = np.arange(12, dtype=np.uint16).reshape(3, 4) * 7
    path = tmp_path / "one.tiff"
    path.write_bytes(build_image_tiff(image))

    assert np.array_equal(read_tiff_array(path), image)


def test_reads_a_multi_strip_tiff(tmp_path: Path) -> None:
    image = np.arange(24, dtype=np.uint16).reshape(4, 6) * 3
    path = tmp_path / "strips.tiff"
    path.write_bytes(build_image_tiff(image, strips=2))

    assert np.array_equal(read_tiff_array(path), image)


def test_reads_a_big_endian_tiff(tmp_path: Path) -> None:
    image = np.arange(6, dtype=np.uint16).reshape(2, 3) * 5
    path = tmp_path / "be.tiff"
    path.write_bytes(build_image_tiff(image, endian=">"))

    assert np.array_equal(read_tiff_array(path), image)


def test_compressed_tiffs_are_refused_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "packed.tiff"
    path.write_bytes(build_image_tiff(np.zeros((2, 2), np.uint16), compression=32773))

    with pytest.raises(Exception, match="compressed TIFF"):
        read_tiff_array(path)


def test_exif_survives_a_tiff_that_has_none(tmp_path: Path) -> None:
    """The image reader must not choke on a file with no EXIF IFD."""
    path = tmp_path / "plain.tiff"
    path.write_bytes(build_image_tiff(np.zeros((2, 2), np.uint16)))

    exif = read_frame_exif(path)
    assert exif.model == ""
    assert exif.pixel_size_um is None


def test_fits_files_are_written_bottom_up(tmp_path: Path) -> None:
    """FITS stores the bottom row first; a top-down file reads as a vertical mirror.

    This cost a real debugging session: a synthetic frame written top-down solved with
    position angle 180 and parity "mirrored" instead of 0 and normal, and every FITS we
    produce would have been upside down to ASTAP, NINA and any stacker.
    """
    from ministar.io.fits import read_fits, write_fits

    image = np.array([[1, 1], [2, 2], [3, 3]], dtype=np.uint16)  # row 0 at the TOP
    path = write_fits(tmp_path / "orientation.fit", image)

    raw = path.read_bytes()
    offset = 2880 * (raw.index(b"END") // 2880 + 1)
    body = np.frombuffer(raw, dtype=">i2", offset=offset)[:6]
    # uint16 is stored as int16 with BZERO=32768, so value 3 is -32765: the bottom row.
    assert body[:2].tolist() == [-32765, -32765], "the file must start with the bottom row"
    assert np.array_equal(read_fits(path)[0], image), "and round-trip unchanged"
