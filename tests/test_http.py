"""Session HTTP partagée : retries sur les erreurs transitoires d'athle.fr."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from core.http import make_session


def test_session_retries_transient_503() -> None:
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            calls.append(self.path)
            self.send_response(503 if len(calls) < 3 else 200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = make_session(backoff_factor=0)
        response = session.get(f"http://127.0.0.1:{server.server_port}/", timeout=5)
    finally:
        server.shutdown()

    assert response.status_code == 200
    assert len(calls) == 3
    assert "MyPacer" in session.headers["User-Agent"]
