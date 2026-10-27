"""Command line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from ministar import __version__, preflight
from ministar.config import Settings
from ministar.solver import SolveHints, solve


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"ministar {__version__}")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    settings = Settings.load(Path(args.home) if args.home else None)
    checks = preflight.run(settings)
    print(preflight.render(checks))

    blocked = preflight.blocking(checks)
    if blocked:
        print(f"\n{len(blocked)} blocking problem(s); fix these before a session")
        return 1
    print("\ncamera checks required at session start:")
    for item in preflight.CAMERA_CHECKS:
        print(f"  [ ] {item}")
    return 0


def _cmd_hfd(args: argparse.Namespace) -> int:
    from ministar.focus.metrics import measure_hfd
    from ministar.io.fits import read_fits

    image, header = read_fits(args.image)
    hfd, stars = measure_hfd(image, max_stars=args.max_stars)
    shape = "x".join(str(n) for n in image.shape)
    print(args.image)
    print(f"  {shape} {image.dtype}  FL={header.get('FOCALLEN')}  EXPTIME={header.get('EXPTIME')}")
    print(f"  stars {stars}   median HFD {hfd:.2f} px")
    return 0 if stars > 0 else 1


def _cmd_solve(args: argparse.Namespace) -> int:
    settings = Settings.load(Path(args.home) if args.home else None)
    if settings.astap is None:
        print("error: astap_cli.exe not found; run `ministar preflight`")
        return 2

    image = Path(args.image)
    out_base = settings.paths.scratch / image.stem
    hints = None
    if args.fov:
        hints = SolveHints(
            fov_height_deg=args.fov,
            ra_hours=args.ra,
            south_pole_distance_deg=args.spd,
            radius_deg=args.radius,
        )

    result = solve(image, settings.astap, out_base=out_base, hints=hints)

    if result.warnings:
        for warning in result.warnings:
            print(f"warning: {warning}")

    if not result.solved or result.solution is None:
        print(f"not solved: {result.error or result.error_meaning or 'unknown'}")
        if result.starved:
            print("  (no stars detected -- check focus, dew, cloud, exposure length)")
        return 1

    sol = result.solution
    print(f"solved   RA {sol.crval[0]:.5f}  Dec {sol.crval[1]:+.5f}")
    print(f"         scale {sol.pixel_scale_arcsec:.3f} arcsec/px")
    print(f"         PA    {sol.position_angle_deg:.2f} deg")
    print(f"         parity {'mirrored' if sol.is_mirrored else 'normal'}")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """Capture one frame and report the rig facts carried in its EXIF.

    Across the WSL/Windows boundary the machine-readable channel is a file, not
    stdout (00 §7), so everything lands in ``rig-probe.json`` beside the frame and
    only a summary is printed. ``--symbols`` answers part of it without a camera,
    which separates "our bindings are wrong" from "the body did not behave".
    """
    import json

    from ministar.camera import edsdk_ffi

    settings = Settings.load(Path(args.home) if args.home else None)
    out = Path(args.out) if args.out else settings.paths.data / "probe"
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "ok": False,
        # We do not set Tv/ISO yet, so the frame records whatever the dial says.
        "note": "exposure and ISO are whatever the body is set to; setting them is "
        "not implemented (brief 16 §11)",
    }
    code = 1

    try:
        path, origin = edsdk_ffi.resolve_dll(None)
        report["dll"] = {"path": str(path), "origin": origin}

        bindings = edsdk_ffi.Edsdk(path)
        report["symbols"] = bindings.symbol_report()
        bindings.close()

        required = (
            "EdsSetObjectEventHandler",
            "EdsGetEvent",
            "EdsGetDirectoryItemInfo",
            "EdsCreateFileStream",
            "EdsDownload",
            "EdsDownloadComplete",
        )
        missing = [name for name in required if not report["symbols"].get(name)]
        report["missing_symbols"] = missing

        if args.symbols:
            report["ok"] = not missing
            code = 0 if not missing else 3
        elif missing:
            report["error"] = f"this EDSDK.dll lacks {', '.join(missing)}"
            code = 3
        else:
            code = _probe_capture(report=report, args=args, out=out, dll=path)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    finally:
        report_path = out / "rig-probe.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(report_path)
        if report.get("ok"):
            print("ok")
        else:
            print(f"failed: {report.get('error') or report.get('missing_symbols')}")
    return code


def _probe_capture(
    *, report: dict[str, object], args: argparse.Namespace, out: Path, dll: Path
) -> int:
    """The half that needs a camera: shoot, download, read the EXIF back."""
    from datetime import datetime

    from ministar.camera.edsdk import EdsdkCamera
    from ministar.io.tiff import TiffError, read_frame_exif

    camera = EdsdkCamera(dll_path=dll)
    try:
        info = camera.connect()
        report["camera"] = {"name": info.name, "port": info.port}

        target = out / f"testframe_{datetime.now():%Y%m%dT%H%M%S}.cr2"
        report["frame"] = str(target)
        camera.capture_to(target, timeout_s=args.timeout)
        report["captured_bytes"] = target.stat().st_size

        try:
            exif = read_frame_exif(target)
            report["exif"] = exif.as_dict()
            if problem := exif.geometry_problem():
                report["exif_geometry_problem"] = problem
        except TiffError as exc:
            report["exif_error"] = str(exc)

        # Brief 16 §11 asks whether a still download kills live view on this body.
        # Answering it costs one line here and a whole hardware session later.
        try:
            report["live_view_after_capture_bytes"] = len(camera.grab_live_jpeg())
        except Exception as exc:
            report["live_view_after_capture_error"] = f"{type(exc).__name__}: {exc}"

        report["ok"] = True
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        camera.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ministar", description=__doc__)
    parser.add_argument("--home", help="override MINISTAR_HOME")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print version").set_defaults(func=_cmd_version)
    sub.add_parser("preflight", help="check the environment").set_defaults(func=_cmd_preflight)

    p_hfd = sub.add_parser("hfd", help="measure star sharpness (HFD) in an image")
    p_hfd.add_argument("image")
    p_hfd.add_argument("--max-stars", type=int, default=40)
    p_hfd.set_defaults(func=_cmd_hfd)

    p_solve = sub.add_parser("solve", help="plate solve one image")
    p_solve.add_argument("image")
    p_solve.add_argument("--fov", type=float, help="image HEIGHT in degrees")
    p_solve.add_argument("--ra", type=float, help="hint RA in hours")
    p_solve.add_argument("--spd", type=float, help="hint south-pole distance (dec + 90)")
    p_solve.add_argument("--radius", type=float, default=3.0, help="search radius in degrees")
    p_solve.set_defaults(func=_cmd_solve)

    p_probe = sub.add_parser(
        "probe", help="capture a test frame and report the rig facts in its EXIF"
    )
    p_probe.add_argument("--out", help="directory for the frame and rig-probe.json")
    p_probe.add_argument("--timeout", type=float, default=120.0, help="download timeout, s")
    p_probe.add_argument(
        "--symbols",
        action="store_true",
        help="only check which EDSDK entry points exist (no camera needed)",
    )
    p_probe.set_defaults(func=_cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
