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
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

import numpy as np

from ministar.camera import decode_jpeg
from ministar.focus.driver import Direction, PulseResult
from ministar.focus.metrics import measure_hfd, tenengrad, to_gray

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

    Lets every screen be developed, screenshotted and tested with no hardware. The
    PSF models this rig: sigma 0.7 px at best focus, i.e. HFD ~1.75 px (brief 03 §3).
    """

    #: Dim and unsaturated: a realistic live view, and it keeps the saturation
    #: guard from (correctly) discarding every synthetic star.
    BACKGROUND = 30.0
    STAR_PEAK = 170.0
    NOISE = 4.0

    def __init__(self, best: int = 24, shape: tuple[int, int] = (704, 1056)) -> None:
        self._position = 0
        self._best = best
        self._shape = shape
        self._rng = np.random.default_rng(1)
        h, w = shape
        self._stars = [
            (int(self._rng.integers(40, h - 40)), int(self._rng.integers(40, w - 40)))
            for _ in range(14)
        ]
        self.name = "Fake EOS (synthetic)"

    connected = True

    @property
    def position(self) -> int:
        return self._position

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def grab_live_jpeg(self) -> bytes:
        import cv2

        h, w = self._shape
        sigma = 0.7 + 0.06 * abs(self._position - self._best)
        img = np.full((h, w), self.BACKGROUND, dtype=np.float32)
        yy, xx = np.mgrid[0:h, 0:w]
        for cy, cx in self._stars:
            img += self.STAR_PEAK * np.exp(
                -(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma * sigma))
            )
        img += self._rng.normal(0.0, self.NOISE, (h, w))
        gray = np.clip(img, 0, 255).astype(np.uint8)
        ok, buffer = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, 85])
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
