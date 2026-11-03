"""Ops HTTP API on localhost (stdlib, like camview_token_server.py): /health, /status, /pause, /resume."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger("uptime.api")


def start_api(service, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path == "/health":
                self._json(200, service.health())
            elif path == "/status":
                self._json(200, service.status())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except ValueError:
                body = {}
            if path == "/pause":
                service.sched.pause("api", str(body.get("reason") or "paused via API"))
                self._json(200, {"ok": True, "paused": True})
            elif path == "/resume":
                service.sched.resume("api")
                self._json(200, {"ok": True, "paused": False})
            else:
                self._json(404, {"error": "not found"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="uptime-api", daemon=True).start()
    log.info("Ops API on http://%s:%d (/health /status, POST /pause /resume)", host, port)
    return server
