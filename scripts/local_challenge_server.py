"""Small local platform fixture for exercising /monitor.

Run from the project directory:
    python scripts/local_challenge_server.py --port 8765

Endpoints:
    GET /api/challenges  -> aggregate + challenge list
    GET /api/challenges/<id> -> one challenge
    POST /api/challenges/<id>/submit -> marks the challenge solved
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

AUTH_TOKEN = "foxdog-local-token"

CHALLENGES = [
    {"id": "web-001", "title": "Local Login", "status": "open", "solved": False},
    {"id": "crypto-001", "title": "Local Caesar", "status": "solved", "solved": True},
    {"id": "pwn-001", "title": "Local Overflow", "status": "open", "solved": False},
    {"id": "misc-001", "title": "Local Archive", "status": "open", "solved": False},
    {"id": "rev-001", "title": "Local Bytecode", "status": "solved", "solved": True},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print(fmt % args)

    def _send(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if parse_qs(parsed.query).get("token", [""])[0] != AUTH_TOKEN:
            self._send({"error": "authentication required", "hint": "use ?token=foxdog-local-token"}, 401)
            return
        if path == "/api/challenges":
            solved = sum(1 for item in CHALLENGES if item["solved"])
            self._send({"total": len(CHALLENGES), "solved": solved, "challenges": CHALLENGES})
            return
        prefix = "/api/challenges/"
        if path.startswith(prefix):
            challenge_id = path[len(prefix):]
            for item in CHALLENGES:
                if item["id"] == challenge_id:
                    self._send(item)
                    return
        self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if parse_qs(parsed.query).get("token", [""])[0] != AUTH_TOKEN:
            self._send({"error": "authentication required"}, 401)
            return
        prefix = "/api/challenges/"
        if path.startswith(prefix) and path.endswith("/submit"):
            challenge_id = path[len(prefix):-len("/submit")].rstrip("/")
            for item in CHALLENGES:
                if item["id"] == challenge_id:
                    item["solved"] = True
                    item["status"] = "solved"
                    self._send({"ok": True, "challenge": item})
                    return
        self._send({"error": "not found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"local challenge platform: http://{args.host}:{args.port}/api/challenges")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
