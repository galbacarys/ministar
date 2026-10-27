"""The focus screen: live view, HFD, and the lens drive.

First real screen, in NiceGUI per brief 14 §2 -- one codebase serving both the
laptop window and the phone-browser console. Focus first because it is the only
part of the app whose behaviour is verified on hardware (brief 16).

Three rules from brief 14 shape this file:

* **Dark adaptation is a hard requirement, not a theme** (§6). Red on black, no
  white, large touch targets, readable on a dimmed phone at arm's length.
* **Nothing blocks the UI** (§4). All camera work happens in one pump thread; the
  page only ever reads an immutable snapshot. An earlier harness held the camera
  lock on the GUI thread, and a burst of clicks froze the window.
* **The client renders** (§1). The device sends a JPEG and some numbers; the
  browser does the drawing, which is what makes a cheap SBC viable.

Run it::

    python -m ministar.ui.focus            # real camera
    python -m ministar.ui.focus --fake     # synthetic stars, no hardware
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

import numpy as np
from nicegui import ui

from ministar.camera import decode_jpeg
from ministar.focus.driver import Direction, PulseResult
from ministar.focus.metrics import measure_hfd, tenengrad, to_gray

#: Pulses per pump tick. Small enough that a frame is still grabbed between
#: batches, so a long sweep keeps the preview moving instead of freezing.
PULSES_PER_TICK = 4

#: Canon needs a settle between pulses or it coalesces them.
PULSE_SETTLE_S = 0.06

#: Hard cap so a stuck key cannot grow the backlog without bound.
MAX_QUEUED = 300

#: Red on black. Bright colours are deliberately absent: at a dark site anything
#: brighter than this costs dark adaptation, and a white flash costs minutes.
CSS = """
body, .nicegui-content { background: #000000 !important; }
.mn-title { color: #ff5a4d; font-family: Consolas, monospace; font-size: 15px;
            letter-spacing: 1px; }
.mn-metrics { color: #e04a3c; font-family: Consolas, monospace; font-size: 17px;
              line-height: 1.55; white-space: pre; }
.mn-warn { color: #ff9d33; font-family: Consolas, monospace; font-size: 14px; }
.mn-preview { border: 1px solid #5a1510; background: #000000; width: 100%;
              max-width: 880px; }
.mn-drive { background: #24080a !important; color: #ff7a6a !important;
            border: 1px solid #6b1f1a !important; font-family: Consolas, monospace;
            font-size: 19px !important; font-weight: 700;
            min-width: 108px; min-height: 68px; }
.mn-drive:hover { background: #3a0d10 !important; }
"""

#: Name by direction, ordered far-to-near so the row reads like the lens barrel.
DRIVE_ROW: tuple[tuple[str, Direction], ...] = (
    ("FAR 3", Direction.FAR_3),
    ("FAR 2", Direction.FAR_2),
    ("FAR 1", Direction.FAR_1),
    ("NEAR 1", Direction.NEAR_1),
    ("NEAR 2", Direction.NEAR_2),
    ("NEAR 3", Direction.NEAR_3),
)


# ---------------------------------------------------------------------------
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
    gray = to_gray(decode_jpeg(jpeg))
    hfd, stars = measure_hfd(gray)
    return Analysis(hfd=hfd, stars=stars, contrast=tenengrad(gray), thumb=thumbnail(gray))


# ---------------------------------------------------------------------------
class FakeCamera:
    """Synthetic star field whose blur depends on position.

    Lets the screen be developed, screenshotted and tested with no hardware. The
    PSF is modelled on this rig: sigma 0.7 px at best focus, i.e. HFD ~1.75 px
    (brief 03 §3), widening linearly either side.
    """

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
        img = np.full((h, w), 200.0, dtype=np.float32)
        yy, xx = np.mgrid[0:h, 0:w]
        for cy, cx in self._stars:
            img += 2500.0 * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * sigma * sigma)))
        img += self._rng.normal(0.0, 8.0, (h, w))
        gray = np.clip(img, 0, 255).astype(np.uint8)
        ok, buffer = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:  # pragma: no cover - should not happen
            raise RuntimeError("cv2 failed to encode the synthetic frame")
        return buffer.tobytes()

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        step = direction.magnitude if direction.toward_near else -direction.magnitude
        self._position += step * max(1, count)
        return PulseResult(ok=True, moved=True, pulses_sent=max(1, count), detail="")


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Snapshot:
    """Everything the page draws. Immutable, so a tick can never tear."""

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
    """Owns the camera in exactly one thread. The UI only reads snapshots."""

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
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with contextlib.suppress(Exception):
            self._camera.disconnect()

    # -- the page's whole API ----------------------------------------------
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


# ---------------------------------------------------------------------------
_STATION: Station | None = None


@ui.page("/")
def focus_page() -> None:
    ui.dark_mode(True)
    ui.add_css(CSS)

    station = _STATION
    if station is None:  # pragma: no cover - only if imported without run()
        ui.label("station is not running").classes("mn-warn")
        return

    with ui.column().classes("w-full items-center").style("padding: 10px"):
        name_label = ui.label("").classes("mn-title")
        preview = ui.image("").classes("mn-preview")
        metrics = ui.label("").classes("mn-metrics")
        warn = ui.label("").classes("mn-warn")

        count = ui.toggle({1: "1", 3: "3", 10: "10"}, value=1).classes("mn-title")
        with ui.row().classes("items-center justify-center"):
            for label, direction in DRIVE_ROW:
                ui.button(
                    label,
                    on_click=lambda d=direction: station.request(d, count.value),
                ).classes("mn-drive").style("margin: 3px")

        ui.label("lens must be on MF (focus-by-wire)").classes("mn-title").style("opacity: 0.55")

    def tick() -> None:
        snap = station.snapshot()
        name_label.text = f"{snap.name}   frame {snap.frames}"
        if snap.data_uri:
            preview.source = snap.data_uri
            preview.update()

        if snap.stars:
            hfd_text = f"{snap.hfd:6.2f} px   ({snap.stars} stars)"
        else:
            hfd_text = "   --       (no stars usable)"

        moved = ""
        if snap.changed >= 0.0:
            moved = "   moved: " + ("YES" if snap.changed >= 2.0 else "no")

        metrics.text = (
            f"HFD      {hfd_text}\n"
            f"contrast {snap.contrast:10.2f}   (Tenengrad; NOT a focus metric)\n"
            f"position {snap.position:+5d}   pending {snap.pending:3d}{moved}"
        )
        warn.text = snap.error

    ui.timer(0.10, tick)


# ---------------------------------------------------------------------------
def run(
    *,
    fake: bool = False,
    host: str = "127.0.0.1",
    port: int = 8080,
    native: bool = False,
    show: bool = True,
) -> None:
    global _STATION

    if fake:
        camera = FakeCamera()
        initial_error = ""
    else:
        from ministar.camera import EdsdkCamera

        camera = EdsdkCamera()
        initial_error = ""
        try:
            camera.connect()
        except Exception as exc:
            initial_error = f"{type(exc).__name__}: {exc}"

    _STATION = Station(camera, initial_error=initial_error)
    _STATION.start()

    try:
        ui.run(
            host=host,
            port=port,
            title="ministar focus",
            dark=True,
            reload=False,
            show=show,
            native=native,
        )
    finally:
        _STATION.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ministar focus screen")
    parser.add_argument("--fake", action="store_true", help="synthetic stars, no camera")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--native", action="store_true", help="pywebview window instead of a browser tab"
    )
    parser.add_argument("--no-show", action="store_true", help="do not open a browser")
    args = parser.parse_args(argv)
    run(
        fake=args.fake,
        host=args.host,
        port=args.port,
        native=args.native,
        show=not args.no_show,
    )
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    raise SystemExit(main())
