"""The RUN loop: capture, stack, pace, and tell the truth about all three.

This is the piece brief 12 §6 says the wizard must not own. Inside RUN there is one
loop, and it does not re-enter the wizard: it captures, accumulates, and counts down to
the reframe. Re-pointing is deliberately *not* automated here (the cheap re-entry cycle
is a later pass); the loop warns instead of silently running the frame into the ground.

**Threading.** The camera is owned by :class:`ministar.ui.station.Station`, whose pump
runs live view in its own thread. RUN borrows the camera: it suspends the pump and takes
the camera lock for each capture, so EDSDK is never entered from two threads. That is
why ``capture`` is injected as a callable rather than a camera reference — the station's
lock is the only place that knows how to serialise access.

**Pacing.** A frame is due ``exposure + overhead`` after the *start* of the previous
one, so the cadence does not drift by the capture's own duration. If a capture runs long
(RAW conversion, a slow card) the loop runs flat out rather than sleeping negative time.

**Frame files.** Real frames are the session's originals and stay on disk. Fake frames
are worthless once read, so they go to a temporary directory that is removed on stop
(the user asked for exactly this: no litter in ``frames/`` from a demo).
"""

from __future__ import annotations

import base64
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ministar.camera.base import CameraSettings
from ministar.io.fits import read_fits
from ministar.run.reframe import Dwell
from ministar.stack.frames import normalise_bayer
from ministar.stack.stacker import FrameResult, LiveStack, StackStats, empty_stats
from ministar.stack.transform import FieldModel

#: Everything the camera and the disk need between exposures: card write, download, a
#: RAW conversion on the real path. Deliberately generous — a cadence that misses its
#: own schedule is worse than one that is 0.3 s slower than it could be.
PACING_OVERHEAD_S = 0.35

#: Consecutive "the mount moved" rejections before the UI is told to stop and re-solve.
#: Only reached when no resolver is wired in, or it keeps failing.
BUMP_STREAK_LIMIT = 3

#: How long to wait before trying the solver again after a failure. A hinted solve is ~1 s
#: and a blind one can be 8 s; retrying every frame on a persistently empty field would turn
#: a bad night into a slideshow of solver runs.
RESOLVE_COOLDOWN_S = 20.0

#: Below this overlap the re-solve is technically valid but the frame is mostly a new field,
#: and the user should be told rather than left with a stack that quietly stopped growing.
MIN_USEFUL_OVERLAP = 0.5

#: How long ``stop()`` waits for an in-flight exposure before giving up on the join. The
#: thread is a daemon, so a stuck camera call cannot keep the process alive.
STOP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class RunConfig:
    exposure_s: float
    iso: int = 800
    #: From the session's PARAMETERS step. ``None`` means "no reframe scheduled", which is
    #: legitimate for a short run or a target that does not drift out.
    reframe_interval_s: float | None = None
    bin_factor: int = 2
    keep_frames: bool = True
    target_name: str = ""
    #: The object, so the preview can centre on it rather than on the shrinking usable area.
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None


@dataclass(frozen=True)
class RunSnapshot:
    """Everything the RUN screen draws, immutable so a tick cannot tear."""

    state: str = "idle"
    frame: int = 0
    stats: StackStats | None = None
    data_uri: str = ""
    error: str = ""
    warning: str = ""
    elapsed_s: float = 0.0
    reframe_due_in_s: float | None = None
    frames_dir: str = ""
    kept_frames: int = 0
    #: Bumps detected and recovered by re-solving the offending frame.
    recoveries: int = 0
    #: Progress through the current reframe interval, 0..1. Drives the progress bar.
    reframe_progress: float = 0.0
    #: When the loop stopped for a reframe (wall clock), so the UI can tell a *fresh* solve
    #: from one taken before the mount was touched.
    reframe_since: datetime | None = None
    #: Whether the session named an object, which decides whether the preview *could* have
    #: centred on it and therefore whether a usable-area crop needs explaining.
    target_known: bool = False


class Runner:
    """One capture-and-stack loop. Construct, :meth:`start`, poll :meth:`snapshot`."""

    def __init__(
        self,
        *,
        capture,
        camera,
        model: FieldModel,
        config: RunConfig,
        frames_dir: Path,
        scratch_dir: Path | None = None,
        dcraw: Path | None = None,
        stack: LiveStack | None = None,
        #: ``(path, when, model) -> Solution | None``. Called when a frame's measured shift
        #: disagrees with the model; the runner rebases onto whatever comes back. Injected
        #: so the loop can be tested without ASTAP, and so this module stays solver-agnostic.
        resolve=None,
        #: Where "now" comes from. Injected for the same reason the fake camera takes one:
        #: a test can then stamp frames on the same clock as the model it built, which is
        #: what a re-solve needs (it converts the canvas through the model's absolute epoch).
        clock=None,
    ) -> None:
        self._capture = capture
        self._resolve = resolve
        self._clock = clock or (lambda: datetime.now(UTC))
        self._camera = camera
        self._config = config
        self._frames_dir = Path(frames_dir)
        self._scratch_dir = Path(scratch_dir) if scratch_dir is not None else self._frames_dir
        self._dcraw = dcraw
        #: The CFA pattern is a property of the body, not the frame. Probe it once per run
        #: instead of running ``dcraw -i -v`` on every capture.
        self._bayer: str | None = None
        self._model = model
        #: Built from the first *frame*, not from the rig profile: dcraw reports the raw
        #: sensor area (5202x3465 on the 60D) while EXIF and a solved profile can say
        #: 5184x3456, and a stacker whose grid disagrees with its frames warps to the wrong
        #: scale. The solve gives the plate scale; the header gives the dimensions.
        self._stack = stack

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._error = ""
        #: Loop events (a failed recovery, a far move). The camera-settings message is a
        #: separate notice so a recovery cannot erase it from the screen.
        self._warning = ""
        self._notice = ""
        self._frame = 0
        self._kept = 0
        self._started_at: float | None = None
        self._started_wall = datetime.now(UTC)
        #: Monotonic start of the current reframe interval. Reset by a pause/resume and by
        #: every reframe, which is what makes the bar start over after a re-point.
        self._interval_started: float | None = None
        #: The last decoded frame's dimensions, so a re-solve can be turned into a model that
        #: matches the accumulator's grid rather than the rig file's idea of it.
        self._shape: tuple[int, int] | None = None
        #: Lives on the runner, not the page: a solve triggers a page refresh, and a dwell
        #: reset every time the DOM rebuilt would never reach fifteen seconds.
        self.reframe_dwell = Dwell()
        self.reframe_since: datetime | None = None
        self._last_result: FrameResult | None = None
        self._bumps = 0
        self._recoveries = 0
        self._next_resolve_at = 0.0
        self._uri = ""
        self._uri_for: bytes | None = None
        #: Frames live in a temp directory unless the camera says they are worth keeping.
        self._temp_dir: Path | None = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        if not self._config.keep_frames:
            self._scratch_dir.mkdir(parents=True, exist_ok=True)
            self._temp_dir = Path(tempfile.mkdtemp(prefix="ministar-run-", dir=self._scratch_dir))
            self._frames_dir = self._temp_dir
        self._apply_camera_settings()
        self._started_at = time.monotonic()
        self._interval_started = self._started_at
        with self._lock:
            self._state = "running"
        self._thread = threading.Thread(target=self._loop, name="ministar-run", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._resume.clear()
        with self._lock:
            if self._state == "running":
                self._state = "paused"

    def resume(self) -> None:
        self._resume.set()
        with self._lock:
            was_reframe = self._state == "reframe"
            if self._state in {"paused", "reframe"}:
                self._state = "running"
        if was_reframe:
            # "Continue now" skips the wait but still counts as a reframe: the countdown
            # starts again, because the target has just been put back at the entry point.
            self._restart_interval()

    def resume_reframe(
        self,
        *,
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        pa_y_deg: float | None = None,
        plate_scale_arcsec: float | None = None,
    ) -> bool:
        """Resume after a reframe, rebasing the stack onto the new pointing if we have one.

        The mount has deliberately moved by most of a frame, so the old model is wrong for
        every new frame. A solve taken to confirm the aim is exactly the measurement
        :meth:`LiveStack.rebase` needs, so the re-point costs no extra solver run and the
        stack keeps every frame it already has.
        """
        rebased = False
        if (
            self._stack is not None
            and self._shape is not None
            and None not in (ra_deg, dec_deg, pa_y_deg, plate_scale_arcsec)
        ):
            model = FieldModel(
                ra_deg=float(ra_deg),
                dec_deg=float(dec_deg),
                pa_y_deg=float(pa_y_deg),
                plate_scale_arcsec=float(plate_scale_arcsec),
                width_px=self._shape[1],
                height_px=self._shape[0],
                epoch=self._clock(),
            )
            try:
                self._stack.rebase(model)
                rebased = True
            except Exception as exc:
                with self._lock:
                    self._warning = (
                        f"re-pointed, but could not rebase the stack: {type(exc).__name__}: {exc}"
                    )
        with self._lock:
            if self._state == "reframe":
                self._state = "running"
        self._restart_interval()
        self._resume.set()
        return rebased

    def _restart_interval(self) -> None:
        self._interval_started = time.monotonic()
        self.reframe_since = None
        self.reframe_dwell.reset()

    def _reframe_due(self) -> bool:
        interval = self._config.reframe_interval_s
        if not interval or self._interval_started is None:
            return False
        return time.monotonic() - self._interval_started >= interval

    def _enter_reframe(self) -> None:
        with self._lock:
            if self._state != "running":
                return
            self._state = "reframe"
            self.reframe_since = self._clock()
        self.reframe_dwell.reset()
        self._resume.clear()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_S)
        with self._lock:
            if self._state != "error":
                self._state = "stopped"
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    def restretch(self) -> None:
        if self._stack is not None:
            self._stack.restretch()

    # -- readout ------------------------------------------------------------
    def snapshot(self) -> RunSnapshot:
        stats = (
            self._stack.stats()
            if self._stack is not None
            else empty_stats(
                self._model.width_px // self._config.bin_factor,
                self._model.height_px // self._config.bin_factor,
                self._config.bin_factor,
            )
        )
        if stats.jpeg is not self._uri_for:
            self._uri_for = stats.jpeg
            self._uri = (
                "data:image/jpeg;base64," + base64.b64encode(stats.jpeg).decode("ascii")
                if stats.jpeg
                else ""
            )
        with self._lock:
            state = self._state
            error = self._error
            warning = "\n".join(part for part in (self._notice, self._warning) if part)
            frame = self._frame
            kept = self._kept
            recoveries = self._recoveries
            started = self._started_at
            since = self.reframe_since
            interval_started = self._interval_started
        elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
        interval = self._config.reframe_interval_s
        if interval and interval_started is not None:
            into_interval = max(0.0, time.monotonic() - interval_started)
            due = interval - into_interval
            progress = min(1.0, into_interval / interval)
        else:
            due = None
            progress = 0.0
        return RunSnapshot(
            state=state,
            frame=frame,
            stats=stats,
            data_uri=self._uri,
            error=error,
            warning=warning,
            elapsed_s=elapsed,
            reframe_due_in_s=due,
            reframe_progress=progress,
            reframe_since=since,
            target_known=(
                self._config.target_ra_deg is not None and self._config.target_dec_deg is not None
            ),
            frames_dir=str(self._frames_dir),
            kept_frames=kept,
            recoveries=recoveries,
        )

    # -- internals ----------------------------------------------------------
    def _apply_camera_settings(self) -> None:
        """Set ISO and shutter if the body can be asked.

        The 60D path does not implement this yet (08 §7.1, 16 §11), and pretending
        otherwise would make the run silently use whatever the dials say. So the failure is
        caught and *said out loud* in the snapshot.
        """
        apply = getattr(self._camera, "apply", None)
        if apply is None:
            return
        settings = CameraSettings(iso=self._config.iso, shutter_seconds=self._config.exposure_s)
        try:
            apply(settings)
        except NotImplementedError:
            self._notice = (
                "this camera cannot be told an exposure or ISO yet: using the dials as set"
            )
        except Exception as exc:  # a camera setting failure must not lose the loop
            self._notice = f"could not set the camera: {type(exc).__name__}: {exc}"

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._resume.is_set():
                self._resume.wait(0.1)
                continue
            if self._reframe_due():
                self._enter_reframe()
                continue
            started = time.monotonic()
            try:
                self._one_frame()
            except Exception as exc:
                with self._lock:
                    self._state = "error"
                    self._error = f"{type(exc).__name__}: {exc}"
                return
            pace = max(0.01, self._config.exposure_s + PACING_OVERHEAD_S)
            remaining = started + pace - time.monotonic()
            if remaining > 0 and self._stop.wait(remaining):
                return
        with self._lock:
            if self._state != "error":
                self._state = "stopped"

    def _one_frame(self) -> None:
        self._frame += 1
        index = self._frame
        when = self._clock()
        suffix = ".fit" if getattr(self._camera, "disposable_frames", False) else ".cr2"
        raw = self._frames_dir / f"run_{self._started_wall:%Y%m%dT%H%M%S}_{index:05d}{suffix}"
        self._capture(raw)

        # Real frames are decoded straight to an array. Writing a FITS per frame cost
        # ~0.7 s and 35 MB for a container that was read once and deleted; ASTAP reads the
        # TIFF directly and the CR2 stays as the original. Fake cameras already produce
        # the decoded file, so that path still reads it.
        tiff: Path | None = None
        if raw.suffix.lower() in {".fit", ".fits"}:
            image, header = read_fits(raw)
            bayer = normalise_bayer(header.get("BAYERPAT"))
            solve_path = raw
        else:
            if self._dcraw is None:
                raise RuntimeError(
                    "no dcraw: the camera wrote a RAW file and nothing can convert it "
                    "(see PREFLIGHT)"
                )
            from ministar.raw import decode_raw

            decoded = decode_raw(
                raw,
                dcraw=self._dcraw,
                pattern=self._bayer,
                keep_tiff=True,
            )
            image = decoded.pixels
            if decoded.pattern:
                self._bayer = decoded.pattern
            bayer = normalise_bayer(decoded.pattern or None)
            tiff = decoded.tiff
            solve_path = tiff or raw
        try:
            self._shape = (int(image.shape[0]), int(image.shape[1]))
            if self._stack is None:
                shaped = replace(
                    self._model,
                    width_px=int(image.shape[1]),
                    height_px=int(image.shape[0]),
                )
                self._stack = LiveStack(
                    shaped,
                    bin_factor=self._config.bin_factor,
                    target_ra_deg=self._config.target_ra_deg,
                    target_dec_deg=self._config.target_dec_deg,
                )
            if bayer is not None and self._stack.bayer is None:
                self._stack.bayer = bayer
            result = self._stack.add(image, when, exposure_s=self._config.exposure_s)
            if not result.accepted and result.reason.startswith("the mount moved"):
                recovered = self._try_recover(solve_path, when, image)
                if recovered is not None:
                    result = recovered
        finally:
            # The TIFF is scratch; the CR2 (when kept) is the original.
            if tiff is not None:
                tiff.unlink(missing_ok=True)
        if not self._config.keep_frames:
            raw.unlink(missing_ok=True)
        else:
            with self._lock:
                self._kept = index

        self._last_result = result
        if result.reason.startswith("the mount moved"):
            self._bumps += 1
        elif result.accepted:
            self._bumps = 0
        if self._bumps >= BUMP_STREAK_LIMIT:
            with self._lock:
                self._warning = (
                    "the mount moved and the stack is frozen: stop, re-point on POINT, "
                    "and start again"
                )

    def _try_recover(self, path: Path, when: datetime, image) -> FrameResult | None:
        """Re-solve the frame the model disagreed with, and keep it if the solve succeeds.

        The frame is already on disk and already decoded, so recovery costs one ASTAP run on
        a frame we would otherwise throw away. The accumulator keeps its canvas (see
        ``LiveStack.rebase``), which is the difference between recovering a nudge and
        starting the night over.
        """
        if self._resolve is None or self._stack is None:
            return None
        now = time.monotonic()
        if now < self._next_resolve_at:
            return None
        self._next_resolve_at = now + RESOLVE_COOLDOWN_S

        try:
            solution = self._resolve(path, when, self._stack.model)
        except Exception as exc:  # a solver failure is a message, not a crash
            with self._lock:
                self._warning = (
                    f"the mount moved and the solver failed: {type(exc).__name__}: {exc}"
                )
            return None
        if solution is None:
            with self._lock:
                self._warning = (
                    "the mount moved and that frame would not solve: stop, re-point on "
                    "POINT, and start again"
                )
            return None

        model = FieldModel(
            ra_deg=float(solution.crval[0]),
            dec_deg=float(solution.crval[1]),
            pa_y_deg=float(solution.position_angle_deg),
            plate_scale_arcsec=float(solution.pixel_scale_arcsec),
            width_px=int(image.shape[1]),
            height_px=int(image.shape[0]),
            epoch=when,
        )
        try:
            overlap = self._stack.rebase(model)
        except Exception as exc:
            with self._lock:
                self._warning = (
                    f"re-solved, but could not rebase the stack: {type(exc).__name__}: {exc}"
                )
            return None

        with self._lock:
            self._recoveries += 1
            self._warning = (
                ""
                if overlap >= MIN_USEFUL_OVERLAP
                else (
                    f"re-solved after the mount moved, but only {overlap:.0%} of the old "
                    "frame still overlaps: this is nearly a new field"
                )
            )
        return self._stack.add(image, when, exposure_s=self._config.exposure_s)


__all__ = [
    "BUMP_STREAK_LIMIT",
    "MIN_USEFUL_OVERLAP",
    "PACING_OVERHEAD_S",
    "RESOLVE_COOLDOWN_S",
    "STOP_TIMEOUT_S",
    "RunConfig",
    "RunSnapshot",
    "Runner",
]
