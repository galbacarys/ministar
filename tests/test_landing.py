"""The HTTP landing/captive server, without a browser.

The important behaviours are small but easy to get wrong: the CA must be downloadable
over plain HTTP, the captive-probe paths must not redirect into a certificate warning,
and ordinary paths must land on HTTPS with the same host and query.
"""

from __future__ import annotations

import http.client
from pathlib import Path

import pytest

from ministar.ui.landing import LandingServer


@pytest.fixture
def landing(tmp_path: Path):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    server = LandingServer("127.0.0.1", 0, ca_cert=ca)
    server.start()
    try:
        yield server, ca
    finally:
        server.stop()


def _get(port: int, path: str, host: str = "ministar.local") -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_the_ca_is_served_over_plain_http(landing) -> None:
    server, ca = landing
    status, headers, body = _get(server.port, "/ca.crt")
    assert status == 200
    assert headers["Content-Type"] == "application/x-x509-ca-cert"
    assert body == ca.read_bytes()
    assert "attachment" in headers["Content-Disposition"]


def test_a_normal_path_redirects_to_https_and_keeps_host_and_query(landing) -> None:
    server, _ca = landing
    status, headers, _body = _get(server.port, "/foo?x=1", host="ministar.local:80")
    assert status == 302
    assert headers["Location"] == "https://ministar.local/foo?x=1"


def test_captive_probes_get_a_page_not_a_redirect(landing) -> None:
    server, _ca = landing
    for path in ("/generate_204", "/hotspot-detect.html", "/connecttest.txt"):
        status, headers, body = _get(server.port, path, host="connectivitycheck.gstatic.com")
        assert status == 200, path
        assert "Location" not in headers, path
        assert b"Open ministar" in body, path


def test_health_is_plain_text(landing) -> None:
    server, _ca = landing
    status, headers, body = _get(server.port, "/health")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert body == b"ok\n"


def test_a_missing_ca_is_a_404(tmp_path: Path) -> None:
    server = LandingServer("127.0.0.1", 0, ca_cert=tmp_path / "absent.crt")
    server.start()
    try:
        status, _headers, body = _get(server.port, "/ca.crt")
        assert status == 404
        assert b"no CA" in body
    finally:
        server.stop()
