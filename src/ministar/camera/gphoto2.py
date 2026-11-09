"""The Canon body over libgphoto2.

This is the Linux counterpart to :mod:`ministar.camera.edsdk`. It implements the same
surface the station and the focus routine already use, so no astronomy code changes:
the seam is the whole point of the port (brief 14 section 3).

Two things are genuinely different from EDSDK and both are handled here:

* **Focus pulses do not stack.** Setting ``manualfocusdrive`` to ``Far 2`` twice only
  acts once (libgphoto2 issue #767). Every pulse is therefore followed by a ``None``
  pulse before the next one.
* **There is no position readback.** :attr:`position` is a counter of Near1-equivalents,
  exactly like the EDSDK one, and only useful for logging and for noticing that a move
  happened.

The ``gphoto2`` module is injectable (``gp=...``) so the driver can be tested without a
camera or the shared library. All calls are made on the thread that opened the camera;
libgphoto2 contexts are not safe to share across threads, and the station's pump thread
is the owner.
"""

from __future__ import annotations

import contextlib
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from ministar.camera.base import (
    CameraInfo,
    CameraSettings,
    Frame,
    label_for_seconds,
    snap_shutter,
)
from ministar.focus.driver import Direction, FocusSample, PulseResult

#: Rejected live-view frames are normal during a focus sweep; same gate as EDSDK.
MIN_STARS = 5

#: Canon's three focus tiers, in the labels libgphoto2 uses for ``manualfocusdrive``.
_FOCUS_CHOICE: dict[Direction, str] = {
    Direction.FAR_3: "Far 3",
    Direction.FAR_2: "Far 2",
    Direction.FAR_1: "Far 1",
    Direction.NEAR_1: "Near 1",
    Direction.NEAR_2: "Near 2",
    Direction.NEAR_3: "Near 3",
}

#: Preferred section for each widget: the status tree often repeats ISO and Tv, and the
#: first match is not necessarily the writable one.
_PREFERRED_SECTION = {
    "iso": "/main/imgsettings/",
    "shutterspeed": "/main/capturesettings/",
    "f-number": "/main/capturesettings/",
    # The 60D exposes the aperture under this name; some bodies use f-number.
    "aperture": "/main/capturesettings/",
    "manualfocusdrive": "/main/actions/",
    "capturetarget": "/main/settings/",
    "viewfinder": "/main/actions/",
    "mirrorlockup": "/main/capturesettings/",
    "longexpnoisereduction": "/main/capturesettings/",
}

_SUMMARY_MODEL = re.compile(r"^\s*Model:\s*(.+?)\s*$", re.MULTILINE)


class Gphoto2Unavailable(RuntimeError):
    """python-gphoto2 or libgphoto2 cannot be used on this machine."""


class Gphoto2NotConnected(RuntimeError):
    """A camera call was made before :meth:`Gphoto2Camera.connect`."""


def _normalise(text: str) -> str:
    """Choice labels vary in spacing and case between camera drivers."""
    return re.sub(r"[\s_\-]+", "", str(text)).lower()


def _widget_path(prefix: str, name: str) -> str:
    """Path for one widget; the synthetic root has no name and therefore no path."""
    if not prefix and not name:
        return ""
    return f"{prefix}/{name}" if prefix else f"/{name}"


def _as_float(value: Any) -> float | None:
    try:
        text = str(value).strip().strip('"')
    except Exception:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_iso(value: Any) -> int:
    """``'1600'``, ``'ISO 1600'`` and ``1600`` all mean 1600; ``'Auto'`` means 0."""
    number = _as_float(value)
    return int(number) if number is not None and number > 0 else 0


def parse_shutter(value: Any) -> float:
    """Seconds from a gphoto2 shutter label, or 0.0 for bulb/unknown.

    Labels are ``1/60``, ``0.5``, ``2`` and occasionally ``2"``. Bulb is deliberately
    not a number: the session cannot run at bulb (brief 08 section 6).
    """
    text = str(value).strip().strip('"').lower()
    if "bulb" in text:
        return 0.0
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return 0.0
    number = _as_float(text)
    return number if number is not None and number > 0 else 0.0


def parse_aperture(value: Any) -> float | None:
    text = str(value).strip().lower().replace("f/", "")
    number = _as_float(text)
    return number if number is not None and number > 0 else None


def decode_jpeg(jpeg: bytes) -> np.ndarray:
    """Decode a live-view frame, with Pillow as the bench-only fallback."""
    try:
        import cv2
    except ImportError:
        pass
    else:
        array = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            raise ValueError("cv2 could not decode the live-view frame")
        return array

    try:
        from io import BytesIO

        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "OpenCV is a required dependency but is not importable; install it or "
            "Pillow (ministar[bench]) as a fallback decoder"
        ) from exc
    return np.asarray(Image.open(BytesIO(jpeg)).convert("RGB"))


class Gphoto2Camera:
    """A Canon body over libgphoto2.

    ``gp`` is the imported :mod:`gphoto2` module by default and is injectable for tests.
    ``sleep`` is injectable for the same reason. The constructor never touches hardware.
    """

    def __init__(
        self,
        *,
        gp: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        settle_s: float = 0.06,
        connect_retries: int = 3,
        connect_retry_s: float = 0.5,
    ) -> None:
        self._gp = gp if gp is not None else _load_gphoto2()
        self._sleep = sleep
        self._settle_s = settle_s
        self._connect_retries = connect_retries
        self._connect_retry_s = connect_retry_s
        self._camera: Any = None
        self._config: Any = None
        self._name = ""
        self._position = 0
        self._live = False
        self._saved_capture_target: str | None = None
        self.capabilities: dict[str, str] = {}

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> CameraInfo:
        if self._camera is not None:
            raise RuntimeError("camera is already connected")

        last: Exception | None = None
        for attempt in range(max(1, self._connect_retries)):
            camera = self._gp.Camera()
            try:
                camera.init()
                summary = str(camera.get_summary())
                config = camera.get_config()
            except Exception as exc:
                last = exc
                with contextlib.suppress(Exception):
                    camera.exit()
                if attempt + 1 < self._connect_retries:
                    self._sleep(self._connect_retry_s)
                continue

            self._camera = camera
            self._config = config
            self._name = _model_name(summary)
            self._position = 0
            self._live = False
            self.capabilities = self._probe_capabilities(config)
            self._prepare_capture_target()
            return CameraInfo(name=self._name, port="")

        detail = f": {last}" if last is not None else ""
        raise Gphoto2Unavailable(
            "no camera visible to libgphoto2 (powered off, asleep, not in PTP mode, "
            "or another process holds the session)" + detail
        )

    def disconnect(self) -> None:
        camera = self._camera
        if camera is None:
            return
        # Clean up while the handle is still set: both helpers read self._camera.
        with contextlib.suppress(Exception):
            self._restore_capture_target()
        with contextlib.suppress(Exception):
            self._set_viewfinder(0)
        with contextlib.suppress(Exception):
            camera.exit()
        self._camera = None
        self._config = None
        self._live = False

    def __enter__(self) -> Gphoto2Camera:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    @property
    def connected(self) -> bool:
        return self._camera is not None

    @property
    def name(self) -> str:
        return self._name

    #: The run loop reads files the body produced; there is no in-memory frame path.
    disposable_frames = False

    def _require(self) -> Any:
        if self._camera is None:
            raise Gphoto2NotConnected("connect() the camera first")
        return self._camera

    # -- configuration helpers ---------------------------------------------
    def _refresh(self) -> Any:
        self._camera = self._require()
        self._config = self._camera.get_config()
        return self._config

    def _walk(self, root: Any):
        """Yield ``(path, widget)`` for every node, depth first."""

        def walk(widget, prefix: str):
            path = _widget_path(prefix, widget.get_name())
            yield path, widget
            if widget.count_children():
                for child in widget.get_children():
                    yield from walk(child, path)

        yield from walk(root, "")

    def _find(self, name: str, *, config: Any = None) -> tuple[str | None, Any]:
        """Find a widget by name, preferring the writable section for that name."""
        root = config if config is not None else self._config
        if root is None:
            return None, None
        preferred = _PREFERRED_SECTION.get(name)
        matches = [(path, widget) for path, widget in self._walk(root) if widget.get_name() == name]
        if not matches:
            return None, None
        if preferred:
            for path, widget in matches:
                if path.startswith(preferred):
                    return path, widget
        return matches[0]

    def _probe_capabilities(self, config: Any) -> dict[str, str]:
        found: dict[str, str] = {}
        for name in _PREFERRED_SECTION:
            path, _ = self._find(name, config=config)
            if path is not None:
                found[name] = path
        return found

    @staticmethod
    def _choices(widget: Any) -> list[str]:
        try:
            return [str(widget.get_choice(i)) for i in range(widget.count_choices())]
        except Exception:
            return []

    @classmethod
    def _choice_matching(cls, widget: Any, desired: str) -> str | None:
        target = _normalise(desired)
        choices = cls._choices(widget)
        for choice in choices:
            if _normalise(choice) == target:
                return choice
        for choice in choices:
            if target in _normalise(choice):
                return choice
        return None

    def _set_widget(self, widget: Any, value: Any) -> None:
        widget.set_value(value)

    # -- settings -----------------------------------------------------------
    def iso_options(self) -> tuple[int, ...]:
        _, widget = self._find("iso")
        values = (
            {parse_iso(choice) for choice in self._choices(widget)} if widget is not None else set()
        )
        return tuple(sorted(value for value in values if value > 0))

    def get_settings(self) -> CameraSettings:
        config = self._refresh()
        _, iso_widget = self._find("iso", config=config)
        _, tv_widget = self._find("shutterspeed", config=config)
        _, av_widget = self._find("aperture", config=config)
        if av_widget is None:
            _, av_widget = self._find("f-number", config=config)
        iso = parse_iso(iso_widget.get_value()) if iso_widget is not None else 0
        tv = parse_shutter(tv_widget.get_value()) if tv_widget is not None else 0.0
        av = parse_aperture(av_widget.get_value()) if av_widget is not None else None
        return CameraSettings(iso=iso, shutter_seconds=tv, aperture=av)

    def apply(self, settings: CameraSettings) -> None:
        """Write ISO/Tv/Av using the body's own choice lists, then read them back.

        gphoto2, like EDSDK, can silently clamp an illegal value. The read-back is the
        only way to know that the exposure on every frame matches the arithmetic.
        """
        config = self._refresh()

        _, iso_widget = self._find("iso", config=config)
        if iso_widget is None:
            raise RuntimeError("the body exposes no ISO control")
        iso_target = settings.iso
        iso_choices = sorted(
            value
            for value in {parse_iso(choice) for choice in self._choices(iso_widget)}
            if value > 0
        )
        if iso_choices:
            iso_target = min(iso_choices, key=lambda value: abs(value - settings.iso))
        iso_choice = self._choice_matching(iso_widget, str(iso_target))
        if iso_choice is None:
            raise RuntimeError(f"ISO {iso_target} is not one of {self._choices(iso_widget)}")
        self._set_widget(iso_widget, iso_choice)

        tv_target = snap_shutter(settings.shutter_seconds)
        _, tv_widget = self._find("shutterspeed", config=config)
        if tv_widget is None:
            raise RuntimeError("the body exposes no shutter-speed control")
        tv_choice = self._closest_choice(tv_widget, tv_target, parse_shutter, log=True)
        if tv_choice is None:
            raise RuntimeError(
                f"shutter {label_for_seconds(tv_target)} is not one of {self._choices(tv_widget)}"
            )
        self._set_widget(tv_widget, tv_choice)

        if settings.aperture is not None:
            _, av_widget = self._find("aperture", config=config)
            if av_widget is None:
                _, av_widget = self._find("f-number", config=config)
            if av_widget is not None:
                av_choice = self._closest_choice(av_widget, settings.aperture, parse_aperture)
                if av_choice is not None:
                    self._set_widget(av_widget, av_choice)

        self._camera.set_config(config)

        readback = self.get_settings()
        if readback.iso != iso_target:
            raise RuntimeError(
                f"the camera did not accept ISO {iso_target}: read back {readback.iso} "
                "(is the mode dial set to M?)"
            )
        if tv_target > 0 and abs(readback.shutter_seconds - tv_target) > max(
            1e-4, tv_target * 0.01
        ):
            raise RuntimeError(
                f"the camera did not accept shutter {label_for_seconds(tv_target)}: "
                f"read back {readback.shutter_seconds:.4f}s"
            )

    def _closest_choice(
        self,
        widget: Any,
        value: float,
        parser: Callable[[Any], float | None],
        *,
        log: bool = False,
    ) -> str | None:
        """The choice whose parsed value is nearest; Tv is nearest in log space."""
        parsed: list[tuple[str, float]] = []
        for choice in self._choices(widget):
            number = parser(choice)
            if number is not None and number > 0:
                parsed.append((choice, number))
        if not parsed:
            return None
        if log:
            return min(parsed, key=lambda item: abs(math.log(item[1]) - math.log(value)))[0]
        return min(parsed, key=lambda item: abs(item[1] - value))[0]

    # -- capture and live view ---------------------------------------------
    def capture_to(self, path: Path, *, timeout_s: float = 120.0) -> Path:
        """Take a picture and download it to ``path``.

        ``timeout_s`` is accepted for interface parity with EDSDK but cannot be enforced:
        a blocking libgphoto2 call has no cancellation. The station's own job timeout is
        the outer guard; a wedged body needs ``gphoto2 --reset`` or a replug.
        """
        camera = self._require()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        camera_file = camera.capture(self._gp.GP_CAPTURE_IMAGE)
        downloaded = camera.file_get(
            camera_file.folder, camera_file.name, self._gp.GP_FILE_TYPE_NORMAL
        )
        downloaded.save(str(target))
        return target

    def capture(self, exposure_s: float) -> Frame:
        raise NotImplementedError(
            "capture() returns a decoded array and no raw decoder is wired in; use "
            "capture_to(path) for the file the camera produced"
        )

    def grab_live_jpeg(self) -> bytes:
        camera = self._require()
        camera_file = camera.capture_preview()
        data = camera_file.get_data_and_size()
        self._live = True
        return bytes(memoryview(data))

    def live_view(self) -> Frame:
        return Frame(pixels=decode_jpeg(self.grab_live_jpeg()), is_preview=True)

    def stop_live_view(self) -> None:
        self._live = False
        if self._camera is None:
            return
        self._set_viewfinder(0)

    def _set_viewfinder(self, value: int) -> None:
        """Turn the EVF stream off; harmless when the widget does not exist."""
        if self._camera is None:
            return
        config = self._camera.get_config()
        _, widget = self._find("viewfinder", config=config)
        if widget is None:
            return
        choices = self._choices(widget)
        if choices:
            wanted = "1" if value else "0"
            off = next(
                (
                    choice
                    for choice in choices
                    if _normalise(choice) in {_normalise(wanted), "off", "disable"}
                ),
                None,
            )
            if off is not None:
                widget.set_value(off)
                self._camera.set_config(config)
            return
        with contextlib.suppress(Exception):
            widget.set_value(int(value))
            self._camera.set_config(config)

    def keep_alive(self) -> None:
        """No-op: libgphoto2/the body manages its own power timer."""
        return None

    # -- capture target -----------------------------------------------------
    def _prepare_capture_target(self) -> None:
        """Prefer Internal RAM so a dark-site session never depends on the card."""
        config = self._config
        _, widget = self._find("capturetarget", config=config)
        if widget is None:
            return
        choices = self._choices(widget)
        wanted = next(
            (
                choice
                for choice in choices
                if _normalise(choice) in {"internalram", "ram", "sdram", "internalmemory"}
            ),
            None,
        )
        if wanted is None:
            return
        try:
            self._saved_capture_target = str(widget.get_value())
            widget.set_value(wanted)
            self._camera.set_config(config)
        except Exception:
            self._saved_capture_target = None

    def _restore_capture_target(self) -> None:
        if self._camera is None or self._saved_capture_target is None:
            return
        config = self._camera.get_config()
        _, widget = self._find("capturetarget", config=config)
        if widget is not None:
            widget.set_value(self._saved_capture_target)
            self._camera.set_config(config)
        self._saved_capture_target = None

    # -- FocusDriver --------------------------------------------------------
    @property
    def position(self) -> int:
        return self._position

    def reset_position(self, position: int = 0) -> None:
        self._position = int(position)

    def pulse(self, direction: Direction, count: int = 1) -> PulseResult:
        """Send focus pulses, alternating every pulse with ``None``.

        Without the alternation the body applies the first call and ignores the rest
        (libgphoto2 #767); the motion planner's ``Near3 x 419`` would otherwise move
        once. The pulse is also why this cannot be batched.
        """
        if self._camera is None:
            return PulseResult(ok=False, moved=False, pulses_sent=0, detail="not connected")
        config = self._camera.get_config()
        _, widget = self._find("manualfocusdrive", config=config)
        if widget is None:
            return PulseResult(
                ok=False,
                moved=False,
                pulses_sent=0,
                detail="the body exposes no manualfocusdrive action (autofocus only?)",
            )

        choice = self._choice_matching(widget, _FOCUS_CHOICE[direction])
        if choice is None:
            return PulseResult(
                ok=False,
                moved=False,
                pulses_sent=0,
                detail=f"{_FOCUS_CHOICE[direction]} is not one of {self._choices(widget)}",
            )
        idle = self._choice_matching(widget, "None")
        if idle is None:
            return PulseResult(
                ok=False,
                moved=False,
                pulses_sent=0,
                detail=f"no 'None' choice in {self._choices(widget)}; pulses cannot alternate",
            )

        step = direction.magnitude if direction.toward_near else -direction.magnitude
        count = max(1, int(count))
        sent = 0
        for _ in range(count):
            try:
                widget.set_value(choice)
                self._camera.set_config(config)
                widget.set_value(idle)
                self._camera.set_config(config)
            except Exception as exc:
                return PulseResult(
                    ok=False,
                    moved=sent > 0,
                    pulses_sent=sent,
                    detail=f"{type(exc).__name__}: {exc} (is the lens switch on MF?)",
                )
            sent += 1
            self._position += step
            if self._settle_s:
                self._sleep(self._settle_s)
        return PulseResult(ok=True, moved=True, pulses_sent=sent, detail="")

    def measure(
        self,
        *,
        decode: Callable[[bytes], np.ndarray] | None = None,
        min_stars: int = MIN_STARS,
    ) -> FocusSample:
        """Grab a live-view frame and measure star sharpness, like the EDSDK path."""
        if self._camera is None:
            return FocusSample(
                position=self._position,
                hfd=float("nan"),
                star_count=0,
                accepted=False,
                reason="not connected",
            )
        try:
            jpeg = self.grab_live_jpeg()
        except Exception as exc:
            return FocusSample(
                position=self._position,
                hfd=float("nan"),
                star_count=0,
                accepted=False,
                reason=f"{type(exc).__name__}: {exc}",
            )
        if not jpeg:
            return FocusSample(
                position=self._position,
                hfd=float("nan"),
                star_count=0,
                accepted=False,
                reason="empty live-view frame",
            )
        from ministar.focus.metrics import measure_hfd

        image = (decode or decode_jpeg)(jpeg)
        hfd, stars = measure_hfd(image)
        accepted = stars >= min_stars and math.isfinite(hfd)
        if accepted:
            reason = ""
        elif stars < min_stars:
            reason = f"too few usable stars ({stars} < {min_stars})"
        else:
            reason = "median HFD was not finite"
        return FocusSample(
            position=self._position,
            hfd=float(hfd),
            star_count=int(stars),
            accepted=accepted,
            reason=reason,
        )


def _load_gphoto2() -> Any:
    try:
        import gphoto2
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise Gphoto2Unavailable(
            "python-gphoto2 is not installed. On the Pi: `sudo apt install "
            "python3-gphoto2`, or `pip install gphoto2` in the venv."
        ) from exc
    return gphoto2


def _model_name(summary: str) -> str:
    match = _SUMMARY_MODEL.search(summary or "")
    if match:
        return match.group(1).strip()
    for line in (summary or "").splitlines():
        if line.lower().startswith("model"):
            return line.split(":", 1)[-1].strip()
    return "Canon"


__all__ = [
    "Gphoto2Camera",
    "Gphoto2NotConnected",
    "Gphoto2Unavailable",
    "decode_jpeg",
    "parse_aperture",
    "parse_iso",
    "parse_shutter",
]
