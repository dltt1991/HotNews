"""Observable admin operations and the browser-facing localhost boundary."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hotnews.admin.app import AdminApplication
from hotnews.admin import server as admin_server
from hotnews.config import AppConfig, ServerConfig
from hotnews.domain import NewsResult, Schedule, ValidationError
from hotnews.feishu.connection import ConnectionSnapshot
from hotnews.storage.database import Database
from hotnews.storage.runs import RunRepository
from hotnews.storage.subscriptions import SubscriptionRepository


NOW = datetime(2026, 10, 2, 0, tzinfo=timezone.utc)
HOST = "127.0.0.1:8081"
TOKEN = "test-process-csrf-token"


class MemorySocket:
    def __init__(self, request):
        self.incoming = io.BytesIO(request)
        self.outgoing = bytearray()
        self.timeout = None

    def makefile(self, mode, buffering=None):
        return self.incoming

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, value):
        self.timeout = value


def http_request(method, path, body=b"", extra_headers=()):
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: " + HOST,
             "Content-Length: %d" % len(body)]
    lines.extend(extra_headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class AdminTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = AppConfig(database_path=str(Path(directory.name) / "admin.db"))
        self.database = Database(self.config.database_path)
        self.database.migrate()
        self.repository = SubscriptionRepository(self.database)
        self.app = AdminApplication(self.config, self.repository, csrf_token=TOKEN, clock=lambda: NOW)

    def create(self, **changes):
        arguments = dict(chat_id="chat-a", creator_id="member", topic="AI 新闻",
                         keywords=["AI"], search_terms=["AI", "人工智能"], now=NOW)
        arguments.update(changes)
        return self.repository.create(**arguments)

    def request(self, method, path, value=None, headers=None, raw=None, app=None):
        request_headers = {"Host": HOST}
        if method != "GET":
            request_headers.update({"Origin": "http://" + HOST, "X-Hotnews-CSRF": TOKEN,
                                    "Content-Type": "application/json"})
        if headers is not None:
            for name, value_or_none in headers.items():
                if value_or_none is None:
                    request_headers.pop(name, None)
                else:
                    request_headers[name] = value_or_none
        body = raw if raw is not None else (b"" if value is None else json.dumps(value).encode())
        return (app or self.app).handle(method, path, request_headers, body)

    def item(self, response):
        self.assertEqual(response.status, 200)
        return json.loads(response.body)["subscription"]

    def listed(self, path="/api/subscriptions", headers=None):
        response = self.request("GET", path, headers=headers)
        self.assertEqual(response.status, 200)
        return json.loads(response.body)["subscriptions"]

    def route(self, sub, action=""):
        return "/api/subscriptions/" + sub.id + action

    def count(self, table):
        with self.database.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_list_and_get_include_display_metadata_without_csrf(self):
        first = self.create()
        second = self.create(chat_id="chat-b", topic="新能源", keywords=["汽车"])
        with self.database.connect() as connection:
            connection.execute("UPDATE chats SET name = ? WHERE chat_id = ?", ("研发群", "chat-a"))
        values = self.listed()
        self.assertEqual([item["id"] for item in values], [first.id, second.id])
        item = self.item(self.request("GET", self.route(first)))
        self.assertEqual((item["chat_name"], item["display_number"], item["topic"]), ("研发群", 1, "AI 新闻"))
        self.assertEqual(item["keywords"], ["AI"])
        self.assertEqual(item["schedule"], {"kind": "daily", "daily_at": "09:00"})
        self.assertEqual(item["timezone"], "Asia/Shanghai")
        self.assertEqual(item["next_run_at"], "2026-10-02T01:00:00.000000Z")
        self.assertEqual((item["state"], item["search_terms_status"], item["consecutive_failures"]),
                         ("ready", "ready", 0))
        self.assertIsNone(item["last_success_at"])
        self.assertEqual(values[1]["chat_name"], "chat-b")

    def test_connection_status_is_get_only_local_and_sanitized(self):
        snapshot = ConnectionSnapshot(
            "reconnecting", datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 2, 1, 2, tzinfo=timezone.utc), 3,
            "failed wss://open.feishu.cn/ws?ticket=secret via http://u:p@proxy:7890")
        app = AdminApplication(self.config, self.repository, csrf_token=TOKEN,
                               clock=lambda: NOW, status_provider=lambda: snapshot)
        response = self.request("GET", "/api/connection", app=app)
        self.assertEqual(response.status, 200)
        value = json.loads(response.body)["connection"]
        self.assertEqual(value, {
            "state": "reconnecting",
            "connected_at": "2026-10-02T01:00:00.000000Z",
            "last_event_at": "2026-10-02T01:02:00.000000Z",
            "reconnect_attempts": 3,
            "last_error": "failed wss://open.feishu.cn/ws?<redacted> via http://***:***@proxy:7890",
        })
        self.assertNotIn("secret", response.body.decode())
        self.assertEqual(self.request("POST", "/api/connection", {}, app=app).status, 405)
        self.assertEqual(self.request("GET", "/api/connection", headers={"Host": "evil:8081"}, app=app).status, 403)

    def test_filters_combine_group_state_and_keyword_case_insensitively(self):
        keep = self.create(topic="AI Agents", keywords=["大模型"])
        self.create(topic="汽车", keywords=["汽车"])
        self.create(chat_id="chat-b", topic="AI Agents")
        paused = self.create(topic="AI Agents")
        self.repository.pause(paused.id, paused.version, now=NOW)
        self.assertEqual([item["id"] for item in self.listed(
            "/api/subscriptions?chat_id=chat-a&status=ready&keyword=agents")], [keep.id])
        self.assertEqual([item["id"] for item in self.listed(
            "/api/subscriptions?chat_id=chat-a&keyword=%E5%A4%A7%E6%A8%A1%E5%9E%8B")], [keep.id])

    def test_cancelled_history_is_hidden_until_explicitly_included(self):
        sub = self.create()
        self.repository.cancel(sub.id, sub.version, now=NOW)
        self.assertEqual(self.listed(), [])
        self.assertEqual([item["id"] for item in self.listed(
            "/api/subscriptions?include_cancelled=true&status=cancelled")], [sub.id])
        self.assertEqual(self.item(self.request("GET", self.route(sub)))["state"], "cancelled")

    def test_keyword_edit_invalidates_terms_recomputes_time_and_does_not_run(self):
        sub = self.create()
        response = self.request("PATCH", self.route(sub), {"version": sub.version, "topic": "新能源",
            "keywords": ["新能源汽车", "电池"], "schedule": {"kind": "interval", "interval_minutes": 120}})
        item = self.item(response)
        self.assertEqual((item["topic"], item["keywords"], item["version"]), ("新能源", ["新能源汽车", "电池"], 2))
        self.assertEqual((item["state"], item["search_terms"], item["search_terms_status"]),
                         ("search_terms_pending", [], "pending"))
        self.assertEqual(item["next_run_at"], "2026-10-02T02:00:00.000000Z")
        self.assertEqual(self.count("subscription_runs"), 0)

    def test_topic_and_schedule_only_edit_keeps_current_search_terms(self):
        sub = self.create()
        item = self.item(self.request("PATCH", self.route(sub), {"version": 1, "topic": "AI 最新资讯",
            "schedule": {"kind": "daily", "daily_at": "10:30"}}))
        self.assertEqual((item["version"], item["state"], item["search_terms"]), (2, "ready", ["AI", "人工智能"]))
        self.assertEqual(item["next_run_at"], "2026-10-02T02:30:00.000000Z")

    def test_edit_preserves_existing_delivery_records(self):
        sub = self.create()
        self.repository.request_manual_run(sub.id, sub.version, now=NOW)
        runs = RunRepository(self.database)
        run = runs.claim_due("agent", 1, NOW, 60)[0]
        runs.complete(run.id, "agent", [NewsResult("发布", "https://example.com/news", "官方",
            NOW - timedelta(hours=1), "中文摘要", "release")], now=NOW, search_window_days=1)
        current = self.repository.get(sub.id)
        self.item(self.request("PATCH", self.route(sub), {"version": current.version, "keywords": ["新主题"]}))
        self.assertEqual((self.count("articles"), self.count("deliveries"), self.count("outbox")), (1, 1, 1))

    def test_unrepresentable_interval_edit_returns_400_without_changing_row_or_history(self):
        sub = self.create()
        self.repository.request_manual_run(sub.id, sub.version, now=NOW)
        runs = RunRepository(self.database)
        run = runs.claim_due("agent", 1, NOW, 60)[0]
        runs.complete(run.id, "agent", [NewsResult("发布", "https://example.com/news", "官方",
            NOW - timedelta(hours=1), "中文摘要", "release")], now=NOW, search_window_days=1)
        current = self.repository.get(sub.id)
        with self.database.connect() as connection:
            history = {table: [dict(row) for row in connection.execute("SELECT * FROM " + table)]
                       for table in ("articles", "deliveries", "subscription_runs", "outbox")}
        for minutes in (10 ** 12, 10 ** 40):
            with self.subTest(minutes=minutes):
                response = self.request("PATCH", self.route(sub), {"version": current.version,
                    "topic": "不得保存", "keywords": ["不得保存"],
                    "schedule": {"kind": "interval", "interval_minutes": minutes}})
                self.assertEqual(response.status, 400)
                self.assertEqual(json.loads(response.body), {"error": "invalid_request"})
                self.assertEqual(self.repository.get(sub.id), current)
                with self.database.connect() as connection:
                    for table, expected in history.items():
                        self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM " + table)], expected)

    def test_stale_version_for_every_mutation_preserves_newer_subscription(self):
        sub = self.create()
        newer = self.repository.update(sub.id, sub.version, topic="新主题", now=NOW)
        for method, action, fields in (("PATCH", "", {"topic": "旧页面"}), ("POST", "/pause", {}),
                                      ("POST", "/resume", {}), ("POST", "/run-now", {}), ("DELETE", "", {})):
            with self.subTest(action=action, method=method):
                response = self.request(method, self.route(sub, action), {"version": 1, **fields})
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.body)["error"], "version_conflict")
                self.assertEqual(self.repository.get(sub.id), newer)
        self.assertEqual(self.count("subscription_runs"), 0)

    def test_pause_resume_and_run_now_preserve_pause_and_regular_schedule(self):
        sub = self.create()
        paused = self.item(self.request("POST", self.route(sub, "/pause"), {"version": 1}))
        self.assertEqual((paused["state"], paused["version"], paused["next_run_at"]),
                         ("paused", 2, "2026-10-02T01:00:00.000000Z"))
        response = self.request("POST", self.route(sub, "/run-now"), {"version": 2})
        item = self.item(response)
        run_id = json.loads(response.body)["run_id"]
        self.assertEqual((item["state"], item["version"], item["next_run_at"]),
                         ("paused", 3, "2026-10-02T01:00:00.000000Z"))
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM subscription_runs WHERE id = ?", (run_id,)).fetchone()
            self.assertEqual((row["trigger"], row["status"], row["subscription_version"]), ("manual", "pending", 3))
        resumed = self.item(self.request("POST", self.route(sub, "/resume"), {"version": 3}))
        self.assertEqual((resumed["state"], resumed["version"]), ("ready", 4))

    def test_edit_pending_paused_row_keeps_pause_and_run_now_requires_terms(self):
        sub = self.create()
        self.repository.pause(sub.id, 1, now=NOW)
        edited = self.item(self.request("PATCH", self.route(sub), {"version": 2, "keywords": ["更新"]}))
        self.assertEqual((edited["state"], edited["search_terms_status"]), ("paused", "pending"))
        self.assertEqual(self.request("POST", self.route(sub, "/run-now"), {"version": 3}).status, 400)
        self.assertEqual(self.count("subscription_runs"), 0)
        self.assertEqual(self.item(self.request("POST", self.route(sub, "/resume"), {"version": 3}))["state"],
                         "search_terms_pending")

    def test_delete_is_soft_and_cancelled_rows_cannot_be_modified(self):
        sub = self.create()
        item = self.item(self.request("DELETE", self.route(sub), {"version": 1}))
        self.assertEqual((item["state"], item["version"], item["cancelled_at"]),
                         ("cancelled", 2, "2026-10-02T00:00:00.000000Z"))
        self.assertEqual(self.count("subscriptions"), 1)
        self.assertEqual(self.request("POST", self.route(sub, "/resume"), {"version": 2}).status, 400)

    def test_malformed_duplicate_nonfinite_or_nonobject_json_is_rejected(self):
        sub = self.create()
        for raw in (b"{", b"[]", b"null", b"\xff", b'{"version":1,"version":2}',
                    b'{"version":NaN}', b'{"version":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}"):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(self.request("PATCH", self.route(sub), raw=raw).status, 400)
        self.assertEqual(self.repository.get(sub.id), sub)

    def test_unknown_fields_and_invalid_edit_values_cannot_change_a_row(self):
        sub = self.create()
        cases = ({}, {"topic": "新"}, {"version": 1},
                 {"version": True, "topic": "新"}, {"version": 0, "topic": "新"},
                 {"version": 1, "topic": None}, {"version": 1, "topic": "x" * 201},
                 {"version": 1, "keywords": []}, {"version": 1, "keywords": "AI"},
                 {"version": 1, "keywords": ["x" * 81]}, {"version": 1, "keywords": ["x"] * 21},
                 {"version": 1, "keywords": [False]}, {"version": 1, "topic": " "},
                 {"version": 1, "search_terms": ["injected"]}, {"version": 1, "chat_id": "other"},
                 {"version": 1, "schedule": {"kind": "interval", "interval_minutes": 4}},
                 {"version": 1, "schedule": {"kind": "daily", "daily_at": "9:00"}},
                 {"version": 1, "schedule": {"kind": "manual"}},
                 {"version": 1, "schedule": {"kind": "daily", "daily_at": "10:00", "sql": "SELECT"}})
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(self.request("PATCH", self.route(sub), value).status, 400)
                self.assertEqual(self.repository.get(sub.id), sub)
        self.assertEqual(self.request("POST", self.route(sub, "/pause"), {"version": 1, "topic": "新"}).status, 400)

    def test_mutations_require_json_content_type(self):
        sub = self.create()
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "application/jsonp"):
            response = self.request("PATCH", self.route(sub), {"version": 1, "topic": "新"},
                                    {"Content-Type": content_type})
            self.assertEqual(response.status, 415)
        self.assertEqual(self.repository.get(sub.id), sub)
        self.assertEqual(self.request("PATCH", self.route(sub), {"version": 1, "topic": "新"},
                                     {"Content-Type": "application/json; charset=utf-8"}).status, 200)

    def test_nonlocal_malformed_missing_or_wrong_port_host_is_forbidden(self):
        sub = self.create()
        for host in (None, "evil.example:8081", "localhost.evil:8081", "127.0.0.1.evil:8081",
                     "localhost@evil:8081", "127.0.0.2:8081", "[::1]:8081", "localhost:8082",
                     "localhost", "localhost:8081/", "localhost:8081,evil.example", " localhost:8081"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", self.route(sub), headers={"Host": host}).status, 403)
                self.assertEqual(self.request("GET", "/api/session", headers={"Host": host}).status, 403)
        self.assertEqual(self.request("GET", self.route(sub), headers={"Host": "localhost:8081"}).status, 200)

    def test_missing_mismatched_or_malformed_origin_is_forbidden(self):
        sub = self.create()
        for origin in (None, "null", "https://" + HOST, "http://evil.example:8081", "http://localhost:8081",
                       "http://127.0.0.1:8082", "http://" + HOST + "/", "http://" + HOST + "?x=1",
                       "http://" + HOST + " http://evil.example", "http://user@" + HOST):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("PATCH", self.route(sub), {"version": 1, "topic": "新"},
                    {"Origin": origin}).status, 403)
        self.assertEqual(self.repository.get(sub.id), sub)
        self.assertEqual(self.request("PATCH", self.route(sub), {"version": 1, "topic": "新"},
            {"Host": "localhost:8081", "Origin": "http://localhost:8081"}).status, 200)

    def test_missing_wrong_or_duplicate_case_csrf_is_forbidden(self):
        sub = self.create()
        for value in (None, "wrong", "错误令牌", TOKEN + "," + TOKEN):
            self.assertEqual(self.request("POST", self.route(sub, "/pause"), {"version": 1},
                                         {"X-Hotnews-CSRF": value}).status, 403)
        self.assertEqual(self.request("POST", self.route(sub, "/pause"), {"version": 1},
            {"x-hotnews-csrf": "wrong"}).status, 403)
        self.assertEqual(self.repository.get(sub.id), sub)

    def test_lowercase_http_headers_are_accepted(self):
        sub = self.create()
        body = json.dumps({"version": 1}).encode()
        headers = {"host": HOST, "origin": "http://" + HOST, "x-hotnews-csrf": TOKEN,
                   "content-type": "application/json"}
        self.assertEqual(self.app.handle("POST", self.route(sub, "/pause"), headers, body).status, 200)

    def test_session_bootstrap_without_origin_is_uncached_and_never_enables_cors(self):
        for headers in ({}, {"Origin": "http://evil.example"}):
            response = self.request("GET", "/api/session", headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.body), {"csrf_token": TOKEN})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertFalse(any(key.lower().startswith("access-control-") for key in response.headers))
        self.assertEqual(self.request("POST", "/api/session", {}).status, 405)

    def test_default_process_tokens_are_distinct_and_one_process_token_cannot_mutate_another(self):
        first = AdminApplication(self.config, self.repository)
        second = AdminApplication(self.config, self.repository)
        one = json.loads(self.request("GET", "/api/session", app=first).body)["csrf_token"]
        two = json.loads(self.request("GET", "/api/session", app=second).body)["csrf_token"]
        self.assertGreaterEqual(len(one), 32)
        self.assertNotEqual(one, two)
        sub = self.create()
        self.assertEqual(self.request("POST", self.route(sub, "/pause"), {"version": 1},
                                     {"X-Hotnews-CSRF": one}, app=second).status, 403)

    def test_unknown_or_duplicate_query_filters_are_rejected(self):
        for query in ("state=ready", "status=unknown", "include_cancelled=yes", "chat_id=",
                      "keyword=", "status=ready&status=paused", "sql=SELECT"):
            self.assertEqual(self.request("GET", "/api/subscriptions?" + query).status, 400)
        sub = self.create()
        self.assertEqual(self.request("PATCH", self.route(sub) + "?keyword=AI", {"version": 1, "topic": "新"}).status, 400)

    def test_exact_routes_do_not_allow_creating_subscriptions(self):
        sub = self.create()
        self.assertEqual(self.request("POST", "/api/subscriptions", {}).status, 405)
        self.assertEqual(self.request("GET", "/api/subscriptions/missing").status, 404)
        self.assertEqual(self.request("PATCH", "/api/subscriptions/missing", {"version": 1, "topic": "新"}).status, 404)
        for path in (self.route(sub) + "/unknown", self.route(sub) + "/pause/", "/api/subscriptions/", "/elsewhere"):
            self.assertEqual(self.request("GET", path).status, 404)
        response = self.request("GET", self.route(sub, "/pause"))
        self.assertEqual((response.status, response.headers["Allow"]), (405, "POST"))
        self.assertEqual(self.count("subscriptions"), 1)

    def test_body_size_and_framing_are_checked_before_mutation(self):
        sub = self.create()
        for headers in ({"Content-Length": "bad"}, {"Content-Length": "-1"}, {"Content-Length": "3"},
                        {"Transfer-Encoding": "chunked"}):
            self.assertEqual(self.request("POST", self.route(sub, "/pause"), {"version": 1}, headers).status, 400)
        self.assertEqual(self.request("POST", self.route(sub, "/pause"), raw=b" " * (self.app.max_body_bytes + 1)).status, 413)
        self.assertEqual(self.request("POST", self.route(sub, "/pause"), raw=b"",
            headers={"Content-Length": str(self.app.max_body_bytes + 1)}).status, 413)
        self.assertEqual(self.repository.get(sub.id), sub)

    def test_storage_errors_return_safe_json_without_echoing_sql_or_diagnostics(self):
        sub = self.create()
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER reject_edit BEFORE UPDATE ON subscriptions "
                               "BEGIN SELECT RAISE(ABORT, 'secret-database-diagnostic'); END")
        response = self.request("POST", self.route(sub, "/pause"), {"version": 1})
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"secret-database-diagnostic", response.body)
        self.assertEqual(self.repository.get(sub.id), sub)

    def test_adapter_uses_the_application_security_and_response(self):
        sub = self.create()
        request = http_request("POST", self.route(sub, "/pause"), b'{"version":1}',
            ("Origin: http://" + HOST, "X-Hotnews-CSRF: " + TOKEN, "Content-Type: application/json"))
        socket = MemorySocket(request)
        admin_server.make_handler(self.app)(socket, ("127.0.0.1", 1234), object())
        header, body = bytes(socket.outgoing).split(b"\r\n\r\n", 1)
        self.assertIn(b"200 OK", header)
        self.assertEqual(json.loads(body)["subscription"]["state"], "paused")
        self.assertLessEqual(socket.timeout, 10)

    def test_adapter_rejects_duplicate_host_and_does_not_read_oversized_body(self):
        socket = MemorySocket(http_request("GET", "/api/session", extra_headers=("Host: evil.example",)))
        admin_server.make_handler(self.app)(socket, ("127.0.0.1", 1234), object())
        self.assertIn(b"403 Forbidden", socket.outgoing)
        self.assertNotIn(TOKEN.encode(), socket.outgoing)
        socket = MemorySocket(("POST /api/subscriptions/missing/pause HTTP/1.1\r\nHost: " + HOST +
                               "\r\nContent-Length: %d\r\n\r\n" % (self.app.max_body_bytes + 1)).encode())
        class GuardedInput(io.BytesIO):
            def read(inner, size=-1):
                raise AssertionError("oversized body must not be read")
        socket.incoming = GuardedInput(socket.incoming.getvalue())
        admin_server.make_handler(self.app)(socket, ("127.0.0.1", 1234), object())
        self.assertIn(b"413 Request Entity Too Large", socket.outgoing)

    def test_server_only_binds_127_and_shuts_down_with_stop_event(self):
        stopped = threading.Event()
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
                socket = MemorySocket(http_request("GET", "/api/session"))
                inner.handler(socket, ("127.0.0.1", 1234), inner)
                observed["response"] = bytes(socket.outgoing)
                stopped.set()
        with patch.object(admin_server, "ThreadingHTTPServer", ServerBoundary):
            admin_server.serve_admin(self.config, stopped)
        self.assertEqual(observed["address"], ("127.0.0.1", 8081))
        self.assertTrue(observed["closed"])
        self.assertIn(b"200 OK", observed["response"])
        for host in ("0.0.0.0", "localhost", "::1", "192.168.1.2"):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                admin_server.serve_admin(replace(self.config, admin=ServerConfig(host, 8081)), threading.Event())


if __name__ == "__main__":
    unittest.main()
