"""Console output must be pure ASCII.

The Windows console runs on code page 437/850, so non-ASCII output mojibakes
(00 sec. 7). This was found the hard way: an em-dash in preflight output rendered
as a box character on the actual target.

Anything we print goes through here. Docstrings and comments are exempt -- they
are never written to a console.
"""

from __future__ import annotations

from pathlib import Path

from ministar import preflight
from ministar.cli import main
from ministar.config import Paths, Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(paths=Paths.under(tmp_path), astap=None, edsdk=None)


def test_preflight_render_is_ascii(tmp_path: Path) -> None:
    rendered = preflight.render(preflight.run(_settings(tmp_path)))
    rendered.encode("ascii")  # raises UnicodeEncodeError on failure


def test_preflight_check_details_are_ascii(tmp_path: Path) -> None:
    for check in preflight.run(_settings(tmp_path)):
        (check.name + check.detail).encode("ascii")


def test_camera_checklist_is_ascii() -> None:
    for item in preflight.CAMERA_CHECKS:
        item.encode("ascii")


def test_cli_preflight_output_is_ascii(tmp_path: Path, capsys) -> None:
    code = main(["--home", str(tmp_path), "preflight"])
    captured = capsys.readouterr()
    captured.out.encode("ascii")
    captured.err.encode("ascii")
    # No astap in this environment, so preflight must report a blocking failure.
    assert code == 1
