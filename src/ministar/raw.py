"""RAW to FITS: the one conversion astronomy requires.

Every other part of this project speaks FITS. ``astap_cli.exe`` reads FITS, TIFF, JPEG
and PNM but **not** camera RAW (01 §9), the focus metric works on arrays, and the stacker
will too. So the CR2 is converted once, on the way in, and never consulted again.

``dcraw`` does the decoding. It ships inside the ASTAP installation (01 §9) and is GPL,
so it is driven as a subprocess and never linked -- the same relationship we have with
ASTAP itself. The flags are the ones that keep the frame as captured:

``-D``
    Document mode: no demosaic, no interpolation. A CFA plane is what a stacker wants;
    debayering here would interpolate noise into the data before calibration.
``-4``
    16 bits per sample, linear.
``-T``
    Write TIFF, which :func:`ministar.io.tiff.read_tiff_array` can read without
    needing a decompressor.
``-o 0 -r 1 1 1 1``
    Raw colour space and unity channel multipliers, so no white balance is baked in.
    Getting this wrong is silent: the frame looks plausible and is not linear.

What this does *not* do: calibration (bias/dark/flat), debayering, or stacking. Those
are separate steps with their own designs (03, 06), and all of them want this output.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ministar.io.fits import write_fits
from ministar.io.tiff import FrameExif, read_frame_exif, read_tiff_array

#: As documented in the module docstring. Order matters to dcraw.
DCRAW_ARGS = ("-4", "-D", "-T", "-o", "0", "-r", "1", "1", "1", "1")

#: FITS cards written from EXIF. Anything absent from the file is absent from the
#: header too -- a zero would claim we measured it.
_CARD_COMMENTS = {
    "INSTRUME": "camera body",
    "DATE-OBS": "capture time, local to the camera (EXIF DateTimeOriginal)",
    "EXPTIME": "exposure time, seconds",
    "ISO": "camera ISO setting",
    "FOCALLEN": "focal length, mm",
    "XPIXSZ": "pixel pitch, um",
    "YPIXSZ": "pixel pitch, um",
    "BAYERPAT": "CFA pattern of the undebayered frame",
    "XBAYROFF": "Bayer pattern X offset",
    "YBAYROFF": "Bayer pattern Y offset",
    "ORIGIN": "creator of this file",
    "SWCREATE": "converted by",
    "IMAGETYP": "frame type",
    "HISTORY": "provenance",
}


def _run(runner, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """One subprocess call, refusing to guess about failures."""
    result = runner(args, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"{Path(args[0]).name} exited {result.returncode}: "
            f"{detail[-1] if detail else 'no output'}"
        )
    return result


def bayer_pattern(*, dcraw: Path, raw: Path, runner=subprocess.run) -> str:
    """dcraw's own view of the CFA pattern, e.g. ``RGGB``; empty if it will not say."""
    try:
        result = _run(runner, [str(dcraw), "-i", "-v", str(raw)], raw.parent)
    except (RuntimeError, OSError):
        return ""
    match = re.search(r"Filter pattern:\s*([RGB]{4})", result.stdout or "")
    return match.group(1) if match else ""


def _converted_tiff(raw: Path) -> Path | None:
    """Where dcraw put its TIFF. It appends ``.tiff`` to the full input name."""
    for candidate in (raw.parent / f"{raw.name}.tiff", raw.with_suffix(".tiff")):
        if candidate.is_file():
            return candidate
    return None


def cards_from_exif(exif: FrameExif, *, pattern: str, source: str) -> dict[str, object]:
    """FITS header values derived from EXIF, omitting anything unknown."""
    cards: dict[str, object] = {
        "ORIGIN": "ministar",
        "SWCREATE": "ministar",
        "IMAGETYP": "LIGHT",
        "XBAYROFF": 0,
        "YBAYROFF": 0,
        "HISTORY": f"converted from {source} by ministar (dcraw, no demosaic)",
    }
    if exif.model:
        cards["INSTRUME"] = exif.model
    if exif.date_obs:
        cards["DATE-OBS"] = exif.date_obs
    if exif.exposure_s:
        cards["EXPTIME"] = round(exif.exposure_s, 6)
    if exif.iso:
        cards["ISO"] = exif.iso
    if exif.focal_length_mm:
        cards["FOCALLEN"] = exif.focal_length_mm
    if exif.pixel_size_um:
        cards["XPIXSZ"] = round(exif.pixel_size_um, 4)
    if exif.pixel_size_um_y:
        cards["YPIXSZ"] = round(exif.pixel_size_um_y, 4)
    if pattern:
        cards["BAYERPAT"] = pattern
    return cards


@dataclass(frozen=True)
class DecodedRaw:
    """One RAW frame as pixels, plus the CFA pattern and the scratch TIFF if kept."""

    pixels: object
    pattern: str
    tiff: Path | None = None


def decode_raw(
    raw: Path | str,
    *,
    dcraw: Path | str,
    pattern: str | None = None,
    keep_tiff: bool = False,
    runner=subprocess.run,
) -> DecodedRaw:
    """Decode a RAW to an undebayered array, without writing a FITS.

    The live loop does not need a FITS: ASTAP reads the TIFF directly and the stacker
    wants the array. Writing one per frame cost ~0.7 s and 35 MB for a container that was
    deleted in the same frame. ``pattern`` is a property of the sensor, so the caller can
    cache the first answer and avoid the extra ``dcraw -i -v`` pass on every frame.

    ``keep_tiff`` leaves the TIFF in place for a solver that runs after the read; the
    caller owns deleting it.
    """
    # Resolve first: dcraw runs with cwd=source.parent, so a relative input path
    # would be looked up *inside* that directory (probe-01/probe-01/frame.cr2).
    source = Path(raw).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    dcraw = Path(dcraw)

    _run(runner, [str(dcraw), *DCRAW_ARGS, source.name], source.parent)
    produced = _converted_tiff(source)
    if produced is None:
        raise RuntimeError(f"dcraw produced no TIFF beside {source.name}")

    try:
        pixels = read_tiff_array(produced)
        resolved_pattern = pattern
        if resolved_pattern is None:
            resolved_pattern = bayer_pattern(dcraw=dcraw, raw=source, runner=runner)
    except Exception:
        produced.unlink(missing_ok=True)
        raise

    if keep_tiff:
        return DecodedRaw(pixels=pixels, pattern=resolved_pattern, tiff=produced)
    produced.unlink(missing_ok=True)
    return DecodedRaw(pixels=pixels, pattern=resolved_pattern)


def convert_to_fits(
    raw: Path | str,
    out: Path | str | None = None,
    *,
    dcraw: Path | str,
    pattern: str | None = None,
    runner=subprocess.run,
) -> Path:
    """Convert one RAW file to FITS beside it (or at ``out``). Returns the FITS path.

    The intermediate TIFF is removed: it is a means, not an artifact, and leaving it
    would double the disk cost of a night's frames for no benefit. This is the offline
    conversion path; the live loop uses :func:`decode_raw` and keeps the CR2s.
    """
    source = Path(raw).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    target = Path(out) if out is not None else source.with_suffix(".fit")

    decoded = decode_raw(
        source, dcraw=dcraw, pattern=pattern, keep_tiff=False, runner=runner
    )

    try:
        exif = read_frame_exif(source)
    except Exception:  # a frame with no EXIF is still a frame
        exif = FrameExif()

    write_fits(
        target,
        decoded.pixels,
        cards_from_exif(
            exif, pattern=decoded.pattern, source=source.name
        ),
        comments=_CARD_COMMENTS,
    )
    return target


__all__ = [
    "DCRAW_ARGS",
    "DecodedRaw",
    "bayer_pattern",
    "cards_from_exif",
    "convert_to_fits",
    "decode_raw",
]
