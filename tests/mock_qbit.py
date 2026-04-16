"""Minimal mock qBittorrent for browser_e2e isolation.

Implements only what `_test_client` and the test harness exercise:
  - POST /api/v2/auth/login   → 200, body "Ok."
  - GET  /api/v2/app/version  → 200, body "4.6-mock"
  - GET  /                     → 200 (healthcheck)
Anything else returns 404 to make missing assertions visible.

Runs as a sidecar in docker-compose.test.yml. No deps beyond stdlib so the
sidecar can use a plain python:3.12-slim image without a separate Dockerfile.
"""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def _ok(self, body: bytes, ctype: str = "text/plain"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        msg = b"mock-qbit: path not implemented\n"
        self.send_response(404)
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def do_POST(self):
        # Drain the body — qBit clients send form data.
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/api/v2/auth/login"):
            return self._ok(b"Ok.")
        if self.path.startswith("/api/v2/torrents/add"):
            return self._ok(b"")
        return self._not_found()

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/healthz"):
            return self._ok(b"ok\n")
        if self.path.startswith("/api/v2/app/version"):
            return self._ok(b"4.6-mock")
        if self.path.startswith("/api/v2/torrents/info"):
            return self._ok(b"[]", ctype="application/json")
        return self._not_found()

    # Keep the noise out of the test-run log.
    def log_message(self, fmt, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
