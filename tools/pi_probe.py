#!/usr/bin/env python3
"""M0 hardware probe for the ministar Linux port.

Run this on the Pi with the camera attached, before any of the port is written. It
answers the questions that cannot be answered by reading code:

* what the 60D actually exposes over PTP (focus drive, mirror lockup, LENR, ISO/Tv);
* capture and download time for one real CR2;
* live-view frame rate and JPEG size through python-gphoto2;
* whether `manualfocusdrive` moves the lens, and in which direction;
* how long an ASTAP solve takes on this board.

Nothing here is part of the shipping app. It is deliberately standalone: stdlib plus
`gphoto2`, with numpy/Pillow used only when present for the sharpness metric. Run it
with the system Python or the app's venv, either is fine.

    python3 tools/pi_probe.py --out probe-01
    python3 tools/pi_probe.py --list-only
    python3 tools/pi_probe.py --fov-deg 8.78

Writes `probe.json`, `report.md`, preview JPEGs and one CR2 under `--out`. It restores
the camera settings it changed (ISO, Tv, capture target, focus widget) unless
`--no-restore` is given. It does not change system settings.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

#: Where distro and ASTAP installs usually live. `PATH` is checked first.
ASTAP_CANDIDATES = (
    "/opt/astap/astap_cli",
    "/usr/bin/astap_cli",
    "/usr/local/bin/astap_cli",
)
DCRAW_CANDIDATES = ("/usr/bin/dcraw", "/usr/bin/dcraw_emu", "/usr/local/bin/dcraw")

#: Camera widgets worth extracting from the dump. The probe records all of them; these
#: are the ones the report calls out because the port depends on them.
KEY_WIDGETS = (
    "manualfocusdrive",
    "capturetarget",
    "mirrorlockup",
    "mirrordownstatus",
    "longexpnoisereduction",
    "shutterspeed",
    "aperture",
    "f-number",
    "autoexposuremode",
    "iso",
    "viewfinder",
)


class ProbeError(RuntimeError):
    pass


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run(argv: list[str], *, timeout: float = 60.0) -> dict[str, object]:
    """Run one command and keep the exit code, output and duration."""
    started = time.perf_counter()
    try:
        done = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "argv": [str(a) for a in argv],
            "returncode": done.returncode,
            "stdout": done.stdout,
            "stderr": done.stderr,
            "seconds": round(time.perf_counter() - started, 3),
        }
    except FileNotFoundError:
        return {"argv": [str(a) for a in argv], "error": "not found", "seconds": 0.0}
    except subprocess.TimeoutExpired:
        return {
            "argv": [str(a) for a in argv],
            "error": f"timed out after {timeout:g}s",
            "seconds": round(time.perf_counter() - started, 3),
        }


def which(name: str, candidates: tuple[str, ...] = ()) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def system_report(out: Path) -> dict[str, object]:
    """Facts about the board, the OS and the current USB/permission setup."""
    os_release = read_text("/etc/os-release")
    cpuinfo = read_text("/proc/cpuinfo")
    model = ""
    for line in cpuinfo.splitlines():
        if line.startswith(("Model", "model name", "Hardware")):
            model = line.split(":", 1)[-1].strip()
            break

    temperature = ""
    throttled = ""
    if which("vcgencmd"):
        temperature = str(run(["vcgencmd", "measure_temp"], timeout=10).get("stdout", "")).strip()
        throttled = str(run(["vcgencmd", "get_throttled"], timeout=10).get("stdout", "")).strip()

    usbfs = read_text("/sys/module/usbcore/parameters/usbfs_memory_mb")
    groups = str(run(["id"], timeout=10).get("stdout", "")).strip()
    udev = sorted(str(p) for p in Path("/usr/lib/udev/rules.d").glob("*gphoto*"))
    udev += sorted(str(p) for p in Path("/etc/udev/rules.d").glob("*gphoto*"))

    gphoto_version = run(["gphoto2", "--version"], timeout=20) if which("gphoto2") else {}
    astap = which("astap_cli", ASTAP_CANDIDATES)
    db_dir = Path(astap).parent if astap else None
    db_tiles = sorted(p.name for p in db_dir.glob("*.1476")) if db_dir else []
    db_tiles += sorted(p.name for p in db_dir.glob("*.290")) if db_dir else []

    return {
        "hostname": platform.node(),
        "os_release": os_release,
        "kernel": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "cpu": model,
        "memory": str(run(["free", "-m"], timeout=10).get("stdout", "")).strip(),
        "disk": str(run(["df", "-h", str(out)], timeout=10).get("stdout", "")).strip(),
        "usbfs_memory_mb": usbfs,
        "vcgencmd": {"temp": temperature, "throttled": throttled},
        "groups": groups,
        "udev_rules": udev,
        "gphoto2_cli": gphoto_version,
        "astap": astap,
        "astap_database_dir": str(db_dir) if db_dir else None,
        "astap_tiles": {"count": len(db_tiles), "sample": db_tiles[:8]},
        "lsusb": str(run(["lsusb"], timeout=15).get("stdout", "")).strip(),
    }


# ---------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------


def import_gphoto2():
    try:
        import gphoto2
    except ImportError as exc:  # pragma: no cover - depends on the Pi
        raise ProbeError(
            "python-gphoto2 is not installed. On the Pi: `sudo apt install python3-gphoto2` "
            "or `python3 -m pip install gphoto2` in the venv. "
            f"({exc})"
        ) from exc
    return gphoto2


class Camera:
    """Thin wrapper that keeps libgphoto2 calls on one thread and restores settings."""

    def __init__(self) -> None:
        self.gp = import_gphoto2()
        self.camera = self.gp.Camera()
        self.camera.init()
        self.saved: dict[str, str] = {}
        try:
            self.config = self.camera.get_config()
        except Exception:
            self.camera.exit()
            raise

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.camera.exit()

    def __enter__(self) -> Camera:
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.restore()
        finally:
            self.close()

    def widgets(self) -> list[dict[str, object]]:
        """Every leaf widget with its path, current value and radio/menu choices."""
        found: list[dict[str, object]] = []

        def walk(widget, prefix: str) -> None:
            name = widget.get_name()
            path = f"{prefix}/{name}" if prefix else f"/{name}"
            children = widget.count_children()
            if children:
                for child in widget.get_children():
                    walk(child, path)
                return
            choices = []
            if widget.get_type() in (self.gp.GP_WIDGET_RADIO, self.gp.GP_WIDGET_MENU):
                try:
                    choices = [widget.get_choice(i) for i in range(widget.count_choices())]
                except Exception:
                    choices = []
            try:
                value = widget.get_value()
            except Exception:
                value = ""
            found.append(
                {
                    "path": path,
                    "label": widget.get_label(),
                    "type": int(widget.get_type()),
                    "readonly": bool(widget.get_readonly()),
                    "value": value,
                    "choices": choices,
                }
            )

        walk(self.config, "")
        return found

    def find(self, needle: str):
        """The first leaf widget whose path contains ``needle``."""
        found: list[tuple[str, object]] = []

        def walk(widget, prefix: str) -> None:
            name = widget.get_name()
            path = f"{prefix}/{name}" if prefix else f"/{name}"
            if widget.count_children():
                for child in widget.get_children():
                    walk(child, path)
            elif needle in path:
                found.append((path, widget))

        walk(self.config, "")
        return found[0] if found else (None, None)

    def remember(self, path: str, widget) -> None:
        if path and path not in self.saved:
            with contextlib.suppress(Exception):
                self.saved[path] = str(widget.get_value())

    def set_choice(self, needle: str, value: str | None) -> tuple[str | None, str]:
        """Set a radio/menu widget by label, case-insensitively.

        Returns ``(path, applied_value)``. A missing widget is a result, not an error:
        the point of the probe is to learn what the body exposes.
        """
        path, widget = self.find(needle)
        if widget is None:
            return None, ""
        self.remember(path, widget)
        choices = []
        with contextlib.suppress(Exception):
            choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        if value is None:
            return path, ""
        match = next((c for c in choices if c.lower() == value.lower()), None)
        if match is None:
            match = next((c for c in choices if value.lower() in c.lower()), None)
        if match is None:
            return path, f"value {value!r} not in {choices!r}"
        try:
            widget.set_value(match)
            self.camera.set_config(self.config)
        except Exception as exc:  # surfaced in the report, not fatal
            return path, f"{type(exc).__name__}: {exc}"
        return path, match

    def restore(self) -> None:
        changed = False
        for path, value in self.saved.items():
            try:
                path_parts = [p for p in path.split("/") if p]
                widget = self.config
                for part in path_parts:
                    widget = widget.get_child_by_name(part)
                widget.set_value(value)
                changed = True
            except Exception:
                continue
        if changed:
            with contextlib.suppress(Exception):
                self.camera.set_config(self.config)

    def preview(self) -> bytes:
        camera_file = self.camera.capture_preview()
        data = camera_file.get_data_and_size()
        return bytes(memoryview(data))

    def capture(self, target: Path) -> dict[str, object]:
        t0 = time.perf_counter()
        camera_file = self.camera.capture(self.gp.GP_CAPTURE_IMAGE)
        t1 = time.perf_counter()
        file = self.camera.file_get(
            camera_file.folder, camera_file.name, self.gp.GP_FILE_TYPE_NORMAL
        )
        t2 = time.perf_counter()
        file.save(str(target))
        t3 = time.perf_counter()
        return {
            "name": camera_file.name,
            "folder": camera_file.folder,
            "target": str(target),
            "bytes": target.stat().st_size if target.is_file() else 0,
            "capture_s": round(t1 - t0, 3),
            "download_s": round(t2 - t1, 3),
            "save_s": round(t3 - t2, 3),
            "total_s": round(t3 - t0, 3),
        }


# ---------------------------------------------------------------------------
# image metric
# ---------------------------------------------------------------------------


def sharpness(jpeg: bytes) -> dict[str, object]:
    """A focus metric that works on a camera preview JPEG.

    Tenengrad on the green/luma plane is enough to tell "the lens moved" and which
    direction improved. The app's HFD metric is better on stars, but it needs the app's
    package and a dark frame; this has to run on a bare Pi in daylight too.
    """
    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy not installed"}

    gray = None
    try:
        import cv2

        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is not None:
            gray = image.astype(np.float32)
    except ImportError:
        pass

    if gray is None:
        try:
            from io import BytesIO

            from PIL import Image

            gray = np.asarray(Image.open(BytesIO(jpeg)).convert("L"), dtype=np.float32)
        except ImportError:
            return {"error": "neither cv2 nor Pillow is installed"}

    if gray is None or gray.size == 0:
        return {"error": "could not decode JPEG"}
    gy, gx = np.gradient(gray)
    return {
        "tenengrad": float((gx * gx + gy * gy).mean()),
        "mean": float(gray.mean()),
        "std": float(gray.std()),
        "width": int(gray.shape[1]),
        "height": int(gray.shape[0]),
    }


# ---------------------------------------------------------------------------
# probe steps
# ---------------------------------------------------------------------------


def dump_config(cam: Camera, out: Path) -> dict[str, object]:
    widgets = cam.widgets()
    (out / "config.json").write_text(json.dumps(widgets, indent=2) + "\n", encoding="utf-8")
    lines = []
    for item in widgets:
        choices = item["choices"]
        suffix = f"  choices={choices}" if choices else ""
        lines.append(f"{item['path']}  =  {item['value']!r}{suffix}")
    (out / "config.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    key = {}
    for needle in KEY_WIDGETS:
        matches = [w for w in widgets if needle in str(w["path"])]
        key[needle] = matches
    summary = (
        str(run(["gphoto2", "--summary"], timeout=60).get("stdout", "")) if which("gphoto2") else ""
    )
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    return {"widget_count": len(widgets), "key": key}


def focus_sweep(cam: Camera, out: Path, steps: int) -> dict[str, object]:
    """Pulse Near 3 and Far 3, alternating with None, and measure sharpness.

    libgphoto2 #767: repeating the same choice does nothing. Every pulse is therefore
    followed by ``None`` before the next one. If this works, the driver can rely on the
    same alternation.
    """
    path, widget = cam.find("manualfocusdrive")
    if widget is None:
        return {"available": False, "reason": "no manualfocusdrive widget"}

    choices = []
    with contextlib.suppress(Exception):
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
    result: dict[str, object] = {"available": True, "path": path, "choices": choices}

    # A preview first: on EOS bodies focus drive only acts while live view is running.
    baseline_jpeg = cam.preview()
    (out / "focus_baseline.jpg").write_bytes(baseline_jpeg)
    baseline = sharpness(baseline_jpeg)
    result["baseline"] = baseline

    for direction in ("Near 3", "Far 3"):
        if direction not in choices:
            result[direction] = {"error": f"choice absent; got {choices}"}
            continue
        samples = []
        try:
            for index in range(steps):
                path, applied = cam.set_choice("manualfocusdrive", direction)
                if not applied:
                    result[direction] = {"error": f"pulse failed: {path}"}
                    break
                cam.set_choice("manualfocusdrive", "None")  # required, see #767
                jpeg = cam.preview()
                score = sharpness(jpeg)
                samples.append(score.get("tenengrad"))
                if index in (0, steps - 1):
                    name = direction.replace(" ", "_")
                    (out / f"focus_{name}_{index:02d}.jpg").write_bytes(jpeg)
                time.sleep(0.15)
        except Exception as exc:  # a failed pulse is the result
            result[direction] = {"error": f"{type(exc).__name__}: {exc}", "samples": samples}
            continue
        values = [v for v in samples if isinstance(v, (int, float))]
        result[direction] = {
            "samples": samples,
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "delta": (values[-1] - values[0]) if len(values) >= 2 else None,
        }

    # Best effort: walk back the same number of coarse steps. The lens is not where it
    # started, but it is not at an end stop either.
    cam.set_choice("manualfocusdrive", "Near 3")
    cam.set_choice("manualfocusdrive", "None")
    cam.set_choice("manualfocusdrive", "Far 3")
    cam.set_choice("manualfocusdrive", "None")
    cam.restore()
    return result


def live_view_bench(cam: Camera, out: Path, seconds: float) -> dict[str, object]:
    started = time.perf_counter()
    frames = 0
    sizes = []
    times = []
    saved = 0
    error = None
    while time.perf_counter() - started < seconds:
        t0 = time.perf_counter()
        try:
            jpeg = cam.preview()
        except Exception as exc:  # reported, then stop the loop
            error = f"{type(exc).__name__}: {exc}"
            break
        times.append(time.perf_counter() - t0)
        sizes.append(len(jpeg))
        frames += 1
        if saved < 5:
            (out / f"preview_{saved:02d}.jpg").write_bytes(jpeg)
            saved += 1
    elapsed = time.perf_counter() - started
    return {
        "frames": frames,
        "seconds": round(elapsed, 3),
        "fps": round(frames / elapsed, 2) if elapsed > 0 else None,
        "jpeg_bytes": {
            "min": min(sizes) if sizes else None,
            "max": max(sizes) if sizes else None,
            "mean": round(sum(sizes) / len(sizes), 1) if sizes else None,
        },
        "latency_s": {
            "min": round(min(times), 3) if times else None,
            "max": round(max(times), 3) if times else None,
            "mean": round(sum(times) / len(times), 3) if times else None,
        },
        "error": error,
    }


def prep_capture(cam: Camera) -> dict[str, object]:
    """Short exposure and a fixed ISO, so the timing is download-bound."""
    applied = {}
    for label, needle, value in (
        ("capturetarget", "settings/capturetarget", "Internal RAM"),
        ("shutterspeed", "capturesettings/shutterspeed", "1/60"),
        ("iso", "imgsettings/iso", "800"),
    ):
        path, result = cam.set_choice(needle, value)
        applied[label] = {"path": path, "applied": result}
    return applied


def capture_bench(cam: Camera, out: Path, count: int) -> dict[str, object]:
    # One frame is enough to convert and solve; the rest are timing samples.
    frames: list[dict[str, object]] = []
    errors: list[str] = []
    for index in range(count):
        target = out / f"capture_{index:02d}.cr2"
        try:
            frames.append(cam.capture(target))
        except Exception as exc:  # a failed capture is a result
            errors.append(f"frame {index}: {type(exc).__name__}: {exc}")
            continue
        with (out / "capture_log.txt").open("a", encoding="utf-8") as log:
            log.write(json.dumps(frames[-1]) + "\n")
    totals = [f["total_s"] for f in frames]
    return {
        "frames": frames,
        "mean_total_s": round(sum(totals) / len(totals), 3) if totals else None,
        "mean_download_s": round(sum(f["download_s"] for f in frames) / len(frames), 3)
        if frames
        else None,
        "mean_bytes": round(sum(f["bytes"] for f in frames) / len(frames)) if frames else None,
        "errors": errors,
    }


def convert_to_fits(raw: Path, out: Path) -> dict[str, object]:
    """The same conversion the app uses: undebayered TIFF, then ASTAP reads it."""
    astap_input = raw
    attempts = []

    # Prefer the app's converter when the package is importable, because that is the
    # path the port will actually use.
    probe = run(
        [sys.executable, "-m", "ministar", "to-fits", str(raw), "--out", str(out / "frame.fit")],
        timeout=180,
    )
    attempts.append(probe)
    if probe.get("returncode") == 0 and (out / "frame.fit").is_file():
        return {"ok": True, "kind": "fits", "path": str(out / "frame.fit"), "attempts": attempts}

    dcraw = which("dcraw", DCRAW_CANDIDATES) or which("dcraw_emu", DCRAW_CANDIDATES)
    if not dcraw:
        return {"ok": False, "error": "no dcraw / dcraw_emu", "attempts": attempts}
    converted = run(
        [dcraw, "-4", "-D", "-T", "-o", "0", "-r", "1", "1", "1", "1", str(raw)],
        timeout=180,
    )
    attempts.append(converted)
    for candidate in (raw.with_suffix(raw.suffix + ".tiff"), raw.with_suffix(".tiff")):
        if candidate.is_file():
            astap_input = candidate
            break
    return {
        "ok": astap_input != raw,
        "kind": "tiff" if astap_input != raw else "raw",
        "path": str(astap_input),
        "attempts": attempts,
    }


def astap_bench(image: Path, out: Path, fov_deg: float | None) -> dict[str, object]:
    astap = which("astap_cli", ASTAP_CANDIDATES)
    if not astap:
        return {"available": False, "reason": "astap_cli not found"}
    solved_base = out / "astap"
    args = [astap, "-f", str(image), "-o", str(solved_base), "-wcs"]
    if fov_deg:
        # A blind solve starts at RA 0 / Dec 0. The field target can be 60+ degrees
        # away, so 30 is not a search radius; 60 is what the app uses with no hint.
        args += ["-fov", f"{fov_deg:g}", "-r", "60", "-s", "100", "-z", "2"]
    else:
        args += ["-r", "60"]
    result = run(args, timeout=180)
    ini = solved_base.with_suffix(".ini")
    text = read_text(ini)
    return {
        "available": True,
        "astap": astap,
        "args": result.get("argv"),
        "seconds": result.get("seconds"),
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "ini": text,
        "solved": "PLTSOLVD=T" in text,
    }


def report_markdown(data: dict[str, object], out: Path) -> str:
    system = data.get("system", {})
    config = data.get("config", {})
    live = data.get("live_view", {})
    capture = data.get("capture", {})
    focus = data.get("focus", {})
    solve = data.get("astap", {})

    os_first = str(system.get("os_release") or "?").splitlines()[0]
    lines = [
        "# ministar Pi probe",
        "",
        f"Generated {datetime.now().isoformat(timespec='seconds')} on {system.get('hostname')}.",
        "",
        "## System",
        "",
        f"- OS: {os_first}",
        f"- Kernel: {system.get('kernel')}",
        f"- CPU: {system.get('cpu')}",
        f"- Python: {system.get('python', '').splitlines()[0] if system.get('python') else '?'}",
        f"- usbfs_memory_mb: **{system.get('usbfs_memory_mb') or '?'}**",
        f"- vcgencmd: {system.get('vcgencmd')}",
        f"- ASTAP: `{system.get('astap')}` ({system.get('astap_tiles')})",
        "",
        "## Camera",
        "",
        f"- Config widgets: {config.get('widget_count')}",
    ]
    key = config.get("key", {})
    for name in KEY_WIDGETS:
        matches = key.get(name, []) if isinstance(key, dict) else []
        if matches:
            first = matches[0]
            choices = first.get("choices") or []
            lines.append(
                f"- `{first.get('path')}` = {first.get('value')!r}"
                + (f" (choices: {', '.join(map(str, choices))})" if choices else "")
            )
        else:
            lines.append(f"- **missing:** `{name}`")
    lines += [
        "",
        "## Timings",
        "",
        f"- Live view: **{live.get('fps')} fps**, JPEG {live.get('jpeg_bytes')}, "
        f"latency {live.get('latency_s')}",
        f"- Capture: mean **{capture.get('mean_total_s')} s** total, "
        f"{capture.get('mean_download_s')} s download, {capture.get('mean_bytes')} bytes",
        f"- ASTAP: **{solve.get('seconds')} s**, solved={solve.get('solved')}",
        f"- Focus drive: {json.dumps(focus, indent=2)[:1200]}",
        "",
        "## Verdict",
        "",
    ]
    fps = live.get("fps")
    if isinstance(fps, (int, float)):
        lines.append(f"- live view {'OK' if fps >= 5 else 'SLOW'} ({fps} fps; want >= 5)")
    total = capture.get("mean_total_s")
    if isinstance(total, (int, float)):
        lines.append(f"- capture {'OK' if total <= 3 else 'SLOW'} ({total} s; want <= 3)")
    if focus.get("available"):
        near = focus.get("Near 3") or {}
        far = focus.get("Far 3") or {}
        moved = any(
            isinstance(v.get("delta"), (int, float)) and abs(v["delta"]) > 0
            for v in (near, far)
            if isinstance(v, dict)
        )
        lines.append(f"- focus drive {'MOVED' if moved else 'NO MEASURABLE CHANGE'}")
    if solve.get("available"):
        lines.append(f"- ASTAP {'OK' if solve.get('solved') else 'FAILED'}")
    lines += [
        "",
        "Raw data is in `probe.json`; the full widget dump is `config.txt`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="output directory (default: probe-<timestamp>)")
    parser.add_argument("--preview-seconds", type=float, default=8.0)
    parser.add_argument("--captures", type=int, default=5)
    parser.add_argument("--focus-steps", type=int, default=8)
    parser.add_argument(
        "--fov-deg",
        type=float,
        default=None,
        help="image height in degrees, for a hinted ASTAP solve",
    )
    parser.add_argument("--list-only", action="store_true", help="dump config, no captures")
    parser.add_argument("--no-focus", action="store_true")
    parser.add_argument("--no-capture", action="store_true")
    parser.add_argument("--no-astap", action="store_true")
    parser.add_argument("--no-restore", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.out or f"probe-{now_stamp()}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"started": datetime.now().isoformat(timespec="seconds")}

    print(f"probe output: {out}")
    data["system"] = system_report(out)
    print(f"system: {data['system'].get('cpu')} / usbfs={data['system'].get('usbfs_memory_mb')}")

    try:
        with Camera() as cam:
            print("camera open; dumping config")
            data["config"] = dump_config(cam, out)

            if not args.list_only and not args.no_focus:
                print(f"focus sweep ({args.focus_steps} steps per direction)")
                data["focus"] = focus_sweep(cam, out, args.focus_steps)

            if not args.list_only:
                print(f"live view for {args.preview_seconds:g}s")
                data["live_view"] = live_view_bench(cam, out, args.preview_seconds)

            if not args.list_only and not args.no_capture:
                print(f"capturing {args.captures} frames")
                data["capture_prep"] = prep_capture(cam)
                data["capture"] = capture_bench(cam, out, args.captures)

            if not args.list_only and not args.no_astap:
                frames = sorted(out.glob("capture_*.cr2"))
                if frames:
                    print(f"converting {frames[0].name}")
                    converted = convert_to_fits(frames[0], out)
                    data["convert"] = converted
                    if converted.get("ok"):
                        print("solving with ASTAP")
                        data["astap"] = astap_bench(Path(str(converted["path"])), out, args.fov_deg)
                else:
                    data["astap"] = {"available": False, "reason": "no captured CR2"}

            if args.no_restore:
                cam.saved.clear()
    except ProbeError as exc:
        data["error"] = str(exc)
        print(f"error: {exc}", file=sys.stderr)
    except Exception as exc:
        data["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {data['error']}", file=sys.stderr)

    data["finished"] = datetime.now().isoformat(timespec="seconds")
    (out / "probe.json").write_text(
        json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8"
    )
    report = report_markdown(data, out)
    (out / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 0 if "error" not in data else 1


if __name__ == "__main__":
    raise SystemExit(main())
