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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
