"""Callbacks are authenticated and committed atomically before acknowledgement."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from hotnews.config import AppConfig, ServerConfig
from hotnews.domain import ValidationError
from hotnews.feishu import gateway
from hotnews.feishu.client import FeishuClient
from hotnews.feishu.gateway import GatewayApplication
from hotnews.storage.database import Database
from hotnews.storage.events import EventRepository
from hotnews.storage.outbox import OutboxRepository
from tests.unit.test_feishu import (ScriptedTransport, config, encrypted_payload, message_event,
                                    response, signed_headers, token)


JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class MemorySocket:
    """Replace the socket boundary while the real HTTP parser and app execute."""

    def __init__(self, request):
        self.incoming = io.BytesIO(request)
        self.outgoing = bytearray()
        self.timeout = None

    def makefile(self, mode, buffering=None):
        return self.incoming

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, timeout):
        self.timeout = timeout


def http_request(body, declared_length=None):
    length = len(body) if declared_length is None else declared_length
    return ("POST /callbacks/feishu HTTP/1.1\r\nHost: localhost\r\n"
            "Content-Type: application/json\r\nContent-Length: %s\r\n\r\n" % length).encode() + body


class GatewayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = AppConfig(database_path=str(Path(directory.name) / "gateway.db"))
        self.database = Database(self.settings.database_path)
        self.database.migrate()
        self.app = GatewayApplication(self.settings, config(), self.database)

    def request(self, payload, app=None, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return (app or self.app).handle("POST", "/callbacks/feishu", headers or JSON_HEADERS, body)

    def rows(self, table):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM %s ORDER BY rowid" % table).fetchall()

    def assert_empty_queues(self):
        self.assertEqual(self.rows("inbound_events"), [])
        self.assertEqual(self.rows("outbox"), [])

    def test_plain_and_encrypted_verification_return_challenge_without_queueing(self):
        payload = {"type": "url_verification", "token": "test-verification-token",
                   "challenge": 'challenge"\n中文'}
        for encrypted in (False, True):
            app = GatewayApplication(self.settings, config(encrypted), self.database)
            value = encrypted_payload(payload) if encrypted else payload
            response = self.request(value, app)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.body), {"challenge": 'challenge"\n中文'})
        self.assert_empty_queues()

    def test_accepted_event_preserves_raw_text_and_queues_one_fixed_ack(self):
        response = self.request(message_event())
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {})
        rows = self.rows("inbound_events")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["event_id"], row["message_id"], row["chat_id"], row["sender_id"]),
                         ("evt_1", "om_1", "oc_group", "ou_member"))
        self.assertEqual((row["raw_text"], row["text"], row["status"]),
                         ("@_user_1 订阅 AI，每天 9 点", "订阅 AI，每天 9 点", "pending"))
        self.assertEqual(json.loads(row["mentions_json"]), [{
            "key": "@_user_1", "id": {"open_id": "ou_bot", "union_id": "", "user_id": ""},
            "name": "热点机器人", "tenant_key": "tenant_test"}])
        outbox = self.rows("outbox")
        self.assertEqual(len(outbox), 1)
        self.assertEqual((outbox[0]["chat_id"], outbox[0]["kind"], outbox[0]["status"]),
                         ("oc_group", "text", "pending"))
        self.assertEqual(json.loads(outbox[0]["content_json"]),
                         {"text": "已收到，将在 5 分钟内处理。"})

    def test_response_waits_until_the_database_commit_succeeds(self):
        staged = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        class GatedDatabase(Database):
            @contextmanager
            def connect(inner):
                with super().connect() as connection:
                    yield connection
                    staged.set()
                    if not release.wait(5):
                        raise RuntimeError("test transaction gate timed out")

        app = GatewayApplication(self.settings, config(), GatedDatabase(self.database.path))
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.request, message_event(), app)
            try:
                self.assertTrue(staged.wait(5), "callback did not stage its transaction")
                self.assertFalse(pending.done(), "callback replied before commit")
                self.assert_empty_queues()
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).status, 200)
        self.assertEqual(len(self.rows("inbound_events")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_duplicate_event_and_message_retries_do_not_duplicate_either_queue(self):
        self.assertEqual(self.request(message_event()).status, 200)
        for changed_field in (None, "event_id", "message_id"):
            payload = message_event()
            if changed_field == "event_id":
                payload["header"]["event_id"] = "evt_retry"
            if changed_field == "message_id":
                payload["event"]["message"]["message_id"] = "om_retry"
            self.assertEqual(self.request(payload).status, 200)
        self.assertEqual(len(self.rows("inbound_events")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_out_of_order_delivery_retries_preserve_the_first_event_and_ack(self):
        first = message_event()
        second = message_event()
        second["header"].update(event_id="evt_2", create_time="1790906460000")
        second["event"]["message"].update(message_id="om_2", create_time="1790906460000")
        for payload in (second, first, second, first):
            self.assertEqual(self.request(payload).status, 200)
        self.assertEqual([row["event_id"] for row in self.rows("inbound_events")], ["evt_2", "evt_1"])
        self.assertEqual(len(self.rows("outbox")), 2)

    def test_concurrent_duplicate_callbacks_commit_one_event_and_one_ack(self):
        barrier = threading.Barrier(2)

        def receive():
            app = GatewayApplication(self.settings, config(), Database(self.database.path))
            barrier.wait()
            return self.request(message_event(), app).status

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(receive) for _ in range(2)]
            self.assertEqual([future.result(timeout=10) for future in futures], [200, 200])
        self.assertEqual(len(self.rows("inbound_events")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_ignored_messages_do_not_queue_an_ack(self):
        for reason in ("private", "nontext", "bot", "self", "no_mention", "other_bot", "event_type"):
            payload = message_event()
            message = payload["event"]["message"]
            if reason == "private":
                message["chat_type"] = "p2p"
            elif reason == "nontext":
                message["message_type"] = "image"
            elif reason == "bot":
                payload["event"]["sender"]["sender_type"] = "app"
            elif reason == "self":
                payload["event"]["sender"]["sender_id"]["open_id"] = "ou_bot"
            elif reason == "no_mention":
                message["mentions"] = []
            elif reason == "other_bot":
                message["mentions"][0]["id"]["open_id"] = "ou_other"
            else:
                payload["header"]["event_type"] = "im.chat.updated_v1"
            with self.subTest(reason=reason):
                self.assertEqual(self.request(payload).status, 200)
                self.assert_empty_queues()

    def test_gateway_requires_resolved_current_bot_identity(self):
        for bot_open_id in (None, "", " "):
            with self.subTest(bot_open_id=bot_open_id), self.assertRaises(ValidationError):
                GatewayApplication(self.settings, config(bot_open_id=bot_open_id), self.database)

    def test_invalid_token_and_application_are_forbidden_without_queueing(self):
        for field in ("token", "app_id"):
            payload = message_event()
            payload["header"][field] = "invalid-sensitive-value"
            response = self.request(payload)
            self.assertEqual(response.status, 403)
            self.assertNotIn(b"invalid-sensitive-value", response.body)
        challenge = {"type": "url_verification", "token": "wrong", "challenge": "test"}
        self.assertEqual(self.request(challenge).status, 403)
        self.assert_empty_queues()

    def test_encrypted_callback_authenticates_the_exact_body_before_queueing(self):
        app = GatewayApplication(self.settings, config(True), self.database)
        body = json.dumps(encrypted_payload(message_event()), indent=2).encode("utf-8")
        for headers in (JSON_HEADERS, {**JSON_HEADERS, **signed_headers(body + b" ")}):
            self.assertEqual(app.handle("POST", "/callbacks/feishu", headers, body).status, 403)
            self.assert_empty_queues()
        response = app.handle("POST", "/callbacks/feishu", {**JSON_HEADERS, **signed_headers(body)}, body)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.rows("inbound_events")[0]["raw_text"], "@_user_1 订阅 AI，每天 9 点")

    def test_malformed_json_or_event_is_bad_request(self):
        for body in (b"{", b"[]", b"\xff"):
            with self.subTest(body=body):
                self.assertEqual(self.app.handle("POST", "/callbacks/feishu", JSON_HEADERS, body).status, 400)
        malformed = message_event()
        malformed["event"]["message"]["content"] = "{"
        self.assertEqual(self.request(malformed).status, 400)
        self.assert_empty_queues()

    def test_non_json_numeric_value_in_mentions_cannot_create_a_partial_ack(self):
        payload = message_event()
        payload["event"]["message"]["mentions"][0]["name"] = float("nan")
        self.assertEqual(self.request(payload).status, 400)
        self.assert_empty_queues()

    def test_callback_rejects_non_json_content_type(self):
        for headers in ({}, {"Content-Type": "text/plain"}):
            with self.subTest(headers=headers):
                response = self.app.handle("POST", "/callbacks/feishu", headers, b"{}")
                self.assertEqual(response.status, 415)
        self.assert_empty_queues()

    def test_body_limit_is_enforced_for_actual_and_declared_lengths(self):
        app = GatewayApplication(replace(self.settings, callback=ServerConfig("127.0.0.1", 8080, 10)),
                                 config(), self.database)
        self.assertEqual(app.handle("POST", "/callbacks/feishu", JSON_HEADERS, b"x" * 11).status, 413)
        self.assertEqual(app.handle("POST", "/callbacks/feishu", {**JSON_HEADERS, "Content-Length": "11"},
                                    b"").status, 413)
        self.assertEqual(app.handle("POST", "/callbacks/feishu", JSON_HEADERS, b"x" * 10).status, 400)
        self.assert_empty_queues()

    def test_invalid_http_framing_is_bad_request(self):
        for header in ({"Content-Length": "-1"}, {"Content-Length": "bad"},
                       {"Content-Length": "3"}, {"Transfer-Encoding": "chunked"}):
            response = self.app.handle("POST", "/callbacks/feishu", {**JSON_HEADERS, **header}, b"{}")
            self.assertEqual(response.status, 400)
        self.assert_empty_queues()

    def test_health_route_and_wrong_paths_and_methods(self):
        healthy = self.app.handle("GET", "/healthz", {}, b"")
        self.assertEqual((healthy.status, json.loads(healthy.body)), (200, {"status": "ok"}))
        for path in ("/", "/callbacks/wecom", "/callbacks/feishu/", "/api/subscriptions"):
            self.assertEqual(self.app.handle("POST", path, JSON_HEADERS, b"{}").status, 404)
        wrong_method = self.app.handle("GET", "/callbacks/feishu", {}, b"")
        self.assertEqual(wrong_method.status, 405)
        self.assertEqual(wrong_method.headers["Allow"], "POST")
        self.assert_empty_queues()

    def test_outbox_storage_failure_rolls_back_the_inbound_event(self):
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox "
                               "BEGIN SELECT RAISE(ABORT, 'secret-storage-error'); END")
        response = self.request(message_event())
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"secret-storage-error", response.body)
        self.assert_empty_queues()
        with self.database.connect() as connection:
            connection.execute("DROP TRIGGER fail_outbox")
        self.assertEqual(self.request(message_event()).status, 200)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_failed_commit_rolls_back_both_queues_and_returns_no_false_ack(self):
        class FailingCommitDatabase(Database):
            @contextmanager
            def connect(inner):
                with super().connect() as connection:
                    yield connection
                    raise sqlite3.OperationalError("secret-commit-error")

        app = GatewayApplication(self.settings, config(), FailingCommitDatabase(self.database.path))
        response = self.request(message_event(), app)
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"secret-commit-error", response.body)
        self.assert_empty_queues()

    def test_repositories_share_callers_transaction_without_committing_or_closing_it(self):
        events = EventRepository(self.database)
        outbox = OutboxRepository(self.database)
        event = {"event_id": "evt_shared", "message_id": "om_shared", "chat_id": "oc_group",
                 "sender_id": "ou_member", "raw_text": "@bot 帮助", "text": "帮助",
                 "received_at": datetime.now(timezone.utc)}
        with self.assertRaisesRegex(RuntimeError, "rollback requested"):
            with self.database.connect() as connection:
                self.assertTrue(events.insert(event, connection=connection))
                self.assertFalse(events.insert(event, connection=connection))
                first = outbox.enqueue("oc_group", "text", {"text": "ack"}, "shared", connection=connection)
                same = outbox.enqueue("oc_group", "text", {"text": "duplicate"}, "shared", connection=connection)
                self.assertEqual(first, same)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
                self.assert_empty_queues()
                raise RuntimeError("rollback requested")
        self.assert_empty_queues()

    def test_socket_adapter_parses_request_and_writes_application_response(self):
        socket = MemorySocket(http_request(json.dumps(message_event()).encode()))
        gateway.make_handler(self.app)(socket, ("127.0.0.1", 1234), object())
        header, body = bytes(socket.outgoing).split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", header)
        self.assertIn(b"Content-Type: application/json; charset=utf-8", header)
        self.assertIn(b"Content-Length: 2", header)
        self.assertEqual(json.loads(body), {})
        self.assertIsNotNone(socket.timeout)
        self.assertLessEqual(socket.timeout, 10)
        self.assertEqual(len(self.rows("inbound_events")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_socket_adapter_does_not_read_an_oversized_declared_body(self):
        socket = MemorySocket(http_request(b"", self.settings.callback.max_body_bytes + 1))

        class GuardedInput(io.BytesIO):
            def read(inner, size=-1):
                raise AssertionError("oversized request body must not be read")

        socket.incoming = GuardedInput(socket.incoming.getvalue())
        gateway.make_handler(self.app)(socket, ("127.0.0.1", 1234), object())
        self.assertIn(b"413 Request Entity Too Large", socket.outgoing)
        self.assert_empty_queues()

    def test_service_binds_configured_address_and_accepts_injected_bot_identity(self):
        stopped = threading.Event()
        settings = replace(self.settings, callback=ServerConfig("127.0.0.1", 8765, 1024 * 1024))
        observed = {}

        class ServerBoundary:
            def __init__(inner, address, handler):
                observed["address"] = address
                inner.handler = handler

            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                observed["closed"] = True

            def handle_request(inner):
                socket = MemorySocket(http_request(json.dumps(message_event()).encode()))
                inner.handler(socket, ("127.0.0.1", 1234), inner)
                observed["response"] = bytes(socket.outgoing)
                stopped.set()

        with patch.object(gateway, "ThreadingHTTPServer", ServerBoundary):
            gateway.serve_gateway(settings, stopped, config())
        self.assertEqual(observed["address"], ("127.0.0.1", 8765))
        self.assertTrue(observed["closed"])
        self.assertIn(b"200 OK", observed["response"])
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_two_argument_service_loads_environment_and_resolves_bot_identity(self):
        stopped = threading.Event()
        transport = ScriptedTransport([token(), response({"code": 0, "msg": "ok", "bot": {
            "app_name": "热点机器人", "avatar_url": "https://example.com/bot.png",
            "open_id": "ou_bot"}})])
        environment = {"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "test-app-secret",
                       "FEISHU_VERIFICATION_TOKEN": "test-verification-token"}
        observed = {}

        class ServerBoundary:
            def __init__(inner, address, handler):
                inner.handler = handler

            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                observed["closed"] = True

            def handle_request(inner):
                socket = MemorySocket(http_request(json.dumps(message_event()).encode()))
                inner.handler(socket, ("127.0.0.1", 1234), inner)
                observed["response"] = bytes(socket.outgoing)
                stopped.set()

        with patch.dict("os.environ", environment, clear=True), \
                patch.object(gateway, "ThreadingHTTPServer", ServerBoundary), \
                patch.object(gateway, "FeishuClient", lambda credentials: FeishuClient(credentials, transport)):
            gateway.serve_gateway(self.settings, stopped)
        self.assertTrue(observed["closed"])
        self.assertIn(b"200 OK", observed["response"])
        self.assertEqual(len(self.rows("outbox")), 1)


if __name__ == "__main__":
    unittest.main()
