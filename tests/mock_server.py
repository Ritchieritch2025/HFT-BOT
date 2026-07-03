#!/usr/bin/env python3
"""Keep-alive HTTP/1.1 mock of the Kalshi API for integration tests.

Usage: mock_server.py <port> <capture.jsonl>

Every request's auth headers are appended to the capture file so the harness
can verify the RSA-PSS signatures offline. GET /__stats reports how many TCP
connections and requests were served (to prove connection reuse).
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

stats_lock = threading.Lock()
stats = {"connections": 0, "requests": 0}
capture_lock = threading.Lock()
capture_path = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with stats_lock:
            stats["connections"] += 1

    def log_message(self, *args):
        pass

    def _respond(self, payload: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, method: str):
        with stats_lock:
            stats["requests"] += 1
        if self.path == "/__stats":
            with stats_lock:
                body = json.dumps(stats).encode()
            self._respond(body)
            return
        length = int(self.headers.get("Content-Length") or 0)
        req_body = self.rfile.read(length) if length else b""
        record = {
            "method": method,
            "path": self.path,
            "ts": self.headers.get("KALSHI-ACCESS-TIMESTAMP"),
            "sig": self.headers.get("KALSHI-ACCESS-SIGNATURE"),
            "key": self.headers.get("KALSHI-ACCESS-KEY"),
            "body_len": len(req_body),
            "body": req_body.decode("utf-8", errors="replace") if len(req_body) <= 2048 else None,
        }
        with capture_lock:
            with open(capture_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        self._respond(b'{"ok":true}')

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    global capture_path
    port = int(sys.argv[1])
    capture_path = sys.argv[2]
    open(capture_path, "w").close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock server on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
