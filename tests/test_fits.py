"""Round-trip tests for the minimal FITS reader."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ministar.io.fits import FitsError, read_fits


def _card(key: str, value: str) -> str:
    return f"{key:<8}= {value:>20}".ljust(80)


def write_fits(
    path: Path,
    data: np.ndarray,
    *,
    bitpix: int = 16,
    bzero: float | None = None,
    bscale: float | None = None,
    extra: str = "",
    truncate: bool = False,
) -> Path:
    cards = [
        _card("SIMPLE", "T"),
        _card("BITPIX", str(bitpix)),
        _card("NAXIS", str(data.ndim)),
    ]
    for axis in range(data.ndim, 0, -1):
        cards.append(_card(f"NAXIS{axis}", str(data.shape[-axis])))
    if bzero is not None:
        cards.append(_card("BZERO", repr(bzero)))
    if bscale is not None:
        cards.append(_card("BSCALE", repr(bscale)))
    if extra:
        cards.append(extra.ljust(80))
    cards.append("END".ljust(80))

    header = "".join(cards).encode("ascii")
    header += b" " * ((2880 - len(header) % 2880) % 2880)

    raw = data.astype({16: ">i2", 32: ">i4", -32: ">f4"}[bitpix]).tobytes()
    if truncate:
        raw = raw[: len(raw) // 2]
    path.write_bytes(header + raw)
    return path


def test_roundtrip_int16(tmp_path: Path) -> None:
    data = np.arange(12, dtype=np.int16).reshape(3, 4)
    array, header = read_fits(write_fits(tmp_path / "a.fit", data))
    assert array.shape == (3, 4)
    assert np.array_equal(array, data)
    assert header.get("SIMPLE") == "T"


def test_roundtrip_float32(tmp_path: Path) -> None:
    data = (np.arange(6, dtype=np.float32) * 0.5).reshape(2, 3)
    array, _ = read_fits(write_fits(tmp_path / "b.fit", data, bitpix=-32))
    assert np.allclose(array, data)


def test_unsigned_16_via_bzero(tmp_path: Path) -> None:
    """The common DSLR case: unsigned data stored as signed with BZERO=32768."""
    # FITS stores unsigned 16-bit as signed with BZERO=32768, so unsigned
    # 0/32768/65535/1000 land on the wire as -32768/0/32767/-31768.
    data = np.array([[-32768, 0, 32767, -31768]], dtype=np.int16)
    array, _ = read_fits(write_fits(tmp_path / "c.fit", data, bzero=32768.0))
    assert array.dtype == np.uint16
    assert array[0, 0] == 0
    assert array[0, 1] == 32768
    assert array[0, 2] == 65535


def test_three_plane_detection_image(tmp_path: Path) -> None:
    data = np.zeros((3, 4, 5), dtype=np.int16)
    data[1] = 7
    array, _ = read_fits(write_fits(tmp_path / "d.fit", data))
    assert array.shape == (4, 5, 3)
    assert (array[:, :, 1] == 7).all()


def test_comments_after_value_stripped(tmp_path: Path) -> None:
    card = _card("FOCALLEN", "384.718") + " / [mm] focal length"
    path = tmp_path / "e.fit"
    path.write_bytes(b"")
    write_fits(path, np.zeros((2, 2), dtype=np.int16), extra=card[:80])
    _, header = read_fits(path)
    assert header.float("FOCALLEN") == 384.718


def test_truncated_data_raises(tmp_path: Path) -> None:
    data = np.arange(100, dtype=np.int16).reshape(10, 10)
    path = write_fits(tmp_path / "f.fit", data, truncate=True)
    try:
        read_fits(path)
    except FitsError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("expected FitsError for truncated data")
