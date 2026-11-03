"""The camera pump, shared by every screen.

Owns the camera *session* in exactly one thread and publishes immutable snapshots, so no
screen can block on EDSDK. Kept separate from any screen so several screens can share one
camera (brief 12 has FOCUS reading HFD and RUN reading frames).

Two rounds of failure shaped this, and the second explains why the first was not enough:

1. An earlier version called the camera on the GUI thread while holding a lock, and a burst
   of clicks froze the window. The answer was a pump thread plus a camera lock.
2. The lock did not help, because EDSDK does not care about *overlap*, it cares about
   *threads*: a blocking call made off the session-owning thread never returns until that
   owner pumps ``EdsGetEvent`` (measured, see :class:`Station`). Live view had never once
   worked in the app, and a capture from the event loop wedged both threads at once.

So the rule is now structural rather than disciplinary: the pump thread opens the session,
performs every SDK call, and closes it. Everything else queues a job and waits.
"""

from __future__ import annotations

import base64
import contextlib
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ministar.camera import decode_jpeg
from ministar.focus.driver import Direction, PulseResult
from ministar.focus.metrics import measure_hfd, tenengrad, to_gray
from ministar.focus.synth import DEFAULT_SHAPE, StarField
from ministar.ui.livestretch import LiveStretch

#: How long ``stop()`` waits for the pump to close the session before saying it cannot.
STOP_TIMEOUT_S = 3.0

#: How long a queued capture may wait for the pump to run it. Generous on purpose -- a 30 s
#: exposure is not a hang -- but finite, because a stuck SDK call must become an error.
CAPTURE_WAIT_S = 180.0

#: How long a reconnect may take. Opening a session is fast (0.1 s on a healthy body); this
#: allowance covers a body that has to wake up, and still refuses to hang the button.
RECONNECT_WAIT_S = 20.0

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
    #: The decoded frame, kept so the display path does not decode the JPEG a second time.
    #: Measurement above is deliberately done on this *raw* array: a stretched frame would
    #: clip star cores to white and change the HFD it is being used to judge.
    pixels: np.ndarray | None = None
    #: The frame's own size, because HFD is in pixels and a binned live view's pixels are not
    #: the sensor's: without this an HFD cannot be converted to arcseconds, which is the only
    #: scale on which two measurements are comparable (11 §"quality gate").
    width: int = 0
    height: int = 0


def analyse(jpeg: bytes) -> Analysis:
    image = decode_jpeg(jpeg)
    height, width = image.shape[:2]
    gray = to_gray(image)
    # Pass the INTEGER image: saturation_level returns None for float, which would
    # silently disable saturated-star rejection and fabricate star counts.
    hfd, stars = measure_hfd(image)
    return Analysis(
        hfd=hfd,
        stars=stars,
        contrast=tenengrad(gray),
        thumb=thumbnail(gray),
        pixels=image,
        width=int(width),
        height=int(height),
    )


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
        site=None,
        when=None,
        #: Where "now" comes from. Injected so drift can be exercised in seconds rather than
        #: waited for, which is the difference between a test and a demo.
        clock=None,
        #: A fixed mount on a turning sky: the sky moves under the camera unless it is held.
        drift: bool = True,
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
        #: The mount is the honest state: a head on a tripod is set in altitude and azimuth,
        #: and the RA/Dec it points at is a consequence of where and when it is standing.
        self._site = site
        self._clock = clock or (lambda: datetime.now(UTC))
        #: When the mount was last touched. With drift on, the *sky* it points at is then
        #: re-derived from this mount at the current time, which is what a tripod does.
        self._set_at = when or self._clock()
        self._drift = bool(drift)
        self._altaz: tuple[float, float] | None = None
        #: How many times the station has taken this camera out of live view.
        self.live_view_stopped = 0
        if site is not None and getattr(site, "complete", False) and pointing is not None:
            self.set_pointing(*pointing)
        self.name = "Fake EOS (synthetic)"

    #: Frames this camera writes are synthetic and worthless once solved.
    disposable_frames = True

    #: The fake's ``roll`` is the position angle of the image +y axis (see ``set_roll``), not
    #: a clamp angle relative to the ground. The roll advice must therefore use it directly
    #: rather than adding the parallactic angle a second time.
    reports_position_angle = True

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

    def use_site(self, site, when=None) -> None:
        """Tell the fake where it is standing, so an altitude means something.

        The *time* is deliberately not refreshed on later calls: a mount set to an altitude
        and azimuth holds it, and the sky it points at is the same sky until a hand moves it.
        The sky drifting under a fixed mount is real, and a separate behaviour to model.
        """
        self._site = site
        if when is not None:
            self._set_at = when
        if (
            self._altaz is None
            and self._pointing is not None
            and site is not None
            and getattr(site, "complete", False)
        ):
            self.set_pointing(*self._pointing)

    @property
    def altaz(self) -> tuple[float, float] | None:
        """Where the mount is set, in degrees: altitude, azimuth from north through east."""
        return self._altaz

    @property
    def drift(self) -> bool:
        return self._drift

    def set_drift(self, drift: bool) -> None:
        """Let the sky move, or hold it still.

        Real sky by default: a mount holds its altitude and azimuth while the celestial sphere
        turns underneath, so the field crawls westward at 15.04 cos(dec) arcsec/s. Held, the
        fake is frozen for reading numbers off a screenshot -- useful, and clearly not real.
        """
        self._drift = bool(drift)
        if not self._drift:
            # Freeze where it is now, so switching off does not rewind to when it was set.
            self._set_at = self._clock()

    def drift_arcsec_per_s(self) -> float | None:
        """How fast the field sweeps the sky, in arcsec per second of angle."""
        aimed = self.pointing
        if aimed is None or self._optics is None:
            return None
        return self._optics.drift_arcsec_per_s(aimed[1])

    def drift_px_per_s(self) -> float | None:
        """And in pixels per second on this sensor, which is what moves on the screen."""
        rate = self.drift_arcsec_per_s()
        if rate is None or self._optics is None:
            return None
        return rate / self._optics.plate_scale_arcsec

    @property
    def pointing(self) -> tuple[float, float] | None:
        """Where the camera looks in RA/Dec -- derived from the mount and the clock.

        The mount is the state; the sky it points at is a consequence of the time, which is
        why this is a property rather than a stored pair. With drift on, the same altitude and
        azimuth point somewhere different every second, exactly as a tripod does.
        """
        if self._altaz is not None and self._site is not None and self._site.complete:
            from ministar import sky as sky_module

            when = self._clock() if self._drift else self._set_at
            lst = sky_module.lst_deg(when, self._site.longitude_deg)
            ra, dec = sky_module.radec_from_altaz(
                self._altaz[0], self._altaz[1], lst, self._site.latitude_deg
            )
            return float(ra) % 360.0, float(dec)
        return self._pointing

    def set_pointing(self, ra_deg: float, dec_deg: float) -> None:
        """Aim the fake camera at a sky position.

        A real user aims at an altitude and azimuth, so the conversion happens here and the
        RA/Dec kept alongside is only for the no-site case.
        """
        if self._site is not None and self._site.complete:
            from ministar import sky as sky_module

            self._set_at = self._clock()
            lst = sky_module.lst_deg(self._set_at, self._site.longitude_deg)
            alt, az = sky_module.altaz_deg(ra_deg, dec_deg, lst, self._site.latitude_deg)
            self._altaz = (float(alt), float(az) % 360.0)
        self._pointing = (float(ra_deg) % 360.0, float(dec_deg))

    def nudge_altaz(self, d_alt_deg: float = 0.0, d_az_deg: float = 0.0) -> bool:
        """Turn the altitude and azimuth knobs, as a hand on an alt-az head would.

        Altitude stops just short of the zenith (and a little below the horizon); azimuth
        wraps. Returns False when the fake does not know where it is standing, because
        without a latitude and a longitude an altitude is a number without a sky.
        """
        if self._altaz is None:
            return False
        alt, az = self._altaz
        self._altaz = (max(-5.0, min(89.9, alt + d_alt_deg)), (az + d_az_deg) % 360.0)
        return True

    def scene(self) -> StarField:
        """The current field, rebuilt when the pointing or roll changes.

        With drift on the pointing changes every access, so this rebuilds on every frame --
        4 ms against the 500 ms render, which is why exactness is affordable here and
        quantising the sky into steps would buy nothing.
        """
        aimed = self.pointing
        if self._catalog is None or self._optics is None or aimed is None:
            return self._field.rotated(-self._roll)
        key = (aimed, round(self._roll, 3))
        if self._scene is None or self._scene_key != key:
            from ministar.projection import TangentPlane, stars_in_frame

            ra0, dec0 = aimed
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
        # Advance the frame counter *before* rendering. The counter is the noise seed, so
        # leaving it fixed gave every captured frame the same noise realisation -- and
        # identical noise correlates perfectly with itself at zero shift, which silently
        # defeated every frame-to-frame measurement made on captured frames (the stacker's
        # bump detector found a zero residual no matter what the mount did). A real sensor's
        # noise is independent per frame; so is the fake's, now.
        self._frames += 1
        image = (
            np.clip(scene.render(self._position, frame=self._frames), 0, 255).astype(np.uint16)
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
        """The scene as an 8-bit frame. Separate from JPEG so tests need no codec.

        Through :meth:`scene`, not ``_field``: the live preview has to show the sky the
        camera is *pointed at*. Rendering the un-pointed field made the mount controls look
        dead -- the mount moved, the picture did not.
        """
        self._frames += 1
        return self.scene().render_uint8(self._position, frame=self._frames)

    def iso_options(self) -> tuple[int, ...]:
        """The ISO values this body offers: powers of two x 100 up to 3200.

        The real camera will answer from ``EdsGetPropertyDesc`` once that exists (brief 08
        gap 1); until then a screen must be able to say "unknown" rather than invent a
        list, which is why this is a capability the screen *asks about*.
        """
        return (100, 200, 400, 800, 1600, 3200)

    def connect(self) -> None:
        return None

    def stop_live_view(self) -> None:
        """Nothing to route: the synthetic frames cost no mirror and no battery.

        Present anyway, because "leaving live view" is part of the camera contract the station
        relies on, and a fake that cannot express it hides the call site.
        """
        self.live_view_stopped += 1

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
    #: How much the picture changed since the previous frame, in grey levels on the 32x32
    #: thumbnail: the largest change of any single cell.
    #:
    #: **Per frame, not per second.** It was per second, and that was wrong: the change is a
    #: property of the sensor and the JPEG, so dividing it by the frame interval makes the number
    #: depend on the preview rate rather than on the sky. Measured on a static field at 102 ms
    #: per frame, the per-frame change is 0.9 levels -- a quiet field -- while the same data
    #: divided by the interval reads 8.8 levels/s and looked like movement.
    #:
    #: Drift is slow and steady and a hand on the mount is neither, so this separates "the sky is
    #: moving" from "the user is still adjusting" (11 §"quality gate"). -1 = unknown.
    motion: float = -1.0
    #: The live view's pixel dimensions, for converting HFD to arcseconds.
    width: int = 0
    height: int = 0


class StationStopped(RuntimeError):
    """The station is not running, so nothing queued can be executed."""


class StationTimeout(RuntimeError):
    """The pump did not answer in time -- almost certainly a wedged SDK call."""


@dataclass
class _Job:
    """One unit of work for the pump thread.

    Screens and the run loop never touch the camera directly; they hand the pump a job and
    wait for it. That is not ceremony -- see :class:`Station`.
    """

    kind: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class Station:
    """Owns the camera session in exactly one thread; everything else queues.

    **Why one thread, and why that thread opens the session.** EDSDK on Windows is bound to
    the thread that called ``EdsInitializeSDK``/``EdsOpenSession``: a blocking call made from
    any *other* thread does not return until the owning thread pumps ``EdsGetEvent``. Measured
    on an EOS 60D (2026-10-28), same call, same body:

    | session opened on | call made from | owner pumps events | result |
    |---|---|---|---|
    | main | main | no | returns, 0.06 s |
    | main | worker | no | **never returns** |
    | main | worker | yes | returns, 0.13 s, frame after 0.7 s |

    The app used to open the session at startup and then drive live view from this pump
    thread, so *every* live-view call hung: the preview stayed blank, no exception was raised,
    and because the *event loop* also captured the shutter once, the whole console froze.
    Serialising with a lock was never going to fix that -- the calls were on the wrong thread,
    not overlapping.

    So the pump thread here opens the session (``connect=``), performs every SDK call itself
    (live view, focus pulses, captures, settings) and closes it on the way out. Other threads
    submit :class:`_Job`\\ s and wait, with a timeout, so a wedged SDK call is reported as an
    error instead of hanging a screen.
    """

    def __init__(self, camera, *, connect=None, initial_error: str = "") -> None:
        self._camera = camera
        #: Called on the pump thread, once, before anything else touches the camera.
        self._connect = connect
        self._lock = threading.Lock()
        #: Set while a run owns the camera. EDSDK dislikes live view running through a
        #: shutter release (02 §1.7), so the pump stops grabbing rather than queueing.
        self._suspended = threading.Event()
        self._pulses: deque[Direction] = deque()
        self._jobs: deque[_Job] = deque()
        #: The job the pump is running right now, if any. Tracked separately from the queue
        #: because ``stop()`` has to release a caller whose job was already picked up --
        #: otherwise a wedged SDK call leaves that thread waiting on a station that is gone.
        self._in_flight: _Job | None = None
        self._stop = threading.Event()
        #: Clear until a screen asks for a preview. A DSLR's live view raises the mirror and
        #: cycles the shutter, so the pump must not start it merely because the app launched.
        self._live = threading.Event()
        #: Whether the *camera* is currently in live view. Distinct from ``_live`` (what a
        #: screen asked for): this is the body's actual state, and it is what gets stood down.
        self._live_started = False
        #: Whether the session is open. A failed connect is not retried in a loop: the reason
        #: is the useful output, and a retry every 300 ms would overwrite it with the generic
        #: "not connected" from the next grab attempt (which is what used to happen).
        self._connected = connect is None
        self._retry = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_thumb: np.ndarray | None = None
        self._last_thumb_at: float | None = None
        #: (monotonic time, levels per frame) for the last few seconds, so a capture can ask
        #: afterwards whether the mount was moving *while* the shutter was open.
        self._motion: deque[tuple[float, float]] = deque(maxlen=240)
        self._mark: np.ndarray | None = None
        #: The display stretch for the preview. Frozen between frames (livestretch.py).
        self._stretch = LiveStretch()
        self._snap = Snapshot(name=getattr(camera, "name", "camera"), error=initial_error)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera-pump", daemon=True)
        self._thread.start()

    @property
    def thread_id(self) -> int | None:
        """The one thread allowed to touch the camera, or ``None`` before :meth:`start`.

        Exposed because it is the property that has to hold: EDSDK only progresses on the
        thread that opened the session, so tests assert that every camera call was made from
        exactly this thread.
        """
        return None if self._thread is None else self._thread.ident

    def stop(self) -> None:
        """Ask the pump to stop, and say so if it cannot.

        The session is closed *on the pump thread*, because closing it from here is the same
        wrong-thread call that hangs everything else. If the pump is stuck inside the SDK the
        join times out and the failure is published rather than hidden -- an app that will not
        die quietly is better than one that will not die at all.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_S)
            if self._thread.is_alive():
                self._publish(error="the camera did not shut down: a call is stuck in the SDK")
        self._fail_pending("the camera station stopped")

    # -- the screens' whole API --------------------------------------------
    def want_live_view(self, enabled: bool = True) -> None:
        """Ask for previews, or stand the camera down.

        Asking again is also the retry: a screen that shows a preview after a failed connect
        re-opens the session, which is what makes "turn the camera on, go back to FOCUS" work
        without restarting the console.

        ``False`` is not merely "stop showing frames": the pump leaves live view on the body
        as well, which drops a DSLR's mirror and stops the EVF stream. That is the difference
        between a warm sensor and a cold one -- live view is not this instrument's imaging
        state, it is a focusing aid, and it costs battery and dark current while it runs.
        """
        if enabled:
            self._live.set()
            self._retry.set()
        else:
            self._live.clear()

    @property
    def live_wanted(self) -> bool:
        return self._live.is_set()

    def suspend(self) -> None:
        """Hand the camera to a run loop: live view stops, captures may proceed.

        The pump stands the body down as well as pausing itself (see :meth:`_stand_down`), so a
        run images with the mirror where the shutter wants it.
        """
        self._suspended.set()
        self._forget_motion()

    def resume(self) -> None:
        """Give the camera back to live view (after a run stops)."""
        self._suspended.clear()

    @property
    def suspended(self) -> bool:
        return self._suspended.is_set()

    def _forget_motion(self) -> None:
        """Drop the change/motion history, because a gap is not a measurement.

        After a run or a still capture the next live frame describes a different moment, so
        comparing it with the one from before reads as a sudden jump (or, after a long gap,
        as no motion at all). The focus routine uses the same numbers to prove that a pulse
        was followed by a *new* frame.
        """
        with self._lock:
            self._last_thumb = None
            self._last_thumb_at = None
            self._mark = None
            self._motion.clear()

    # -- work submitted to the pump thread ---------------------------------
    def capture_to(self, path, *, wait_s: float = CAPTURE_WAIT_S, **kwargs):
        """Take one picture: the only door to the shutter.

        Blocks the *calling* thread until the pump has done it, which is what callers want:
        a capture is a request/response, not a fire-and-forget. Keeping it here rather than
        at the call site is what stops a screen from entering the SDK off the owning thread --
        which is not a race, it is a hang.
        """
        return self._run_job(_Job("capture", (path,), kwargs), wait_s=wait_s, what="capture")

    def apply(self, settings) -> None:
        """Set ISO and shutter speed, on the pump thread (the run loop calls this)."""
        self._run_job(_Job("apply", (settings,)), wait_s=RECONNECT_WAIT_S, what="camera settings")

    def get_settings(self):
        """What the dials say, read on the pump thread."""
        return self._run_job(_Job("settings"), wait_s=RECONNECT_WAIT_S, what="camera settings")

    def reconnect(self, *, wait_s: float = RECONNECT_WAIT_S) -> bool:
        """Close the session and open a new one. Returns whether it opened.

        The answer to "the camera went away": unplugged, powered off to save battery, or a
        session left in a state EDSDK will not come back from. Everything happens on the pump
        thread -- closing a session from another thread is the same wrong-thread call that
        hangs the rest (see the class note), and a reconnect that hangs would be worse than the
        failure it is meant to fix.

        The live-view *wish* is left alone: on FOCUS or POINT the pump starts previewing again
        as soon as the session is back, which is what the operator is waiting for.
        """
        return bool(self._run_job(_Job("reconnect"), wait_s=wait_s, what="reconnect"))

    @property
    def disposable_frames(self) -> bool:
        """Whether this camera's frames are worthless afterwards (the fake's are)."""
        return bool(getattr(self._camera, "disposable_frames", False))

    def _run_job(self, job: _Job, *, wait_s: float = CAPTURE_WAIT_S, what: str = "request"):
        """Queue a job for the pump thread and wait for its answer.

        A timeout is not belt-and-braces: a blocking SDK call cannot be interrupted from
        Python, so if the pump is stuck the only thing this can do is refuse to hang the
        caller with it. That is the difference between one screen showing an error and the
        whole app dying.
        """
        if self._thread is None or not self._thread.is_alive():
            raise StationStopped("the camera station is not running")
        with self._lock:
            self._jobs.append(job)
        if not job.done.wait(wait_s):
            raise StationTimeout(
                f"the camera did not answer the {what} within {wait_s:.0f} s -- it is probably "
                "stuck in the SDK; the console is still usable, but the body needs a power cycle"
            )
        if job.error is not None:
            raise job.error
        return job.result

    def _pop_job(self) -> _Job | None:
        with self._lock:
            return self._jobs.popleft() if self._jobs else None

    def _fail_pending(self, reason: str) -> None:
        """Nothing queued can ever run now, so release the waiters with an error."""
        with self._lock:
            pending = list(self._jobs)
            self._jobs.clear()
            in_flight, self._in_flight = self._in_flight, None
        for job in pending:
            job.error = StationStopped(reason)
            job.done.set()
        if in_flight is not None and not in_flight.done.is_set():
            # The pump may still be inside the SDK; the caller must not inherit that.
            in_flight.error = StationStopped(reason)
            in_flight.done.set()

    # -- snapshots ----------------------------------------------------------
    @property
    def camera(self):
        """The camera this station drives -- for *state*, never for work.

        Read-only attributes (a body that can report its roll, ``disposable_frames``) are
        what screens legitimately need. Anything that enters the SDK goes through this class:
        :meth:`capture_to`, :meth:`apply`, :meth:`request`, :meth:`want_live_view`.
        """
        return self._camera

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def restretch(self) -> None:
        """Re-measure the display stretch on the next frame."""
        self._stretch.restretch()

    def motion_since(self, since: float) -> float:
        """The largest frame-to-frame motion seen since ``since`` (monotonic seconds).

        -1 when nothing was sampled in that window, which a caller must treat as "unknown"
        rather than "still": silence is not stillness.
        """
        with self._lock:
            seen = [rate for when, rate in self._motion if when >= since]
        return max(seen) if seen else -1.0

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

    def _run(self) -> None:
        try:
            self._open()
            while not self._stop.is_set():
                if self._connect is not None and not self._connected:
                    # Nothing can work without a session. Hold the error that says why, and wait
                    # to be asked again -- a screen re-rendering is the retry.
                    self._retry.wait(0.2)
                    if self._retry.is_set():
                        self._retry.clear()
                        self._open()
                    continue
                if self._serve_job():
                    continue
                if self._suspended.is_set():
                    # A run owns the camera: no live view through a shutter release.
                    self._stand_down()
                    time.sleep(0.02)
                    continue
                if not self._live.is_set():
                    # Nobody is showing a preview (PREFLIGHT/SETUP/TARGET/PARAMETERS/DONE).
                    # Waiting here keeps the mirror down when nothing needs a picture.
                    self._stand_down()
                    self._live.wait(0.05)
                    continue
                self._grab_and_publish()
        finally:
            self._close()
            self._fail_pending("the camera station stopped")

    def _reconnect(self) -> bool:
        """Leave live view, drop the old session, open a new one -- all on this thread."""
        self._stand_down()
        self._close()
        self._live_started = False
        self._open()
        return self._connected

    def _open(self) -> None:
        """Open the session -- on this thread, which is the whole point of the class."""
        # Consume any pending "someone asked again" here: the ask that *preceded* this attempt
        # is what triggered it, and leaving the flag set would make the next park retry instantly.
        self._retry.clear()
        if self._connect is None:
            self._connected = True
            self._publish(name=getattr(self._camera, "name", "camera"))
            return
        try:
            info = self._connect()
        except Exception as exc:
            self._connected = False
            self._publish(error=f"{type(exc).__name__}: {exc}")
            return
        self._connected = True
        name = getattr(info, "name", "") or getattr(self._camera, "name", "camera")
        self._publish(name=name, error="")

    def _close(self) -> None:
        disconnect = getattr(self._camera, "disconnect", None)
        if disconnect is not None:
            with contextlib.suppress(Exception):
                disconnect()

    def _serve_job(self) -> bool:
        """Run one queued job on this thread. Returns whether there was one."""
        job = self._pop_job()
        if job is None:
            return False
        with self._lock:
            self._in_flight = job
        try:
            if job.kind == "capture":
                job.result = self._camera.capture_to(*job.args, **job.kwargs)
            elif job.kind == "apply":
                job.result = self._camera.apply(*job.args, **job.kwargs)
            elif job.kind == "settings":
                job.result = self._camera.get_settings()
            elif job.kind == "reconnect":
                job.result = self._reconnect()
            else:  # pragma: no cover - a typo in a kind is a programming error
                raise RuntimeError(f"unknown job kind {job.kind!r}")
        except BaseException as exc:
            job.error = exc
        finally:
            with self._lock:
                if self._in_flight is job:
                    self._in_flight = None
            # Whatever it did, the scene changed underneath the motion history.
            self._forget_motion()
            job.done.set()
        return True

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

    def _stand_down(self) -> None:
        """Leave live view on the body, once, when nothing is showing a preview.

        Called from the pump loop rather than from :meth:`want_live_view` because the SDK call
        belongs on this thread (see the class note) -- a caller on another thread asking for
        the mirror to drop must not be the one who drops it.
        """
        if not self._live_started:
            return
        self._live_started = False
        stop = getattr(self._camera, "stop_live_view", None)
        if stop is None:
            return
        try:
            stop()
        except Exception as exc:
            self._publish(error=f"could not leave live view: {type(exc).__name__}: {exc}")

    @property
    def connected(self) -> bool:
        """Whether the session is open (always true for a camera with no connect step)."""
        return self._connected

    @property
    def live_view_on(self) -> bool:
        """Whether the *body* is in live view (not merely whether a screen asked for it)."""
        return self._live_started

    def _grab_and_publish(self) -> None:
        try:
            self._send_batch()
            # Set before the grab: asking for a frame is what puts the body in EVF mode, and a
            # start that fails halfway still has to be undone by the stand-down path.
            self._live_started = True
            jpeg = self._camera.grab_live_jpeg()
            analysis = analyse(jpeg)
            # What the screens show is the stretched frame, never the camera's own JPEG: on a
            # dark field the raw one is a black rectangle with a few dots in it, and the
            # screen exists to let a human judge star shape.
            shown = jpeg if analysis.pixels is None else self._stretch.encode(analysis.pixels)
            now = time.monotonic()
            with self._lock:
                mark, self._mark = self._mark, None
                previous = self._snap
                changed = -1.0 if mark is None else float(np.abs(analysis.thumb - mark).max())
                motion = -1.0
                if self._last_thumb is not None and self._last_thumb_at is not None:
                    # No division by the gap: see Snapshot.motion. The interval is the *pump's*,
                    # not the sky's, and including it only made the reading a function of the
                    # preview rate.
                    motion = float(np.abs(analysis.thumb - self._last_thumb).max())
                    self._motion.append((now, motion))
                self._last_thumb = analysis.thumb
                self._last_thumb_at = now
                self._snap = Snapshot(
                    data_uri="data:image/jpeg;base64," + base64.b64encode(shown).decode("ascii"),
                    hfd=analysis.hfd,
                    stars=analysis.stars,
                    contrast=analysis.contrast,
                    position=int(getattr(self._camera, "position", 0)),
                    name=previous.name,
                    error="",
                    frames=previous.frames + 1,
                    pending=len(self._pulses),
                    changed=changed,
                    motion=motion,
                    width=analysis.width,
                    height=analysis.height,
                )
        except Exception as exc:
            self._publish(error=f"{type(exc).__name__}: {exc}")
            time.sleep(0.3)
