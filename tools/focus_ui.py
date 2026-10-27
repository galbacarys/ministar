"""Bench test harness: drive the lens and watch sharpness. NOT the product UI.

Run on Windows with the camera attached:

    python tools\\focus_ui.py            # GUI
    python tools\\focus_ui.py --selftest # headless: prove the drive works

Two sharpness signals, deliberately:

* **HFD** from ``ministar.focus.metrics`` -- the real metric, but it needs a star
  field. On a bench in daylight it will report "no stars" and that is expected.
* **Tenengrad score** -- mean squared gradient over the frame. Always available,
  so on any textured indoor target you can still see the number rise and fall as
  focus moves. This is what makes the harness useful without a night sky.

The GUI keeps one worker thread doing live view (so the window never freezes on
``EdsDownloadEvfImage``) and serialises every EDSDK call behind a single lock.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Make the repo's src importable without installing.
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from edsdk_lite import (  # noqa: E402
    FAR_STEPS,
    NEAR_STEPS,
    Edsdk,
    EdsdkError,
)

from ministar.focus.metrics import measure_hfd  # noqa: E402

#: Canon needs a settle between pulses or it coalesces them.
PULSE_SETTLE_S = 0.06

#: Pulses sent per worker tick. Small enough that a live-view frame is still
#: grabbed between batches, so the preview keeps moving during a long sweep
#: instead of freezing until it finishes.
PULSES_PER_TICK = 4

#: Hard cap on queued pulses, so a stuck key cannot grow the backlog without
#: bound. The UI reports when this is hit rather than silently dropping.
MAX_QUEUED_PULSES = 300


@dataclass(frozen=True)
class Metrics:
    width: int
    height: int
    hfd: float
    stars: int
    tenengrad: float
    jpeg: bytes
    #: 32x32 grayscale thumbnail, used to answer "did the pixels move at all?".
    #: Independent of whether the scene is sharp enough for a focus metric, which
    #: is the only question that survives a lens cap or a blank wall.
    thumb: np.ndarray


def tenengrad(gray: np.ndarray) -> float:
    """Mean squared gradient magnitude. Rises to a maximum at best focus."""
    gy, gx = np.gradient(gray)
    return float(np.mean(gx * gx + gy * gy))


def analyse(jpeg: bytes) -> Metrics:
    from PIL import Image

    img = Image.open(io.BytesIO(jpeg))
    gray_img = img.convert("L")
    gray = np.asarray(gray_img, dtype=np.float32)

    # HFD wants a float image in ADU-ish units; a uint8 grayscale works.
    hfd, stars = measure_hfd(gray)

    thumb = np.asarray(gray_img.resize((32, 32), Image.BILINEAR), dtype=np.float32)

    return Metrics(
        width=img.width,
        height=img.height,
        hfd=hfd,
        stars=stars,
        tenengrad=tenengrad(gray),
        jpeg=jpeg,
        thumb=thumb,
    )


class Controller:
    """Owns the camera, one lock, and the latest frame."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.edsdk: Edsdk | None = None
        self.latest: Metrics | None = None
        self.error: str = ""
        self.running = threading.Event()
        self._thread: threading.Thread | None = None
        #: Snapshot taken immediately before a drive, so the UI can show whether
        #: the image actually changed afterwards.
        self.drive_mark: np.ndarray | None = None
        self.drive_mark_sharp: float | None = None
        #: Pending pulses. The UI thread only appends; the worker drains a few
        #: per tick. This is what keeps clicking from blocking the window.
        self._pulses: deque[int] = deque()
        self.dropped = 0
        self.frame_count = 0
        self.last_frame_at = 0.0
        self.pulses_sent = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> str:
        edsdk = Edsdk()
        edsdk.initialize()
        devices = edsdk.list_cameras()
        if not devices:
            raise RuntimeError("no camera visible to EDSDK")
        with self.lock:
            self.edsdk = edsdk
            info = edsdk.open_camera(0)
            edsdk.start_live_view()
        self.running.set()
        self._thread = threading.Thread(target=self._pump, name="liveview", daemon=True)
        self._thread.start()
        return info.description

    def stop(self) -> None:
        self.running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with self.lock:
            if self.edsdk is not None:
                self.edsdk.stop_live_view()
                self.edsdk.close()
                self.edsdk = None

    def _pump(self) -> None:
        last_keepalive = time.monotonic()
        while self.running.is_set():
            try:
                # Send a bounded batch first, then a frame. Doing it in this
                # order means a 300-pulse sweep still updates the preview live.
                self._send_batch()

                with self.lock:
                    if self.edsdk is None or not self.running.is_set():
                        return
                    jpeg = self.edsdk.grab_jpeg()
                    now = time.monotonic()
                    if now - last_keepalive > 30.0:
                        self.edsdk.keep_alive()
                        last_keepalive = now
                if jpeg:
                    self.latest = analyse(jpeg)
                    self.frame_count += 1
                    self.last_frame_at = time.monotonic()
                    self.error = ""
            except EdsdkError as exc:
                self.error = str(exc)
                time.sleep(0.25)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                time.sleep(0.25)

    def _send_batch(self) -> None:
        for _ in range(PULSES_PER_TICK):
            if not self._pulses:
                return
            with self.lock:
                if self.edsdk is None:
                    self._pulses.clear()
                    return
                param = self._pulses.popleft()
                self.edsdk.drive_lens(param)
            self.pulses_sent += 1
            time.sleep(PULSE_SETTLE_S)

    # -- focus --------------------------------------------------------------
    def drive(self, near: bool, tier: int, count: int) -> str:
        steps = NEAR_STEPS if near else FAR_STEPS
        param = steps[max(1, min(3, tier)) - 1]
        label = ("Near" if near else "Far") + str(tier)

        latest = self.latest
        if latest is not None:
            self.drive_mark = latest.thumb.copy()
            self.drive_mark_sharp = latest.tenengrad

        # Never touches the camera here: this is the Tk thread. Enqueue and return.
        room = MAX_QUEUED_PULSES - len(self._pulses)
        n = max(1, count)
        accept = min(n, max(0, room))
        for _ in range(accept):
            self._pulses.append(param)
        if accept < n:
            self.dropped += n - accept
            return f"queued {label} x{accept} of {n} (backlog full, {self.dropped} dropped)"
        return f"queued {label} x{n}   (pending: {len(self._pulses)})"

    @property
    def pending(self) -> int:
        return len(self._pulses)


# ---------------------------------------------------------------------------
def scene_stats(jpeg: bytes) -> dict:
    """Is there anything in the frame worth focusing on?

    A near-uniform scene makes every focus metric meaningless, so report the
    contrast budget up front instead of letting a noise metric masquerade as a
    result.
    """
    from PIL import Image

    gray = np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"), dtype=np.float32)
    return {
        "mean": float(gray.mean()),
        "std": float(gray.std()),
        "p99": float(np.percentile(gray, 99)),
        "tenengrad": tenengrad(gray),
    }


def diagnose() -> int:
    """Noise-aware drive test.

    Establishes the frame-to-frame spread of the metric with the lens untouched,
    then moves the lens a lot and asks whether the change exceeds that spread.
    Without this, a flat scene reads as "no effect" and a noisy metric reads as
    a result -- both wrong for different reasons.
    """
    edsdk = Edsdk()
    try:
        edsdk.initialize()
        info = edsdk.open_camera(0)
        print(f"camera: {info.description}")
        print(f"lens  : {edsdk.string_property(0x0000040D)!r}")
        edsdk.start_live_view()

        print("\nwarming up live view...")
        jpeg = edsdk.grab_jpeg()
        stats = scene_stats(jpeg)
        print(
            f"scene: mean={stats['mean']:.1f} std={stats['std']:.2f} "
            f"p99={stats['p99']:.0f} tenengrad={stats['tenengrad']:.3f}"
        )
        if stats["std"] < 6.0:
            print("  !! frame is nearly uniform -- focus metrics cannot work on this")
            print("     point the camera at a textured target or the night sky")

        print("\nmeasuring metric noise floor (6 frames, lens untouched)...")
        idle = [scene_stats(edsdk.grab_jpeg()) for _ in range(6)]
        t_idle = np.array([s["tenengrad"] for s in idle])
        h_idle = np.array([analyse(edsdk.grab_jpeg()).hfd for _ in range(1)])
        base = float(t_idle.mean())
        noise = float(t_idle.std())
        print(f"  tenengrad = {base:.4f} +/- {noise:.4f} (std over {len(t_idle)} frames)")
        print(f"  hfd sample = {h_idle[0]:.3f}")

        for label, steps, times in (
            ("Near3 x30", NEAR_STEPS, 30),
            ("Far3 x60", FAR_STEPS, 60),
        ):
            print(f"\ndriving {label} ...")
            for _ in range(times):
                edsdk.drive_lens(steps[2])
                time.sleep(PULSE_SETTLE_S)
            time.sleep(0.4)
            after = [scene_stats(edsdk.grab_jpeg()) for _ in range(3)]
            t_after = np.array([s["tenengrad"] for s in after])
            delta = float(t_after.mean()) - base
            sigma = max(noise, 1e-9)
            print(
                f"  tenengrad = {t_after.mean():.4f} +/- {t_after.std():.4f}  "
                f"delta={delta:+.4f}  ({abs(delta) / sigma:.1f} sigma)"
            )
            if abs(delta) > 5 * sigma:
                print("  => MOVED (change far exceeds metric noise)")
            else:
                print("  => no significant change")
            base = float(t_after.mean())
            noise = max(float(t_after.std()), 1e-9)

        print("\nInterpretation:")
        print("  MOVED on both sweeps            -> drive works")
        print("  no change, scene std was low    -> scene has no focus content; retest on texture")
        print(
            "  no change, scene std was fine   -> lens refusing the drive "
            "(AF/MF switch or STM limit)"
        )
        return 0
    except EdsdkError as exc:
        print(f"EDSDK error: {exc}")
        return 3
    finally:
        with contextlib.suppress(Exception):
            edsdk.stop_live_view()
        edsdk.close()


def stress(n_pulses: int = 60) -> int:
    """Reproduce the fast-clicking overload and prove it no longer hangs.

    Fires a burst of drive requests as fast as the UI thread could, then checks
    that (a) enqueueing was instant, (b) the queue drains, and (c) live-view
    frames keep arriving *during* the drain. Point (c) is the one that failed
    before: the old code held the camera lock while sleeping, so nothing updated.
    """
    controller = Controller()
    try:
        info = controller.start()
        print(f"camera: {info}")
        deadline = time.time() + 8
        while controller.latest is None and time.time() < deadline:
            time.sleep(0.05)
        if controller.latest is None:
            print("live view never produced a frame")
            return 2
        print("live view up")

        for label, near in (("Near3", True), ("Far3", False)):
            t0 = time.time()
            for _ in range(n_pulses):
                controller.drive(near, 3, 1)
            click_ms = (time.time() - t0) * 1000
            print(
                f"\nenqueued {n_pulses}x {label} in {click_ms:.1f} ms "
                f"(pending={controller.pending}, dropped={controller.dropped})"
            )
            if click_ms > 50:
                print("  !! enqueueing was slow; the UI thread would stutter")

            frames_before = controller.frame_count
            t0 = time.time()
            while controller.pending and time.time() - t0 < 90:
                time.sleep(0.1)
            drain_s = time.time() - t0
            frames = controller.frame_count - frames_before
            print(f"  drained in {drain_s:.1f}s, frames delivered meanwhile: {frames}")
            print(f"  error: {controller.error or '(none)'}")
            if controller.pending:
                print(f"  !! still {controller.pending} pending -- stalled")
                return 3
            if frames < 5:
                print("  !! live view starved during the move")
                return 4

        print(f"\ntotal pulses sent: {controller.pulses_sent}")
        print("PASS: bursts never blocked the UI or starved live view")
        return 0
    finally:
        controller.stop()


def selftest() -> int:
    print("focus driver selftest")
    print("=" * 62)
    edsdk = Edsdk()
    try:
        edsdk.initialize()
        devices = edsdk.list_cameras()
        print(f"cameras: {[d.description for d in devices]}")
        if not devices:
            print("no camera")
            return 2

        info = edsdk.open_camera(0)
        print(f"session open: {info.description}")
        for prop, label in (
            (0x00000002, "ProductName"),
            (0x0000040D, "LensName"),
        ):
            try:
                print(f"  {label:12s} = {edsdk.string_property(prop)!r}")
            except EdsdkError as exc:
                print(f"  {label:12s} unavailable ({exc})")

        edsdk.start_live_view()
        print("live view started")

        samples = []
        for i in range(3):
            jpeg = edsdk.grab_jpeg()
            m = analyse(jpeg)
            samples.append(m)
            print(
                f"  frame {i}: {m.width}x{m.height} jpeg={len(jpeg)}B "
                f"tenengrad={m.tenengrad:8.2f} hfd={m.hfd:.3f} stars={m.stars}"
            )
        base = samples[-1].tenengrad

        print("\ndriving Near3 x5 ...")
        for _ in range(5):
            edsdk.drive_lens(NEAR_STEPS[2])
            time.sleep(PULSE_SETTLE_S)
        time.sleep(0.35)
        after_near = analyse(edsdk.grab_jpeg())
        print(
            f"  tenengrad={after_near.tenengrad:8.2f}  "
            f"hfd={after_near.hfd:.3f} stars={after_near.stars}"
        )

        print("driving Far3 x5 (returning) ...")
        for _ in range(5):
            edsdk.drive_lens(FAR_STEPS[2])
            time.sleep(PULSE_SETTLE_S)
        time.sleep(0.35)
        after_far = analyse(edsdk.grab_jpeg())
        print(
            f"  tenengrad={after_far.tenengrad:8.2f}  "
            f"hfd={after_far.hfd:.3f} stars={after_far.stars}"
        )

        print()
        changed = abs(after_near.tenengrad - base)
        rel = changed / base if base else 0.0
        print(f"change on Near3 x5 : {changed:.2f} ({rel * 100:.1f}%)")
        if rel < 0.002:
            print("VERDICT: drive had NO measurable effect.")
            print("  -> check the lens AF/MF switch is on MF")
            print("  -> lens may be at a focus stop")
            print("  -> or the body rejected DriveLensEvf (no error was raised)")
        else:
            print("VERDICT: drive changed the image -> DriveLensEvf works.")
        return 0
    except EdsdkError as exc:
        print(f"EDSDK error: {exc}")
        return 3
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")
        return 4
    finally:
        with contextlib.suppress(Exception):
            edsdk.stop_live_view()
        edsdk.close()


# ---------------------------------------------------------------------------
def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox

    from PIL import Image, ImageTk

    controller = Controller()
    try:
        description = controller.start()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("ministar focus test", f"Could not start camera:\n{exc}")
        root.destroy()
        return 2

    root = tk.Tk()
    root.title(f"ministar focus test - {description}")
    root.configure(bg="#101010")

    image_label = tk.Label(root, bg="#101010")
    image_label.pack(padx=8, pady=(8, 4))

    info = tk.Label(
        root, text="starting...", fg="#7fe07f", bg="#101010", font=("Consolas", 12), justify="left"
    )
    info.pack(pady=2)

    status = tk.Label(root, text="", fg="#e0c060", bg="#101010", font=("Consolas", 10))
    status.pack(pady=(0, 6))

    repeats = tk.IntVar(value=1)
    bar = tk.Frame(root, bg="#101010")
    bar.pack(pady=(0, 4))
    tk.Label(bar, text="pulses:", fg="#cccccc", bg="#101010").pack(side="left", padx=(0, 4))
    for v in (1, 3, 10, 30):
        tk.Radiobutton(
            bar,
            text=str(v),
            variable=repeats,
            value=v,
            fg="#cccccc",
            bg="#101010",
            selectcolor="#303030",
            activebackground="#101010",
        ).pack(side="left")

    buttons = tk.Frame(root, bg="#101010")
    buttons.pack(pady=(0, 8))
    actions = [
        ("Far 3\n<<<", False, 3),
        ("Far 2\n<<", False, 2),
        ("Far 1\n<", False, 1),
        ("Near 1\n>", True, 1),
        ("Near 2\n>>", True, 2),
        ("Near 3\n>>>", True, 3),
    ]

    def do(near: bool, tier: int) -> None:
        msg = controller.drive(near, tier, repeats.get())
        status.config(text=msg)

    for text, near, tier in actions:
        tk.Button(
            buttons,
            text=text,
            width=9,
            height=3,
            font=("Consolas", 11, "bold"),
            bg="#2a4a7a" if near else "#4a2a2a",
            fg="#ffffff",
            activebackground="#3a5a8a",
            command=lambda n=near, t=tier: do(n, t),
        ).pack(side="left", padx=3)

    tk.Label(
        root,
        text="keys:  , / .  = Near1 / Far1      </> = Near2 / Far2",
        fg="#808080",
        bg="#101010",
        font=("Consolas", 9),
    ).pack(pady=(0, 8))

    root.bind("<period>", lambda e: do(True, 1))
    root.bind("<comma>", lambda e: do(False, 1))
    root.bind("<Right>", lambda e: do(True, 2))
    root.bind("<Left>", lambda e: do(False, 2))
    root.bind("<Escape>", lambda e: root.destroy())

    _photo = {"img": None}

    def refresh() -> None:
        latest = controller.latest
        if latest is not None:
            img = Image.open(io.BytesIO(latest.jpeg))
            scale = min(900 / img.width, 620 / img.height, 1.0)
            if scale < 1.0:
                img = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
            _photo["img"] = ImageTk.PhotoImage(img)
            image_label.config(image=_photo["img"])

            hfd_text = (
                f"{latest.hfd:6.2f} px ({latest.stars} stars)" if latest.stars else "no stars"
            )

            change = ""
            if controller.drive_mark is not None:
                moved = float(np.abs(latest.thumb - controller.drive_mark).max())
                d_sharp = latest.tenengrad - (controller.drive_mark_sharp or 0.0)
                verdict = "CHANGED" if moved >= 2.0 else "no change"
                change = f"\nsince pulse {verdict:9s} pixels {moved:5.1f}  sharp {d_sharp:+8.2f}"

            backlog = controller.pending
            if backlog:
                change += f"\nmoving: {backlog} pulses queued"
            elif controller.dropped:
                change += f"\n{controller.dropped} pulses dropped (backlog full)"

            info.config(
                text=(
                    f"size      {latest.width}x{latest.height}\n"
                    f"HFD       {hfd_text}\n"
                    f"sharpness {latest.tenengrad:10.2f}   (Tenengrad; higher = sharper)"
                    f"{change}"
                )
            )
        if controller.error:
            status.config(text=controller.error)
        root.after(70, refresh)

    def on_close() -> None:
        controller.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.mainloop()
    controller.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Drive Canon EDSDK focus for bench testing")
    ap.add_argument("--selftest", action="store_true", help="headless validation")
    ap.add_argument("--diagnose", action="store_true", help="noise-aware drive test")
    ap.add_argument("--stress", action="store_true", help="fast-click overload test")
    args = ap.parse_args(argv)
    if args.stress:
        return stress()
    if args.diagnose:
        return diagnose()
    return selftest() if args.selftest else run_gui()


if __name__ == "__main__":
    sys.exit(main())
