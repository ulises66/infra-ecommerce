import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


def build_response():
    return {
        "message": "Ecommerce backend placeholder",
        "database": {
            "host": os.environ.get("DB_HOST", "unset"),
            "port": os.environ.get("DB_PORT", "unset"),
            "name": os.environ.get("DB_NAME", "unset"),
            "user": os.environ.get("DB_USERNAME", "unset"),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in {"/", "/api", "/api/health", "/health"}:
            self._send_json({"status": "ok", **build_response()})
        else:
            self._send_json({"status": "not_found", "path": self.path}, status=404)

    def log_message(self, format, *args):  # noqa: A003  # pragma: no cover
        return


def main():
    port = int(os.environ.get("PORT", "4000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
