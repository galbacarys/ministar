"""The plain-HTTP side of the console.

HTTPS is where the app lives: geolocation, Wake Lock and service workers all require
a secure context, and `ministar.local` is not one over plain HTTP. But three things
must stay on HTTP:

* the CA download, because it is what makes the HTTPS warning disappear;
* the captive-probe paths, because the phone's restricted portal sheet handles a
  certificate warning badly;
* the redirect for everything else, so a typed ``http://ministar.local`` lands in the
  right place.

This server has no application state and never touches the camera. It exists only
while the console runs with TLS. No HSTS: a self-signed warning must remain
bypassable.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

#: Paths phones probe to decide whether the network has internet. They get a plain
#: HTML page, not a redirect: the portal sheet cannot proceed through a TLS warning.
PROBE_PATHS = frozenset(
    {
        "/generate_204",
        "/gen_204",
        "/hotspot-detect.html",
        "/library/test/success.html",
        "/connecttest.txt",
        "/ncsi.txt",
        "/canonical.html",
        "/success.txt",
    }
)

#: The console URL shown on the landing page. The AP host is fixed, so this does not
#: depend on the Host header a probe used.
CONSOLE_URL = "https://ministar.local/"

_LANDING = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ministar</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; padding: 2rem 1.25rem; background: #000; color: #d33;
         font: 18px/1.5 system-ui, sans-serif; }}
  h1 {{ font-size: 1.6rem; letter-spacing: .08em; }}
  a {{ color: #ff6b6b; }}
  .box {{ max-width: 34rem; margin: 0 auto; }}
  .dim {{ color: #a33; font-size: .9rem; }}
  .button {{ display: inline-block; margin: 1.2rem 0; padding: .8rem 1.1rem;
             border: 1px solid #d33; border-radius: 6px; text-decoration: none; }}
</style>
</head>
<body><div class="box">
<h1>ministar</h1>
<p>The console is on HTTPS. Open it here:</p>
<a class="button" href="{CONSOLE_URL}">Open ministar</a>
<p>If your browser warns about the certificate, you can continue once, or
<a href="/ca.crt">install the ministar CA</a> and stop seeing the warning.</p>
<p class="dim">There is no internet on this network when ministar is in the field.
That is expected.</p>
</div></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "ministar-landing"
    ca_cert: Path | None = None

    def log_message(self, format: str, *args: object) -> None:
        # A captive probe can hammer this; do not fill the journal with it.
        return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in PROBE_PATHS:
            self._send(200, _LANDING.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path in {"/ca.crt", "/ministar-ca.crt", "/ca.pem"}:
            self._ca()
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._redirect(parsed.path, parsed.query)

    def do_HEAD(self) -> None:
        self.do_GET()

    def _ca(self) -> None:
        if self.ca_cert is None or not self.ca_cert.is_file():
            self._send(404, b"no CA certificate on this device\n", "text/plain")
            return
        body = self.ca_cert.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-x509-ca-cert")
        self.send_header("Content-Disposition", 'attachment; filename="ministar-ca.crt"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, query: str) -> None:
        host = self.headers.get("Host", "ministar.local").split(":", 1)[0] or "ministar.local"
        location = f"https://{host}{path}"
        if query:
            location += f"?{query}"
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class LandingServer:
    """A tiny HTTP server for the landing page, CA and captive probes."""

    def __init__(self, host: str, port: int, *, ca_cert: Path | None = None) -> None:
        handler = type("Handler", (_Handler,), {"ca_cert": ca_cert})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="ministar-landing", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread = None

    def __enter__(self) -> LandingServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["CONSOLE_URL", "PROBE_PATHS", "LandingServer"]
