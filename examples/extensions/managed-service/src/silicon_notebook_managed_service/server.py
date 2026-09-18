"""Foreground, loopback-only health service with cooperative SIGTERM shutdown.

The example performs no application work: a real plugin must only report ready
after its own initialization has completed. No request or exception text is logged.
"""

from __future__ import annotations

import argparse
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        healthy = self.path == "/health"
        payload = b'{"ready":true}\n' if healthy else b'{"error":"not_found"}\n'
        self.send_response(200 if healthy else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        """Do not emit raw paths or request data to stdout/stderr."""


class HealthServer(ThreadingHTTPServer):
    # An idle client must not prevent the foreground loop processing SIGTERM.
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        """Do not emit raw request exceptions; supervisor owns lifecycle events."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the example foreground companion service.")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    stopping = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    # handle_request returns on timeout so the main thread can close the server;
    # calling server.shutdown from a signal handler would deadlock it.
    with HealthServer(("127.0.0.1", args.port), HealthHandler) as server:
        server.timeout = 0.2
        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            while not stopping.is_set():
                server.handle_request()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
