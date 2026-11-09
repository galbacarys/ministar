"""The per-device certificate pair.

This is a shell/openssl boundary, so the test checks the properties the app depends on:
a CA that verifies the leaf, the hostname and AP IP in the SAN list, and private keys
that are not world-readable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "ministar-tls.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("openssl") is None,
    reason="needs a POSIX shell and openssl",
)


def test_the_pair_verifies_and_has_the_names_the_app_uses(tmp_path: Path) -> None:
    done = subprocess.run(
        ["sh", str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr

    ca = tmp_path / "ca.crt"
    leaf = tmp_path / "leaf.crt"
    assert ca.is_file() and leaf.is_file()
    assert (tmp_path / "leaf.key").is_file()

    verify = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ca), str(leaf)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr

    text = subprocess.run(
        ["openssl", "x509", "-in", str(leaf), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "DNS:ministar.local" in text
    assert "DNS:ministar" in text
    assert "IP Address:10.42.0.1" in text
    assert "IP Address:127.0.0.1" in text

    mode = (tmp_path / "leaf.key").stat().st_mode & 0o777
    assert mode == 0o600


def test_a_second_run_keeps_the_existing_pair(tmp_path: Path) -> None:
    subprocess.run(["sh", str(SCRIPT), str(tmp_path)], check=True)
    before = (tmp_path / "leaf.crt").read_bytes()
    subprocess.run(["sh", str(SCRIPT), str(tmp_path)], check=True)
    assert (tmp_path / "leaf.crt").read_bytes() == before
