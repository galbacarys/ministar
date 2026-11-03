"""Canon's EDSDK codes for ISO and shutter speed, and what they mean.

``kEdsPropID_Tv`` and ``kEdsPropID_ISOSpeed`` are **not** seconds and ISO numbers: they are
opaque codes whose mapping is absent from Canon's public headers. The tables below come from
digiCamControl's ``CanonSDKBase.cs`` (MIT), which is the reference implementation the briefs
already cite (02 §3.2, 08 §2), cross-checked against the ISO table in EDSDK-helper.

Two rules that keep this honest:

* **The table is the authority, because the body cannot enumerate.** ``EdsGetPropertyDesc``
  is unusable through this binding -- measured on the 60D, it reports ``numElements = 3`` and
  a Tv list ``[0x37, 0x10, 0x13]`` whose first entry is not a Tv code and which omits the
  body's current value (02 §"Verified on the EOS 60D"). Nothing calls it. What we get instead
  is Canon's full 1/3-stop ladder and a read-back that refuses a substitution: an ISO off
  :data:`ISO_LADDER` is refused with the list rather than quietly changed.
* **Read back after writing.** An out-of-range Tv is reported by the SDK as ``0x81``
  (DEVICE_BUSY), which is indistinguishable from a transport problem (08 §7.2b). Comparing
  the value that comes back is what tells the two apart -- and it is what caught the
  descriptor bug, because the read-back verifies the *code*, never the seconds.
"""

from __future__ import annotations

import math

#: Tv code -> seconds. The 1/3-stop ladder, longest first; bulb (0x0C) is deliberately
#: absent because v1 does not offer it (08 §6).
TV_CODE_SECONDS: dict[int, float] = {
    0x10: 30.0,
    0x13: 25.0,
    0x14: 20.0,
    0x18: 15.0,
    0x1B: 13.0,
    0x1C: 10.0,
    0x20: 8.0,
    0x23: 6.0,
    0x25: 5.0,
    0x28: 4.0,
    0x2B: 3.2,
    0x2D: 2.5,
    0x30: 2.0,
    0x33: 1.6,
    0x35: 1.3,
    0x38: 1.0,
    0x3B: 0.8,
    0x3D: 0.6,
    0x40: 0.5,
    0x43: 0.4,
    0x45: 0.3,
    0x48: 1 / 4,
    0x4B: 1 / 5,
    0x4D: 1 / 6,
    0x50: 1 / 8,
    0x53: 1 / 10,
    0x55: 1 / 13,
    0x58: 1 / 15,
    0x5B: 1 / 20,
    0x5D: 1 / 25,
    0x60: 1 / 30,
    0x63: 1 / 40,
    0x65: 1 / 50,
    0x68: 1 / 60,
    0x6B: 1 / 80,
    0x6D: 1 / 100,
    0x70: 1 / 125,
    0x73: 1 / 160,
    0x75: 1 / 200,
    0x78: 1 / 250,
    0x7B: 1 / 320,
    0x7D: 1 / 400,
    0x80: 1 / 500,
    0x83: 1 / 640,
    0x85: 1 / 800,
    0x88: 1 / 1000,
    0x8B: 1 / 1250,
    0x8D: 1 / 1600,
    0x90: 1 / 2000,
    0x93: 1 / 2500,
    0x95: 1 / 3200,
    0x98: 1 / 4000,
    0x9B: 1 / 5000,
    0x9D: 1 / 6400,
    0xA0: 1 / 8000,
}

#: ISO code -> ISO. 0x00 is Auto and is deliberately not mapped: an automatic ISO would
#: change mid-run and silently invalidate the noise model.
ISO_CODE_VALUES: dict[int, int] = {
    0x40: 50,
    0x48: 100,
    0x4B: 125,
    0x4D: 160,
    0x50: 200,
    0x53: 250,
    0x55: 320,
    0x58: 400,
    0x5B: 500,
    0x5D: 640,
    0x60: 800,
    0x63: 1000,
    0x65: 1250,
    0x68: 1600,
    0x6B: 2000,
    0x6D: 2500,
    0x70: 3200,
    0x73: 4000,
    0x75: 5000,
    0x78: 6400,
}

#: Av code -> f-number, for reading the aperture back. Setting Av is not offered yet: the
#: lens is used wide open (11 §2.1), and changing it would invalidate the flat.
AV_CODE_FNUMBERS: dict[int, float] = {
    0x08: 1.0,
    0x0B: 1.1,
    0x0C: 1.2,
    0x0D: 1.4,
    0x10: 1.4,
    0x13: 1.6,
    0x14: 1.8,
    0x15: 2.0,
    0x18: 2.0,
    0x1B: 2.2,
    0x1C: 2.5,
    0x1D: 2.8,
    0x20: 2.8,
    0x23: 3.2,
    0x24: 3.5,
    0x25: 4.0,
    0x28: 4.0,
    0x2B: 4.5,
    0x2D: 5.0,
    0x30: 5.6,
    0x33: 6.3,
    0x35: 7.1,
    0x38: 8.0,
    0x3B: 9.0,
    0x3D: 10.0,
    0x40: 11.0,
    0x43: 13.0,
    0x45: 14.0,
    0x48: 16.0,
}


def tv_seconds(code: int) -> float | None:
    """Seconds for a Tv code, or ``None`` for a value with no time (bulb)."""
    return TV_CODE_SECONDS.get(int(code))


def tv_code_for(seconds: float, *, legal: set[int] | None = None) -> int:
    """The code nearest ``seconds`` in log space, restricted to ``legal`` when given.

    Nearest-in-log because the ladder is 1/3 stops: the arithmetic midpoint of 2 s and 3.2 s
    is 2.5 s, and that is the value a photographer would name.
    """
    candidates = {
        code: value for code, value in TV_CODE_SECONDS.items() if legal is None or code in legal
    }
    if not candidates:
        raise LookupError("this body offers no usable shutter speed in the mapped range")
    target = math.log(max(1e-6, float(seconds)))
    return min(candidates, key=lambda code: abs(math.log(candidates[code]) - target))


def iso_value(code: int) -> int | None:
    """ISO for a code, or ``None`` for Auto/unknown."""
    return ISO_CODE_VALUES.get(int(code))


#: The ISO ladder this project supports, ascending. The EOS 60D's native range is 100-6400
#: in 1/3 stops; a body with a different range would add its own list here rather than have
#: the UI offer a value the camera cannot take.
ISO_LADDER: tuple[int, ...] = tuple(
    iso for _code, iso in sorted(ISO_CODE_VALUES.items()) if 100 <= iso <= 6400
)


def iso_code_for(value: int) -> int:
    """The code for ``value``, or ``ValueError`` if it is not on the ladder.

    Deliberately exact rather than "nearest": ISO 900 is not a camera setting, and silently
    substituting 800 or 1000 would put an exposure the user did not choose on every frame.
    The caller is expected to offer :data:`ISO_LADDER` in the first place.
    """
    value = int(value)
    for code, iso in ISO_CODE_VALUES.items():
        if iso == value:
            return code
    raise ValueError(f"ISO {value} is not on the supported ladder; choose from {ISO_LADDER}")


def av_fnumber(code: int) -> float | None:
    """f-number for an Av code, or ``None``."""
    return AV_CODE_FNUMBERS.get(int(code))


__all__ = [
    "AV_CODE_FNUMBERS",
    "ISO_CODE_VALUES",
    "ISO_LADDER",
    "TV_CODE_SECONDS",
    "av_fnumber",
    "iso_code_for",
    "iso_value",
    "tv_code_for",
    "tv_seconds",
]
