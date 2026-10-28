"""The camera pump, shared by every screen.

Owns the camera in exactly one thread and publishes immutable snapshots, so no
screen can block on EDSDK. This is the same shape that fixed the harness: an
earlier version called the camera on the GUI thread while holding its lock, and a
burst of clicks froze the window.

Kept separate from any screen so that several screens can share one camera
(``brief 12`` has FOCUS reading HFD and RUN reading frames).
"""

from __future__ import annotations

import base64
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from ministar.camera import decode_jpeg
from ministar.focus.driver import Direction, PulseResult
from ministar.focus.metrics import measure_hfd, tenengrad, to_gray
from ministar.focus.synth import DEFAULT_SHAPE, StarField

#: Pulses per pump tick, so a long sweep keeps the preview moving.
PULSES_PER_TICK = 4

#: Canon needs a settle between pulses or it coalesces them.
PULSE_SETTLE_S = 0.06

#: Hard cap so a stuck key cannot grow the backlog without bound.
MAX_QUEUED = 300


def thumbnail(gray: np.ndarray, size: int = 32) -> np.ndarray:
    """Coarse grayscale thumbnail, used only to answer "did the pixels move?"."""
    h, w = gray.shape
    fy, fx = h // size, w // size
    if fy < 1 or fx < 1:
        return np.zeros((size, size), dtype=np.float32)
    cropped = gray[: fy * size, : fx * size]
    return cropped.reshape(size, fy, size, fx).mean(axis=(1, 3))


@dataclass(frozen=True)
class Analysis:
    hfd: float
    stars: int
    contrast: float
    thumb: np.ndarray


def analyse(jpeg: bytes) -> Analysis:
    image = decode_jpeg(jpeg)
    gray = to_gray(image)
    # Pass the INTEGER image: saturation_level returns None for float, which would
    # silently disable saturated-star rejection and fabricate star counts.
    hfd, stars = measure_hfd(image)
    return Analysis(hfd=hfd, stars=stars, contrast=tenengrad(gray), thumb=thumbnail(gray))


class FakeCamera:
    """Synthetic star field whose blur depends on position.

    The scene itself lives in :mod:`ministar.focus.synth`, so every screen developed
    against this camera sees the same model the tests do: stars spread rather than dim,
    noise varies frame to frame, and the field is a value object that a real catalogue
    field can replace without touching this class.
    """

    def __init__(
        self,
        best: int = 24,
        shape: tuple[int, int] = DEFAULT_SHAPE,
        *,
        seed: int = 1,
        count: int = 60,
        roll_deg: float = 0.0,
        #: With a catalogue and a rig, the fake shows the *real* sky it is pointed at, which
        #: is what makes its captures solvable. Without them it shows random stars, which
        #: cannot be solved -- and says so rather than producing a mysterious failure.
        catalog=None,
        optics=None,
        pointing: tuple[float, float] | None = None,
        mag_limit: float = 11.0,
        #: Flux of a magnitude-0 star. Calibrated so a mag-11 star clears five sigma of the
        #: read noise in a 2 s frame: fainter than that and the solver correctly reports "not
        #: enough stars", which is a fake-frame artefact rather than a pipeline problem.
        flux_at_mag_zero: float = 6200.0,
    ) -> None:
        self._position = 0
        self._frames = 0
        self._best = best
        #: Camera roll as seen from behind the camera, counter-clockwise positive. Zero is
        #: the common case for a real user clamping a lens on level ground, so it is the
        #: default the roll page can be tried against.
        self._roll = float(roll_deg)
        self._catalog = catalog
        self._optics = optics
        self._pointing = pointing
        self._mag_limit = mag_limit
        self._flux_at_mag_zero = float(flux_at_mag_zero)
        self._scene: StarField | None = None
        self._scene_key: tuple | None = None
        self._field = StarField.random(count=count, seed=seed, shape=shape, best_position=best)
        self.name = "Fake EOS (synthetic)"

    #: Frames this camera writes are synthetic and worthless once solved.
    disposable_frames = True

    connected = True

    @property
    def position(self) -> int:
        return self._position

    @property
    def field(self) -> StarField:
        return self._field

    @property
    def roll_deg(self) -> float:
        return self._roll

    def set_roll(self, degrees: float) -> None:
        """Turn the fake camera. Real cameras are turned by hand; this is the stand-in.

        For the fake, the roll *is* the position angle of the image's +y axis: there is no
        ground frame to relate it to, and the projection needs exactly that quantity.
        """
        self._roll = float(degrees) % 360.0

    def set_pointing(self, ra_deg: float, dec_deg: float) -> None:
        """Aim the fake camera. A real one is aimed by the user on the tripod."""
        self._pointing = (float(ra_deg), float(dec_deg))

    @property
    def pointing(self) -> tuple[float, float] | None:
        return self._pointing

    def scene(self) -> StarField:
        """The current field, rebuilt when the pointing or roll changes."""
        if self._catalog is None or self._optics is None or self._pointing is None:
            return self._field.rotated(-self._roll)
        key = (self._pointing, round(self._roll, 3))
        if self._scene is None or self._scene_key != key:
            from ministar.projection import TangentPlane, stars_in_frame

            ra0, dec0 = self._pointing
            plane = TangentPlane(
                ra_deg=ra0,
                dec_deg=dec0,
                pa_y_deg=self._roll,
                plate_scale_arcsec=self._optics.plate_scale_arcsec,
                width_px=self._optics.width_px,
                height_px=self._optics.height_px,
            )
            radius = 0.5 * math.hypot(
                self._optics.width_arcmin / 60.0, self._optics.height_arcmin / 60.0
            )
            stars = self._catalog.stars_near(ra0, dec0, radius, mag_limit=self._mag_limit)
            x, y, mag = stars_in_frame(stars.ra_deg, stars.dec_deg, stars.mag, plane)
            self._scene = StarField.from_stars(
                x,
                y,
                mag,
                shape=(plane.height_px, plane.width_px),
                best_position=self._best,
                flux_at_mag_zero=self._flux_at_mag_zero,
            )
            self._scene_key = key
        return self._scene.rotated(0.0)

    def capture_to(self, path) -> Path:
        """Write a solvable frame, through the same API the real camera offers.

        FITS rather than RAW: the fake has no sensor to decode, and FITS is what the rest of
        the pipeline consumes, so the capture path is exercised end to end without a codec.
        No WCS is written -- the solver has to find the stars itself, which is the point.
        """
        from pathlib import Path as _Path

        from ministar.io.fits import write_fits

        scene = self.scene()
        if self._catalog is None:
            raise RuntimeError(
                "fake camera has no star catalogue, so its frames cannot be solved "
                "(point MINISTAR_ASTAP at an ASTAP install with a star database)"
            )
        image = (
            np.clip(scene.render(self._position, frame=self._frames + 1), 0, 255).astype(np.uint16)
            * 257
        )  # 8-bit dynamic range spread over 16 bits, as a real sensor would be
        cards = {
            "INSTRUME": "Fake EOS",
            "EXPTIME": 2.0,
            "IMAGETYP": "LIGHT",
            "ORIGIN": "ministar fake camera",
            "SWCREATE": "ministar",
        }
        target = _Path(path)
        return write_fits(target, image, cards)

    def render_frame(self) -> np.ndarray:
        """The scene as an 8-bit frame, rolled. Separate from JPEG so tests need no codec."""
        self._frames += 1
        turned = self._field.rotated(-self._roll)
        return turned.render_uint8(self._position, frame=self._frames)

    def iso_options(self) -> tuple[int, ...]:
        """The ISO values this body offers: powers of two x 100 up to 3200.

        The real camera will answer from ``EdsGetPropertyDesc`` once that exists (brief 08
        gap 1); until then a screen must be able to say "unknown" rather than invent a
        list, which is why this is a capability the screen *asks about*.
        """
        return (100, 200, 400, 800, 1600, 3200)

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def grab_live_jpeg(self) -> bytes:
        import cv2

        frame = self.render_frame()
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:  # pragma: no cover - should not happen
            raise RuntimeError("cv2 failed to encode the synthetic frame")
        return buffer.tobytes()

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        step = direction.magnitude if direction.toward_near else -direction.magnitude
        self._position += step * max(1, count)
        return PulseResult(ok=True, moved=True, pulses_sent=max(1, count), detail="")


@dataclass(frozen=True)
class Snapshot:
    """Everything a screen draws. Immutable, so a tick can never tear."""

    data_uri: str = ""
    hfd: float = float("nan")
    stars: int = 0
    contrast: float = 0.0
    position: int = 0
    name: str = ""
    error: str = ""
    frames: int = 0
    pending: int = 0
    #: Max thumbnail delta since the last pulse; -1 means "nothing to compare".
    changed: float = -1.0


class Station:
    """Owns the camera in exactly one thread. Screens only read snapshots."""

    def __init__(self, camera, initial_error: str = "") -> None:
        self._camera = camera
        self._lock = threading.Lock()
        self._pulses: deque[Direction] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_thumb: np.ndarray | None = None
        self._mark: np.ndarray | None = None
        self._snap = Snapshot(name=getattr(camera, "name", "camera"), error=initial_error)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        import contextlib

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with contextlib.suppress(Exception):
            self._camera.disconnect()

    # -- the screens' whole API --------------------------------------------
    @property
    def camera(self):
        """The camera this station drives. Screens use it for capture, not for frames."""
        return self._camera

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def request(self, direction: Direction, count: int = 1) -> int:
        """Queue pulses. Never touches the camera, so the click returns instantly."""
        count = max(1, int(count))
        with self._lock:
            accepted = min(count, max(0, MAX_QUEUED - len(self._pulses)))
            for _ in range(accepted):
                self._pulses.append(direction)
        return accepted

    # -- pump internals -----------------------------------------------------
    def _publish(self, **changes) -> None:
        with self._lock:
            self._snap = replace(self._snap, **changes)

    def _send_batch(self) -> None:
        for _ in range(PULSES_PER_TICK):
            with self._lock:
                if not self._pulses:
                    return
                direction = self._pulses.popleft()
                # Compare the next frame against the one from before the move.
                self._mark = self._last_thumb
            try:
                self._camera.pulse(direction)
            except Exception as exc:
                with self._lock:
                    self._pulses.clear()
                self._publish(error=str(exc))
                return
            time.sleep(PULSE_SETTLE_S)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._send_batch()
                jpeg = self._camera.grab_live_jpeg()
                analysis = analyse(jpeg)
                with self._lock:
                    mark, self._mark = self._mark, None
                    self._last_thumb = analysis.thumb
                    previous = self._snap
                    changed = -1.0 if mark is None else float(np.abs(analysis.thumb - mark).max())
                    self._snap = Snapshot(
                        data_uri="data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                        hfd=analysis.hfd,
                        stars=analysis.stars,
                        contrast=analysis.contrast,
                        position=int(getattr(self._camera, "position", 0)),
                        name=previous.name,
                        error="",
                        frames=previous.frames + 1,
                        pending=len(self._pulses),
                        changed=changed,
                    )
            except Exception as exc:
                self._publish(error=f"{type(exc).__name__}: {exc}")
                time.sleep(0.3)
