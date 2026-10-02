"""Small authenticated callback boundary; event processing happens in Codex."""

from dataclasses import replace
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping, Optional

from ..config import AppConfig, FeishuConfig, load_feishu_config
from ..domain import HttpResponse, ValidationError
from ..http import ContentLengthError, bounded_content_length, supervise_request_errors
from ..storage.database import Database
from ..storage.events import EventRepository
from ..storage.outbox import OutboxRepository
from .client import FeishuClient
from .events import decode_request, normalize_event, url_verification_response


ACKNOWLEDGEMENT = "已收到，将在 5 分钟内处理。"
_READ_TIMEOUT_SECONDS = 5


def _json_response(status: int, payload: dict, headers: Optional[dict] = None) -> HttpResponse:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return HttpResponse(status, {"Content-Type": "application/json; charset=utf-8",
                                 "Content-Length": str(len(body)), **(headers or {})}, body)


class GatewayApplication:
    """Authenticate and queue a callback without sending messages or interpreting it."""

    def __init__(self, config: AppConfig, feishu_config: FeishuConfig,
                 database: Optional[Database] = None):
        if not isinstance(feishu_config.bot_open_id, str) or not feishu_config.bot_open_id.strip():
            raise ValidationError("gateway requires a resolved Feishu bot open ID")
        self.feishu_config = feishu_config
        self.max_body_bytes = config.callback.max_body_bytes or 1024 * 1024
        self.database = database if database is not None else Database(config.database_path)
        self.events = EventRepository(self.database)
        self.outbox = OutboxRepository(self.database)

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        route = path.split("?", 1)[0]
        if route == "/healthz":
            if method != "GET":
                return _json_response(405, {"error": "method not allowed"}, {"Allow": "GET"})
            return _json_response(200, {"status": "ok"})
        if route != "/callbacks/feishu":
            return _json_response(404, {"error": "not found"})
        if method != "POST":
            return _json_response(405, {"error": "method not allowed"}, {"Allow": "POST"})
        lowered = {key.lower(): value for key, value in headers.items()}
        if len(body) > self.max_body_bytes:
            return _json_response(413, {"error": "callback body too large"})
        declared_length = lowered.get("content-length")
        if declared_length is not None:
            try:
                length = bounded_content_length(declared_length, self.max_body_bytes)
            except ContentLengthError as error:
                return _json_response(error.status, {"error": "callback body too large" if error.status == 413 else "invalid request framing"})
            if length != len(body):
                return _json_response(400, {"error": "invalid request framing"})
        if "transfer-encoding" in lowered:
            return _json_response(400, {"error": "unsupported request framing"})
        if lowered.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return _json_response(415, {"error": "callback requires JSON"})
        try:
            payload = decode_request(body, headers, self.feishu_config)
            challenge = url_verification_response(payload, self.feishu_config)
            if challenge is not None:
                return _json_response(200, challenge)
            event = normalize_event(payload, self.feishu_config)
            if event is None:
                return _json_response(200, {})
            message = payload["event"]["message"]
            values = dict(vars(event), raw_text=json.loads(message["content"])["text"],
                          mentions=message.get("mentions", []))
        except PermissionError:
            return _json_response(403, {"error": "callback authentication failed"})
        except (ValidationError, ValueError, UnicodeError):
            return _json_response(400, {"error": "invalid callback"})
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.events.insert(values, connection=connection):
                    self.outbox.enqueue(event.chat_id, "text", {"text": ACKNOWLEDGEMENT},
                                        "event:%s:ack" % event.event_id, connection=connection)
        except (sqlite3.Error, OSError, UnicodeError):
            return _json_response(500, {"error": "callback storage unavailable"})
        except (ValueError, TypeError):
            return _json_response(400, {"error": "invalid callback"})
        # Exiting Database.connect has committed both rows before this response.
        return _json_response(200, {})


def make_handler(app: GatewayApplication):
    """Translate HTTP framing only; routing and persistence belong to the app."""
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(_READ_TIMEOUT_SECONDS)

        def _dispatch(self):
            body = b""
            declared_length = self.headers.get("Content-Length", "0")
            if "Transfer-Encoding" not in self.headers:
                try:
                    size = bounded_content_length(declared_length, app.max_body_bytes)
                except ContentLengthError:
                    pass
                else:
                    try:
                        body = self.rfile.read(size)
                    except OSError:
                        self.close_connection = True
                        return
            response = app.handle(self.command, self.path, dict(self.headers.items()), body)
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
        do_HEAD = _dispatch
        do_PUT = _dispatch
        do_PATCH = _dispatch
        do_DELETE = _dispatch
        do_OPTIONS = _dispatch

        def log_message(self, format, *args):
            # BaseHTTPRequestHandler access logs contain the raw URL/query.
            pass

    return Handler


def serve_gateway(config: AppConfig, stop_event: threading.Event,
                  feishu_config: Optional[FeishuConfig] = None) -> None:
    """Serve until stopped; the runtime can inject its resolved bot identity."""
    if feishu_config is None:
        feishu_config = load_feishu_config(os.environ)
        feishu_config = replace(feishu_config, bot_open_id=FeishuClient(feishu_config).get_bot_open_id())
    database = Database(config.database_path)
    database.migrate()
    app = GatewayApplication(config, feishu_config, database)
    with ThreadingHTTPServer((config.callback.host, config.callback.port), make_handler(app)) as server:
        failed = supervise_request_errors(server, stop_event)
        server.timeout = 0.5
        while not stop_event.is_set():
            server.handle_request()
        if failed.is_set():
            raise RuntimeError("callback request handler failed")
