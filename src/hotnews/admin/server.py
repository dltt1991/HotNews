"""HTTP framing adapter for the strictly local management application."""

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config import AppConfig
from ..domain import ValidationError
from ..http import supervise_request_errors
from ..storage.database import Database
from ..storage.subscriptions import SubscriptionRepository
from .app import AdminApplication


_READ_TIMEOUT_SECONDS = 5


def make_handler(app: AdminApplication):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(_READ_TIMEOUT_SECONDS)

        def _dispatch(self):
            body = b""
            length = self.headers.get("Content-Length", "0")
            if re.fullmatch(r"[0-9]+", length) and "Transfer-Encoding" not in self.headers:
                size = int(length)
                if size <= app.max_body_bytes:
                    try:
                        body = self.rfile.read(size)
                    except OSError:
                        self.close_connection = True
                        return
            headers = {}
            for name, value in self.headers.items():
                key = name.lower()
                headers[key] = headers[key] + "," + value if key in headers else value
            response = app.handle(self.command, self.path, headers, body)
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(response.body)

        do_GET = _dispatch
        do_POST = _dispatch
        do_PATCH = _dispatch
        do_DELETE = _dispatch
        do_PUT = _dispatch
        do_HEAD = _dispatch
        do_OPTIONS = _dispatch

        def log_message(self, format, *args):
            # Request paths can contain sensitive keyword filters.
            pass

    return Handler


def serve_admin(config: AppConfig, stop_event: threading.Event) -> None:
    """Run on IPv4 loopback until the owning runtime signals shutdown."""
    if config.admin.host != "127.0.0.1":
        raise ValidationError("admin must bind to 127.0.0.1")
    database = Database(config.database_path)
    database.migrate()
    app = AdminApplication(config, SubscriptionRepository(database))
    with ThreadingHTTPServer(("127.0.0.1", config.admin.port), make_handler(app)) as server:
        failed = supervise_request_errors(server, stop_event)
        server.timeout = 0.5
        while not stop_event.is_set():
            server.handle_request()
        if failed.is_set():
            raise RuntimeError("admin request handler failed")
