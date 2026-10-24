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

    if planes > 1:
        return np.moveaxis(array, 0, -1), header
    return array[0], header
