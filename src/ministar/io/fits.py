"""A minimal FITS reader: primary HDU, no external dependencies.

Deliberately small. It exists so the focus metric can be exercised against real
frames without pulling in astropy, and it is the first thing the future stacker
will need anyway.

Handles what DSLR capture and ASTAP actually emit: ``BITPIX`` 8/16/32/-32/-64,
``BZERO``/``BSCALE`` scaling (unsigned 16-bit stored as signed with BZERO=32768 is
the common case), and three-plane RGB primary HDUs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

_BLOCK = 2880
_CARD = 80

_DTYPES: dict[int, np.dtype] = {
    8: np.dtype(">u1"),
    16: np.dtype(">i2"),
    32: np.dtype(">i4"),
    -32: np.dtype(">f4"),
    -64: np.dtype(">f8"),
}


class FitsError(ValueError):
    pass


@dataclass(frozen=True)
class FitsHeader:
    cards: dict[str, str]

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.cards.get(key, default)

    def float(self, key: str, default: float = 0.0) -> float:
        raw = self.cards.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def int(self, key: str, default: int = 0) -> int:
        return int(self.float(key, default))


def _parse_header(raw: bytes) -> tuple[dict[str, str], int]:
    cards: dict[str, str] = {}
    offset = 0
    while offset + _BLOCK <= len(raw):
        block = raw[offset : offset + _BLOCK]
        for i in range(0, _BLOCK, _CARD):
            card = block[i : i + _CARD].decode("ascii", "replace")
            if card.startswith("END"):
                return cards, offset + _BLOCK
            key = card[:8].strip()
            if card[8:10] == "= " and key:
                # Values may be quoted and carry a / comment.
                value = card[10:].split("/")[0].strip()
                cards[key] = value.strip("'").strip()
        offset += _BLOCK
    raise FitsError("no END card in header")


def read_fits(path: Path | str) -> tuple[np.ndarray, FitsHeader]:
    """Read the primary HDU of a FITS file as a numpy array."""
    data = Path(path).read_bytes()
    cards, data_offset = _parse_header(data)
    header = FitsHeader(cards)

    bitpix = header.int("BITPIX", 0)
    dtype = _DTYPES.get(bitpix)
    if dtype is None:
        raise FitsError(f"unsupported BITPIX {bitpix}")

    naxis = header.int("NAXIS", 0)
    if naxis < 2:
        raise FitsError("only 2D/3D primary HDUs are supported")

    width = header.int("NAXIS1")
    height = header.int("NAXIS2")
    planes = header.int("NAXIS3", 1) if naxis >= 3 else 1
    if width <= 0 or height <= 0:
        raise FitsError("missing or invalid NAXIS1/NAXIS2")

    count = width * height * planes
    needed = count * dtype.itemsize
    if data_offset + needed > len(data):
        raise FitsError(f"truncated data: need {needed} bytes, have {len(data) - data_offset}")

    flat = np.frombuffer(data, dtype=dtype, count=count, offset=data_offset)
    array = flat.astype(dtype.newbyteorder("=")).reshape((planes, height, width))

    bzero = header.float("BZERO", 0.0)
    bscale = header.float("BSCALE", 1.0)
    if bzero or bscale != 1.0:
        array = array.astype(np.float64) * bscale + bzero
        if bitpix == 16 and bzero == 32768.0:
            array = np.clip(array, 0, 65535).astype(np.uint16)

    # The file is bottom-up; numpy arrays are top-down.
    array = np.flip(array, axis=-2)
    if planes > 1:
        return np.moveaxis(array, 0, -1), header
    return array[0], header


def _card(keyword: str, value: object, comment: str = "") -> str:
    """One 80-column header card. Values are written the way FITS expects them."""
    if isinstance(value, bool):
        text = "T" if value else "F"
    elif isinstance(value, str):
        text = f"'{value}'"
    else:
        text = str(value)
    body = f"{keyword:<8}= {text:>20}"
    card = f"{body} / {comment}" if comment else body
    return card[:80].ljust(80)


def write_fits(
    path: Path | str,
    array: np.ndarray,
    cards: dict[str, object] | None = None,
    comments: dict[str, str] | None = None,
) -> Path:
    """Write a primary HDU: 2-D mono or 3-D (height, width, planes) RGB.

    ``uint16`` is stored the way astronomy expects -- ``BITPIX = 16`` with
    ``BZERO = 32768`` -- so a sensor frame keeps its full 0-65535 range without
    becoming float or losing the top half. ``int16``/``float32``/``float64``/``uint8``
    are written directly.

    Rows are written **bottom-up**, as FITS requires: row 0 of the numpy array is the
    *last* data row in the file. Getting this wrong is invisible in a round trip through
    this module and very visible everywhere else -- an array written top-down reads as a
    vertical mirror to ASTAP, which reports position angle 180 with parity "mirrored"
    instead of the truth. Measured on a synthetic frame, 2026-10-27.
    """
    if array.ndim not in (2, 3):
        raise FitsError(f"only 2-D or 3-D arrays can be written, got {array.ndim}-D")

    kind = array.dtype
    if kind == np.uint16:
        bitpix, bzero = 16, 32768
        data = (array.astype(np.int32) - bzero).astype(">i2")
    elif kind == np.int16:
        bitpix, bzero = 16, 0
        data = array.astype(">i2")
    elif kind == np.uint8:
        bitpix, bzero = 8, 0
        data = array.astype(">u1")
    elif kind == np.float32:
        bitpix, bzero = -32, 0
        data = array.astype(">f4")
    elif kind == np.float64:
        bitpix, bzero = -64, 0
        data = array.astype(">f8")
    else:
        raise FitsError(f"unsupported dtype {kind}; use uint8/uint16/int16/float32/float64")

    height, width = data.shape[0], data.shape[1]
    planes = data.shape[2] if data.ndim == 3 else 1

    lines = [_card("SIMPLE", True, "conforms to FITS standard"), _card("BITPIX", bitpix, "")]
    lines.append(_card("NAXIS", 3 if planes > 1 else 2, ""))
    lines.append(_card("NAXIS1", width, "columns"))
    lines.append(_card("NAXIS2", height, "rows"))
    if planes > 1:
        lines.append(_card("NAXIS3", planes, "planes"))
    if bzero:
        lines.append(_card("BZERO", bzero, "unsigned 16-bit offset"))
    lines.append(_card("BSCALE", 1, ""))

    for key, value in (cards or {}).items():
        if len(key) > 8:
            raise FitsError(f"FITS keyword too long: {key!r}")
        lines.append(_card(key, value, (comments or {}).get(key, "")))
    lines.append(_card("EXTEND", True, ""))

    # FITS cards are fixed 80-column records with no line separators; a newline in a
    # header is illegal and makes the file unreadable by everything, including us.
    header = "".join(line.ljust(80) for line in lines) + "END".ljust(80)
    raw_header = header.encode("ascii", "replace")
    raw_header += b" " * (-len(raw_header) % _BLOCK)
    # FITS is bottom-up; numpy is top-down.
    body = np.flip(data, axis=0).tobytes()
    body += b"\0" * (-len(body) % _BLOCK)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw_header + body)
    return target
