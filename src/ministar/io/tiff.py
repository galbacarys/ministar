"""Just enough TIFF to read a camera's EXIF out of a CR2.

The target picker needs three numbers to know what the frame covers: the pixel
pitch, the frame dimensions, and the focal length. All three live in EXIF, which
in a Canon CR2 sits in a TIFF structure (CR2 *is* TIFF with a Canon header on
front). Reading them does not justify a dependency: this is one IFD walk with a
handful of tags, in the same spirit as :mod:`ministar.io.fits`.

Scope, deliberately narrow:

* IFD0 and the Exif IFD it points to (tag ``0x8769``). MakerNotes and the Canon
  raw-specific IFDs are not walked -- nothing we need lives there.
* Both byte orders, because "any camera" means Nikon's ``MM`` as well as Canon's
  ``II``.
* Unknown tags, unknown types and truncated files yield ``None`` rather than an
  exception. A missing resolution is a legitimate outcome, not a crash: some
  bodies simply do not write ``FocalPlaneXResolution``, and the caller has to be
  able to say "unknown" instead of guessing a sensor size.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

#: TIFF field types, mapped to (struct code, size in bytes). RATIONAL is a pair of
#: LONGs, so it is handled separately.
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

# -- the tags we care about, by where they live ------------------------------
#: IFD0.
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ORIENTATION = 0x0112
TAG_EXIF_IFD = 0x8769

#: The sensor's resolution, in ExifIFD. This is the one that matters.
#: (``0x011A``/``0x011B``/``0x0128`` look similar but are ``XResolution``/
#: ``YResolution``/``ResolutionUnit`` -- the TIFF **print** resolution, whose default is
#: 72 dpi. Reading those and calling it a sensor pitch is a well-known way to be wrong
#: by a factor of 80; see the note on PLAUSIBLE_PITCH_UM.)
TAG_FOCAL_PLANE_X_RESOLUTION = 0xA20E
TAG_FOCAL_PLANE_Y_RESOLUTION = 0xA20F
TAG_FOCAL_PLANE_RESOLUTION_UNIT = 0xA210

#: TIFF/EP originals of the same three, still written by some bodies (and by Canon in
#: IFD0 alongside the ExifIFD copies). Fallbacks, not the first choice.
TAG_FOCAL_PLANE_X_RESOLUTION_EP = 0x920E
TAG_FOCAL_PLANE_Y_RESOLUTION_EP = 0x920F
TAG_FOCAL_PLANE_RESOLUTION_UNIT_EP = 0x9210

#: Exif IFD, general.
TAG_FOCAL_LENGTH = 0x920A
TAG_PIXEL_X_DIMENSION = 0xA002
TAG_PIXEL_Y_DIMENSION = 0xA003
TAG_LENS_MODEL = 0xA434
TAG_FOCAL_LENGTH_35MM = 0xA405

#: ``FocalPlaneResolutionUnit`` values (EXIF 3.0, table 10).
RESOLUTION_UNIT_NONE = 1
RESOLUTION_UNIT_INCH = 2
RESOLUTION_UNIT_CENTIMETRE = 3
RESOLUTION_UNIT_MILLIMETRE = 4
RESOLUTION_UNIT_MICROMETRE = 5


#: Plausible pixel pitches, in µm. The floor is below any production sensor (phone
#: sensors reach ~1.4 µm); the ceiling is above medium format (~14 µm).
#:
#: This bound exists because of a measured failure. A first pass at this module read
#: ``XResolution`` (``0x011A``, the print resolution, default 72 dpi) in the belief that
#: it was ``FocalPlaneXResolution``, and produced a 352.8 µm pitch and a 1828 x 1219 mm
#: sensor out of an EOS 60D -- an **82x** error in plate scale and therefore in every
#: field of view. The real tags are ``0xA20E``/``0xA20F`` (see above), which the same
#: files populate correctly at ~5728 ppi. The bound stays anyway: a missing or sentinel
#: value must be refused, not multiplied by, whatever tag it came from.
PLAUSIBLE_PITCH_UM = (1.0, 20.0)


class TiffError(ValueError):
    """The file is not readable as TIFF/CR2."""


@dataclass(frozen=True)
class FrameExif:
    """The camera facts a frame reports about itself.

    Every field is optional: absent EXIF means "unknown", never a default. The
    derived properties carry the unit conversions, so no caller has to remember
    that ``FocalPlaneXResolution`` is *pixels per unit*, not a length.
    """

    make: str = ""
    model: str = ""
    lens_model: str = ""
    focal_length_mm: float | None = None
    focal_length_35mm: int | None = None
    #: Pixels per ``resolution_unit``, as the file states it.
    focal_plane_x_resolution: float | None = None
    focal_plane_y_resolution: float | None = None
    #: One of the ``RESOLUTION_UNIT_*`` constants.
    resolution_unit: int | None = None
    pixel_x_dimension: int | None = None
    pixel_y_dimension: int | None = None
    #: EXIF orientation, 1-8. 5-8 mean the frame is stored rotated.
    orientation: int | None = None

    def _pitch_um(self, resolution: float | None) -> float | None:
        """Pixels per unit -> micrometres per pixel."""
        if resolution is None or resolution <= 0 or self.resolution_unit is None:
            return None
        mm_per_unit = {
            RESOLUTION_UNIT_INCH: 25.4,
            RESOLUTION_UNIT_CENTIMETRE: 10.0,
            RESOLUTION_UNIT_MILLIMETRE: 1.0,
            RESOLUTION_UNIT_MICROMETRE: 0.001,
        }.get(self.resolution_unit)
        if mm_per_unit is None:
            return None
        # pixels per <unit> * <mm per unit> = pixels per mm; invert for mm per pixel.
        return 1000.0 * mm_per_unit / resolution

    @property
    def pixel_size_um(self) -> float | None:
        """Horizontal pixel pitch in µm, from the focal-plane resolution.

        This is the number the whole FOV calculation rests on, so an unrecognised
        (or absent) unit returns ``None`` rather than assuming inches.
        """
        return self._pitch_um(self.focal_plane_x_resolution)

    @property
    def pixel_size_um_y(self) -> float | None:
        """Vertical pixel pitch; some bodies report a slightly different value."""
        return self._pitch_um(self.focal_plane_y_resolution) or self.pixel_size_um

    @property
    def sensor_size_mm(self) -> tuple[float, float] | None:
        """Width and height of the *used* area, in mm; ``None`` if the pitch is bogus.

        Per-axis, because a body may report X and Y resolutions that differ by a
        percent or two (the 60D writes 5728 and 5808 per inch). A sentinel pitch is
        treated as unknown rather than passed on, because anything downstream would
        happily multiply by it.
        """
        pitch_x = self.pixel_size_um
        pitch_y = self.pixel_size_um_y
        if (
            pitch_x is None
            or pitch_y is None
            or not self.pixel_x_dimension
            or not self.pixel_y_dimension
        ):
            return None
        low, high = PLAUSIBLE_PITCH_UM
        if not low <= pitch_x <= high or not low <= pitch_y <= high:
            return None
        return (
            self.pixel_x_dimension * pitch_x / 1000.0,
            self.pixel_y_dimension * pitch_y / 1000.0,
        )

    def geometry_problem(self) -> str:
        """Why the EXIF geometry cannot be used, in one sentence; empty when it can.

        Separate from ``None``: *absent* and *wrong* are different answers, and a UI
        has to say which one it got.
        """
        pitch = self.pixel_size_um
        if pitch is None:
            return "no usable focal-plane resolution in the EXIF"
        low, high = PLAUSIBLE_PITCH_UM
        if not low <= pitch <= high:
            value = self.focal_plane_x_resolution
            return (
                f"EXIF focal-plane resolution is {value:g} per unit "
                f"({pitch:.1f} um pitch), which is not a physical sensor pitch -- "
                "plate solve is needed for this body"
            )
        return ""

    @property
    def stored_rotated(self) -> bool:
        """True when the pixels are stored on their side (orientation 5-8)."""
        return self.orientation is not None and self.orientation >= 5

    def as_dict(self) -> dict[str, object]:
        """Flat, JSON-safe view, for the probe report."""
        size = self.sensor_size_mm
        return {
            "make": self.make,
            "model": self.model,
            "lens_model": self.lens_model,
            "focal_length_mm": self.focal_length_mm,
            "focal_length_35mm": self.focal_length_35mm,
            "focal_plane_x_resolution": self.focal_plane_x_resolution,
            "focal_plane_y_resolution": self.focal_plane_y_resolution,
            "resolution_unit": self.resolution_unit,
            "pixel_x_dimension": self.pixel_x_dimension,
            "pixel_y_dimension": self.pixel_y_dimension,
            "orientation": self.orientation,
            "pixel_size_um": self.pixel_size_um,
            "sensor_width_mm": None if size is None else round(size[0], 3),
            "sensor_height_mm": None if size is None else round(size[1], 3),
        }


def _read_ifd(data: bytes, offset: int, endian: str) -> dict[int, tuple[int, int, int]]:
    """One IFD as ``{tag: (type, count, value_or_offset)}``."""
    if offset <= 0 or offset + 2 > len(data):
        raise TiffError(f"IFD offset {offset} is outside the file")
    (count,) = struct.unpack_from(endian + "H", data, offset)
    entries: dict[int, tuple[int, int, int]] = {}
    for index in range(count):
        base = offset + 2 + index * 12
        if base + 12 > len(data):
            break  # truncated IFD: keep what we have rather than fail
        tag, kind, number, value = struct.unpack_from(endian + "HHII", data, base)
        entries[tag] = (kind, number, value)
    return entries


def _raw_value(data, entry: tuple[int, int, int], endian: str) -> bytes | memoryview:
    """The bytes of a field, inline if it fits in four bytes, else at its offset."""
    kind, number, value = entry
    size = _SIZES.get(kind, 0) * number
    if size == 0:
        return b""
    if size <= 4:
        return struct.pack(endian + "I", value)[:size]
    if value + size > len(data):
        return b""
    return data[value : value + size]


def _number(data, entry: tuple[int, int, int] | None, endian: str) -> float | None:
    """SHORT or LONG as a float; RATIONAL as a ratio; anything else ``None``."""
    if entry is None:
        return None
    kind, number, _ = entry
    raw = _raw_value(data, entry, endian)
    try:
        if kind == 3 and number >= 1:  # SHORT
            return float(struct.unpack_from(endian + "H", raw, 0)[0])
        if kind == 4 and number >= 1:  # LONG
            return float(struct.unpack_from(endian + "I", raw, 0)[0])
        if kind == 5 and number >= 1:  # RATIONAL
            numerator, denominator = struct.unpack_from(endian + "II", raw, 0)
            return None if denominator == 0 else numerator / denominator
    except struct.error:
        return None
    return None


def _text(data, entry: tuple[int, int, int] | None, endian: str) -> str:
    if entry is None:
        return ""
    raw = bytes(_raw_value(data, entry, endian))
    return raw.split(b"\0")[0].decode("ascii", "replace").strip()


def _exif_payload(data: bytes) -> tuple[memoryview, str]:
    """Locate the TIFF structure holding the EXIF, whatever wraps it.

    Three shapes, all seen in real files:

    * **A TIFF file** -- CR2, NEF, ARW, PEF, ORF, RW2, DNG. The header is the start.
    * **A proprietary container with an EXIF blob** -- Fuji's RAF has a
      ``FUJIFILMCCD-RAW`` header and an ``Exif\\0\\0`` + TIFF payload a little way in.
    * **A JPEG APP1 segment** -- thumbnails, and the JPEG previews inside raw files.

    Byte order is read from the payload, not assumed. Canon writes ``II``, Nikon and
    Sony ``II``, and a big-endian ``MM`` file is legal TIFF that used to fail here.
    """
    view = memoryview(data)
    if data[:2] in (b"II", b"MM"):
        return view, "<" if data[:2] == b"II" else ">"

    for identifier in (b"Exif\0\0", b"\xff\xd8\xff\xe1"):
        start = 0
        while (found := data.find(identifier, start)) != -1:
            start = found + 1
            cursor = found + len(identifier)
            if identifier.startswith(b"\xff"):
                # JPEG APP1: a 2-byte length, then the EXIF identifier, then the TIFF.
                if data[cursor : cursor + 5] != b"Exif\0":
                    continue
                cursor += 6
            header = data[cursor : cursor + 2]
            if header in (b"II", b"MM"):
                return view[cursor:], "<" if header == b"II" else ">"

    raise TiffError(f"no TIFF header, and no EXIF payload found (file starts {data[:8]!r})")


def read_frame_exif(path: Path | str) -> FrameExif:
    """Read the camera facts out of a TIFF/CR2 file.

    Raises :class:`TiffError` only when the file is not TIFF at all -- a camera
    that writes fewer tags than we would like is a normal, silent outcome.
    """
    raw_file = Path(path).read_bytes()
    if len(raw_file) < 8:
        raise TiffError("file is too short to be TIFF")
    data, endian = _exif_payload(raw_file)

    (magic,) = struct.unpack_from(endian + "H", data, 2)
    if magic != 42:  # CR2 keeps 42 here too; BigTIFF's 43 is out of scope
        raise TiffError(f"unexpected TIFF magic {magic}")

    (ifd0_offset,) = struct.unpack_from(endian + "I", data, 4)
    ifd0 = _read_ifd(data, ifd0_offset, endian)

    exif: dict[int, tuple[int, int, int]] = {}
    if TAG_EXIF_IFD in ifd0:
        exif_offset = ifd0[TAG_EXIF_IFD][2]
        try:
            exif = _read_ifd(data, exif_offset, endian)
        except TiffError:
            exif = {}

    def tag(number: int) -> tuple[int, int, int] | None:
        return exif.get(number) or ifd0.get(number)

    def pick(*candidates: tuple[str, int]) -> tuple[int, int, int] | None:
        """First entry present among ``(container, tag)`` pairs, in priority order."""
        for where, number in candidates:
            entry = (exif if where == "exif" else ifd0).get(number)
            if entry is not None:
                return entry
        return None

    orientation = _number(data, tag(TAG_ORIENTATION), endian)
    focal_35 = _number(data, tag(TAG_FOCAL_LENGTH_35MM), endian)
    # ExifIFD first, then the TIFF/EP twins (Canon writes both).
    x_resolution = pick(
        ("exif", TAG_FOCAL_PLANE_X_RESOLUTION),
        ("ifd0", TAG_FOCAL_PLANE_X_RESOLUTION_EP),
        ("exif", TAG_FOCAL_PLANE_X_RESOLUTION_EP),
    )
    y_resolution = pick(
        ("exif", TAG_FOCAL_PLANE_Y_RESOLUTION),
        ("ifd0", TAG_FOCAL_PLANE_Y_RESOLUTION_EP),
        ("exif", TAG_FOCAL_PLANE_Y_RESOLUTION_EP),
    )
    resolution_unit = pick(
        ("exif", TAG_FOCAL_PLANE_RESOLUTION_UNIT),
        ("ifd0", TAG_FOCAL_PLANE_RESOLUTION_UNIT_EP),
        ("exif", TAG_FOCAL_PLANE_RESOLUTION_UNIT_EP),
    )

    return FrameExif(
        make=_text(data, ifd0.get(TAG_MAKE), endian),
        model=_text(data, ifd0.get(TAG_MODEL), endian),
        lens_model=_text(data, tag(TAG_LENS_MODEL), endian),
        focal_length_mm=_number(data, tag(TAG_FOCAL_LENGTH), endian),
        focal_length_35mm=None if focal_35 is None else int(focal_35),
        focal_plane_x_resolution=_number(data, x_resolution, endian),
        focal_plane_y_resolution=_number(data, y_resolution, endian),
        resolution_unit=(
            None if (unit := _number(data, resolution_unit, endian)) is None else int(unit)
        ),
        pixel_x_dimension=(
            None if (px := _number(data, tag(TAG_PIXEL_X_DIMENSION), endian)) is None else int(px)
        ),
        pixel_y_dimension=(
            None if (py := _number(data, tag(TAG_PIXEL_Y_DIMENSION), endian)) is None else int(py)
        ),
        orientation=None if orientation is None else int(orientation),
    )


__all__ = [
    "RESOLUTION_UNIT_CENTIMETRE",
    "RESOLUTION_UNIT_INCH",
    "FrameExif",
    "TiffError",
    "read_frame_exif",
]
