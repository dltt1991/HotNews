"""Runtime sends cross the real client and SQLite boundary; only HTTP is fake."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import HTTPException, IncompleteRead
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from hotnews.config import AppConfig, FeishuConfig, ServerConfig, WorkerConfig
from hotnews.domain import HttpResponse, NewsResult, Schedule, ValidationError
from hotnews.feishu.client import FeishuClient
from hotnews.storage.database import Database
from hotnews.storage.events import _datetime
from hotnews.storage.outbox import OutboxRepository
from hotnews.storage.runs import RunRepository
from hotnews.storage.subscriptions import SubscriptionRepository


NOW = datetime(2026, 10, 2, 2, tzinfo=timezone.utc)
SECRET = "never-log-this-secret"


class Transport:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.messages = []
        self.tokens = 0
        self.bot_requests = 0

    def request(self, method, url, headers=None, body=None, timeout=15):
        if url.endswith("tenant_access_token/internal"):
            self.tokens += 1
            value = {"code": 0, "tenant_access_token": "token-%d" % self.tokens, "expire": 7200}
            return HttpResponse(200, {}, json.dumps(value).encode())
        if url.endswith("/bot/v3/info"):
            self.bot_requests += 1
            return HttpResponse(200, {}, b'{"code":0,"bot":{"open_id":"ou_bot"}}')
        self.messages.append(json.loads(body.decode()))
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return HttpResponse(200, {}, b'{"code":0,"data":{"message_id":"om_confirmed"}}')


def failure(status, code, headers=None):
    return HttpResponse(status, headers or {}, json.dumps({"code": code, "msg": SECRET}).encode())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = AppConfig(database_path=str(Path(directory.name) / "runtime.db"))
        self.database = Database(self.config.database_path)
        self.database.migrate()
        self.outbox = OutboxRepository(self.database)
        self.subscriptions = SubscriptionRepository(self.database)
        self.runs = RunRepository(self.database)
        # Retry/error cases assert their logs where relevant; other expected
        # diagnostics stay isolated from unittest's human-readable output.
        logger = logging.getLogger("hotnews.runtime")
        for name, value in (("handlers", [logging.NullHandler()]), ("propagate", False)):
            isolated = patch.object(logger, name, value)
            isolated.start()
            self.addCleanup(isolated.stop)

    def worker(self, transport=None, config=None):
        from hotnews.runtime import OutboxWorker
        transport = transport or Transport()
        client = FeishuClient(FeishuConfig("app", SECRET, "verification"), transport)
        return OutboxWorker(config or self.config, client, clock=lambda: 0), transport

    def rows(self, table):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]

    def prepare(self, chat="chat", manual=False, schedule=None):
        sub = self.subscriptions.create(chat, "member", "AI", ["AI"], ["artificial intelligence"],
                                        schedule or Schedule("interval", interval_minutes=5),
                                        now=NOW - timedelta(days=3))
        if manual:
            sub = self.subscriptions.pause(sub.id, sub.version, now=NOW)
            self.subscriptions.request_manual_run(sub.id, sub.version, now=NOW)
            sub = self.subscriptions.get(sub.id)
        run = next(run for run in self.runs.claim_due("research", 3, NOW, 900)
                   if run.subscription_id == sub.id)
        item = NewsResult("新模型", "https://official.example/" + chat, "官方",
                          NOW - timedelta(hours=25), "发布了新模型。支持多种任务。", "release:" + chat)
        self.runs.complete(run.id, "research", [item], now=NOW, search_window_days=7)
        return sub, run

    def test_text_ack_and_card_send_without_changing_unrelated_events(self):
        self.outbox.enqueue("chat", "text", {"text": "已收到，将在 5 分钟内处理。"}, "event:1:ack")
        self.outbox.enqueue("chat", "card", {"header": {"title": {"tag": "plain_text", "content": "帮助"}}}, "event:1:result")
        worker, transport = self.worker()
        self.assertEqual(worker.run_once(NOW), 2)
        self.assertEqual([message["msg_type"] for message in transport.messages], ["text", "interactive"])
        self.assertEqual(json.loads(transport.messages[0]["content"])["text"], "已收到，将在 5 分钟内处理。")
        self.assertEqual([message["uuid"] for message in transport.messages], ["event:1:ack", "event:1:result"])
        self.assertEqual([row["status"] for row in self.rows("outbox")], ["sent", "sent"])
        self.assertEqual(self.rows("inbound_events"), [])

    def test_confirmed_delivery_atomically_completes_history_and_schedule(self):
        sub, run = self.prepare()
        with self.database.connect() as connection:
            connection.execute("UPDATE subscriptions SET consecutive_failures = 4, alerted = 1 WHERE id = ?", (sub.id,))
        before = self.rows("deliveries")[0]
        worker, transport = self.worker()
        self.assertEqual(worker.run_once(NOW), 1)
        delivery = self.rows("deliveries")[0]
        self.assertEqual((delivery["status"], delivery["feishu_message_id"], delivery["attempts"]), ("sent", "om_confirmed", 1))
        self.assertEqual((delivery["topic_fingerprint"], delivery["event_key"]),
                         (before["topic_fingerprint"], "release:chat"))
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "completed")
        current = self.subscriptions.get(sub.id)
        self.assertEqual((current.last_success_at, current.consecutive_failures, current.alerted), (NOW, 0, False))
        self.assertEqual(current.next_run_at, datetime(2026, 10, 2, 2, 5, tzinfo=timezone.utc))
        self.assertEqual(current.version, sub.version + 1)
        self.assertEqual(self.runs.history(sub.id)[0]["event_key"], "release:chat")
        self.assertEqual(self.runs.complete(run.id, "research", [], now=NOW).status, "completed")
        self.assertEqual(worker.run_once(NOW), 0)
        self.assertEqual(len(transport.messages), 1)

    def test_cancel_before_claim_stops_digest_without_affecting_ack_or_other_subscription(self):
        sub, run = self.prepare()
        original = self.rows("deliveries")[0]
        cancelled = self.subscriptions.cancel(sub.id, sub.version, now=NOW)
        worker, transport = self.worker()
        self.assertEqual(worker.run_once(NOW), 0)
        self.assertEqual(transport.messages, [])
        self.assertEqual(self.rows("outbox")[0]["status"], "failed")
        self.assertEqual(self.rows("deliveries")[0]["status"], "failed")
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "failed")
        self.assertEqual(self.outbox.claim("later", 3, NOW + timedelta(days=1), 60), [])
        self.assertEqual(self.runs.history(sub.id), [])
        self.assertEqual(self.subscriptions.get(sub.id), cancelled)
        self.assertEqual((self.rows("deliveries")[0]["topic_fingerprint"],
                          self.rows("deliveries")[0]["event_key"]),
                         (original["topic_fingerprint"], original["event_key"]))
        self.outbox.enqueue("chat", "text", {"text": "ack"}, "event:cancel:ack")
        self.prepare("other")
        self.assertEqual(worker.run_once(NOW), 2)
        self.assertEqual({message["uuid"] for message in transport.messages},
                         {"event:cancel:ack", self.rows("outbox")[-1]["idempotency_key"]})

    def test_cancel_preserves_inflight_confirmation_and_sent_factual_history(self):
        sub, _ = self.prepare()
        item = self.outbox.claim("sender", 1, NOW, 60)[0]
        cancelled = self.subscriptions.cancel(sub.id, sub.version, now=NOW)
        self.assertEqual(self.rows("outbox")[0]["status"], "leased")
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "awaiting_delivery")
        self.outbox.sent(item.id, "sender", "already-in-flight", NOW + timedelta(seconds=1))
        self.assertEqual(self.subscriptions.get(sub.id), cancelled)
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "completed")
        self.assertEqual(self.runs.history(sub.id)[0]["event_key"], "release:chat")
        other, _ = self.prepare("sent")
        worker, _ = self.worker()
        worker.run_once(NOW)
        sent_history = self.runs.history(other.id)
        self.subscriptions.cancel(other.id, self.subscriptions.get(other.id).version, now=NOW)
        self.assertEqual(self.runs.history(other.id), sent_history)
        self.assertEqual(self.rows("outbox")[-1]["status"], "sent")

    def test_cancel_expires_unsent_leases_and_prevents_defensive_pending_claim(self):
        sub, _ = self.prepare()
        self.outbox.claim("old-sender", 1, NOW - timedelta(minutes=2), 60)
        self.subscriptions.cancel(sub.id, sub.version, now=NOW)
        self.assertEqual(self.rows("outbox")[0]["status"], "failed")
        # Defend against a restored old pending row, or retry after an in-flight
        # attempt reports an unknown outcome following cancellation.
        with self.database.connect() as connection:
            connection.execute("UPDATE outbox SET status = 'pending', next_attempt_at = NULL")
        worker, transport = self.worker()
        self.assertEqual(worker.run_once(NOW), 0)
        self.assertEqual(transport.messages, [])

    def test_cancelled_inflight_lease_expiry_closes_unknown_work_without_a_send(self):
        sub, _ = self.prepare()
        self.outbox.claim("inflight", 1, NOW, 60)
        cancelled = self.subscriptions.cancel(sub.id, sub.version, now=NOW)
        worker, transport = self.worker()
        self.assertEqual(worker.run_once(NOW + timedelta(seconds=60)), 0)
        self.assertEqual(transport.messages, [])
        self.assertEqual(self.rows("outbox")[0]["status"], "failed")
        self.assertEqual(self.rows("deliveries")[0]["status"], "failed")
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "failed")
        self.assertEqual(self.subscriptions.get(sub.id), cancelled)

    def test_cancel_during_unknown_inflight_attempt_closes_retry_without_new_send(self):
        sub, _ = self.prepare()
        subscriptions = self.subscriptions

        class CancelDuringSend(Transport):
            def request(self, method, url, headers=None, body=None, timeout=15):
                if "/im/v1/messages" in url:
                    subscriptions.cancel(sub.id, sub.version, now=NOW)
                return super().request(method, url, headers, body, timeout)

        worker, transport = self.worker(CancelDuringSend([OSError(SECRET)]))
        worker.run_once(NOW)
        self.assertEqual(self.rows("outbox")[0]["status"], "failed")
        self.assertEqual(self.rows("deliveries")[0]["status"], "failed")
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "failed")
        self.assertEqual(self.runs.history(sub.id), [])
        self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, 0)
        self.assertEqual(worker.run_once(NOW + timedelta(minutes=10)), 0)
        self.assertEqual(len(transport.messages), 1)

    def test_malformed_200_retries_same_digest_uuid_without_new_run(self):
        sub, run = self.prepare()
        worker, transport = self.worker(Transport([HttpResponse(200, {}, b'{"code":0,"data":')]))
        worker.run_once(NOW)
        self.assertEqual((self.rows("outbox")[0]["status"], self.rows("outbox")[0]["attempts"]),
                         ("pending", 1))
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "awaiting_delivery")
        self.assertEqual(self.runs.claim_due("next-tick", 3, NOW, 900), [])
        self.assertEqual(self.runs.history(sub.id), [])
        worker.run_once(NOW + timedelta(seconds=10))
        self.assertEqual(transport.messages[0], transport.messages[1])
        self.assertEqual(self.rows("outbox")[0]["status"], "sent")
        self.assertEqual(len(self.rows("subscription_runs")), 1)
        self.assertEqual(self.rows("subscription_runs")[0]["id"], run.id)
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "completed")

    def test_database_finalization_failure_rolls_back_all_success_markers(self):
        sub, _ = self.prepare()
        worker, _ = self.worker()
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER reject_sent BEFORE UPDATE OF status ON deliveries "
                               "WHEN NEW.status = 'sent' BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        with self.assertRaises(sqlite3.Error):
            worker.run_once(NOW)
        self.assertEqual(self.rows("outbox")[0]["status"], "leased")
        self.assertEqual(self.rows("deliveries")[0]["status"], "pending")
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "awaiting_delivery")
        self.assertEqual(self.subscriptions.get(sub.id), sub)

    def test_daily_schedule_after_downtime_has_only_one_catchup(self):
        sub, _ = self.prepare(schedule=Schedule("daily", daily_at="09:00"))
        worker, _ = self.worker()
        worker.run_once(NOW)
        self.assertEqual(self.subscriptions.get(sub.id).next_run_at, datetime(2026, 10, 3, 1, tzinfo=timezone.utc))
        self.assertEqual(self.runs.claim_due("next", 3, NOW, 900), [])

    def test_manual_success_preserves_pause_and_regular_schedule(self):
        sub, _ = self.prepare(manual=True)
        worker, _ = self.worker()
        worker.run_once(NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual((current.state, current.next_run_at), ("paused", sub.next_run_at))
        self.assertEqual(current.last_success_at, NOW)

    def test_sent_old_run_preserves_every_field_of_edited_subscription(self):
        sub, _ = self.prepare()
        edited = self.subscriptions.update(sub.id, sub.version, keywords=["energy"],
                                           schedule=Schedule("daily", daily_at="20:00"), now=NOW)
        worker, _ = self.worker()
        worker.run_once(NOW)
        self.assertEqual(self.subscriptions.get(sub.id), edited)
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "completed")
        self.assertEqual(self.runs.history(sub.id)[0]["event_key"], "release:chat")

    def test_401_refreshes_once_with_same_message_uuid_before_confirmation(self):
        self.prepare()
        worker, transport = self.worker(Transport([failure(401, 99991663)]))
        worker.run_once(NOW)
        self.assertEqual(transport.tokens, 2)
        self.assertEqual(len(transport.messages), 2)
        self.assertEqual(transport.messages[0], transport.messages[1])
        self.assertEqual(self.rows("outbox")[0]["status"], "sent")

    def test_rate_limit_honors_server_delay_even_above_backoff_cap(self):
        sub, _ = self.prepare()
        worker, transport = self.worker(Transport([failure(429, 99991400, {"X-Ogw-Ratelimit-Reset": "600"})]))
        worker.run_once(NOW)
        pending = self.rows("outbox")[0]
        self.assertEqual((pending["status"], pending["attempts"]), ("pending", 1))
        self.assertEqual(_datetime(pending["next_attempt_at"]), NOW + timedelta(seconds=600))
        self.assertEqual(self.rows("deliveries")[0]["attempts"], 1)
        self.assertEqual(self.runs.history(sub.id), [])
        self.assertEqual(self.subscriptions.get(sub.id), sub)
        self.assertEqual(worker.run_once(NOW + timedelta(seconds=599)), 0)
        worker.run_once(NOW + timedelta(seconds=600))
        self.assertEqual(transport.messages[0], transport.messages[1])
        self.assertEqual(self.rows("deliveries")[0]["attempts"], 2)

    def test_long_send_advances_schedule_from_confirmation_and_next_claim_gets_a_fresh_lease(self):
        from hotnews.runtime import OutboxWorker
        first, _ = self.prepare("first")
        self.prepare("second")
        elapsed = [0.0]

        class SlowTransport(Transport):
            def request(inner, method, url, headers=None, body=None, timeout=15):
                if "/im/v1/messages" in url:
                    elapsed[0] += 120
                return super().request(method, url, headers, body, timeout)

        transport = SlowTransport()
        client = FeishuClient(FeishuConfig("app", SECRET, "v"), transport)
        worker = OutboxWorker(self.config, client, clock=lambda: elapsed[0])
        worker.run_once(NOW)
        self.assertEqual(self.subscriptions.get(first.id).last_success_at, NOW + timedelta(seconds=120))
        self.assertEqual(self.subscriptions.get(first.id).next_run_at, NOW + timedelta(minutes=7))
        self.assertEqual(_datetime(self.rows("outbox")[1]["sent_at"]), NOW + timedelta(seconds=240))

    def test_missing_message_confirmation_retries_and_never_marks_history_sent(self):
        sub, _ = self.prepare()
        worker, transport = self.worker(Transport([HttpResponse(200, {}, b'{"code":0,"data":{}}')]))
        worker.run_once(NOW)
        self.assertEqual(self.rows("outbox")[0]["status"], "pending")
        self.assertEqual(self.runs.history(sub.id), [])
        worker.run_once(NOW + timedelta(seconds=10))
        self.assertEqual(transport.messages[0], transport.messages[1])
        self.assertEqual(self.rows("outbox")[0]["status"], "sent")

    def test_repeated_401_is_deferred_after_only_one_refresh(self):
        self.prepare()
        worker, transport = self.worker(Transport([failure(401, 99991663), failure(401, 99991663)]))
        worker.run_once(NOW)
        self.assertEqual((transport.tokens, len(transport.messages)), (2, 2))
        self.assertEqual(self.rows("outbox")[0]["status"], "pending")
        self.assertEqual(self.rows("deliveries")[0]["status"], "pending")

    def test_unrepresentable_remote_retry_delay_defers_only_its_message(self):
        self.prepare("bad")
        good, _ = self.prepare("good")
        worker, transport = self.worker(Transport([failure(429, 99991400, {"Retry-After": "1e300"})]))
        worker.run_once(NOW)
        self.assertEqual([row["status"] for row in self.rows("outbox")], ["pending", "sent"])
        self.assertEqual(self.subscriptions.get(good.id).last_success_at, NOW)
        self.assertEqual(len(transport.messages), 2)
        self.assertEqual(_datetime(self.rows("outbox")[0]["next_attempt_at"]), datetime.max.replace(tzinfo=timezone.utc))

    def test_network_backoff_five_attempts_then_releases_dedupe_suppression(self):
        sub, _ = self.prepare()
        worker, transport = self.worker(Transport([OSError(SECRET)] * 6))
        current = NOW
        for delay in (10, 20, 40, 80):
            worker.run_once(current)
            row = self.rows("outbox")[0]
            self.assertEqual(_datetime(row["next_attempt_at"]), current + timedelta(seconds=delay))
            self.assertNotIn(SECRET, row["last_error"])
            current += timedelta(seconds=delay)
        worker.run_once(current)
        self.assertEqual(len(transport.messages), 5)
        self.assertEqual(len({message["uuid"] for message in transport.messages}), 1)
        self.assertEqual(self.rows("outbox")[0]["status"], "failed")
        self.assertEqual(self.rows("deliveries")[0]["status"], "failed")
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "failed")
        self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, 1)
        self.assertEqual(worker.run_once(current + timedelta(days=1)), 0)
        self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, 1)
        self.assertEqual(len(self.runs.claim_due("new", 3, current, 900)), 1)

    def test_incomplete_http_body_is_retryable_and_does_not_block_another_digest(self):
        for error in (IncompleteRead(b"sensitive partial body", 99), HTTPException(SECRET)):
            with self.subTest(error=type(error).__name__):
                first, _ = self.prepare("truncated:" + type(error).__name__.lower())
                second, _ = self.prepare("healthy:" + type(error).__name__.lower())
                worker, transport = self.worker(Transport([error]))
                with self.assertLogs("hotnews.runtime", level="WARNING") as captured:
                    worker.run_once(NOW)
                self.assertIn("deferred for retry", str(captured.output))
                self.assertNotIn(SECRET, str(captured.output))
                self.assertEqual(self.subscriptions.get(second.id).last_success_at, NOW)
                self.assertEqual(self.runs.history(first.id), [])
                row = next(row for row in self.rows("outbox") if row["chat_id"] == first.chat_id)
                self.assertEqual((row["status"], _datetime(row["next_attempt_at"])),
                                 ("pending", NOW + timedelta(seconds=10)))
                worker.run_once(NOW + timedelta(seconds=10))
                self.assertEqual(transport.messages[0], transport.messages[2])
                self.assertEqual(self.runs.history(first.id)[0]["event_key"], "release:" + first.chat_id)

    def test_non_json_rate_limit_preserves_header_priority_and_defers_only_its_digest(self):
        for headers, delay in (({"Retry-After": "600"}, 600),
                               ({"X-Ogw-Ratelimit-Reset": "900", "Retry-After": "600"}, 900),
                               ({"X-Ogw-Ratelimit-Reset": "nan", "Retry-After": "600"}, 600)):
            with self.subTest(headers=headers):
                first, _ = self.prepare("rate:" + str(delay) + str(len(headers)))
                second, _ = self.prepare("healthy:" + str(delay) + str(len(headers)))
                reply = HttpResponse(429, headers, b"<html>sensitive gateway error</html>")
                worker, transport = self.worker(Transport([reply]))
                with self.assertLogs("hotnews.runtime", level="WARNING"):
                    worker.run_once(NOW)
                row = next(row for row in self.rows("outbox") if row["chat_id"] == first.chat_id)
                self.assertEqual(_datetime(row["next_attempt_at"]), NOW + timedelta(seconds=delay))
                self.assertEqual(self.subscriptions.get(second.id).last_success_at, NOW)
                self.assertEqual(self.runs.history(first.id), [])
                worker.run_once(NOW + timedelta(seconds=delay))
                self.assertEqual(transport.messages[0], transport.messages[2])

    def test_manual_request_after_preparation_does_not_invalidate_scheduled_success(self):
        sub, run = self.prepare()
        manual_id = self.subscriptions.request_manual_run(sub.id, sub.version, now=NOW)
        requested = self.subscriptions.get(sub.id)
        self.assertEqual(requested.version, sub.version + 1)
        worker, _ = self.worker()
        worker.run_once(NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual(current.next_run_at, NOW + timedelta(minutes=5))
        self.assertEqual(current.last_success_at, NOW)
        claimed = self.runs.claim_due("next", 3, NOW, 900)
        self.assertEqual([(item.id, item.trigger) for item in claimed], [(manual_id, "manual")])
        self.runs.complete(manual_id, "next", [], now=NOW)
        self.assertEqual(self.runs.claim_due("next", 3, NOW, 900), [])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM subscription_runs WHERE trigger='scheduled'").fetchone()[0], 1)
        self.assertEqual(self.runs.complete(run.id, "research", [], now=NOW).status, "completed")

    def test_real_edit_then_manual_request_still_invalidates_older_scheduled_snapshot(self):
        sub, _ = self.prepare()
        edited = self.subscriptions.update(sub.id, sub.version, topic="edited topic",
                                           schedule=Schedule("daily", daily_at="20:00"), now=NOW)
        self.subscriptions.request_manual_run(sub.id, edited.version, now=NOW)
        requested = self.subscriptions.get(sub.id)
        worker, _ = self.worker()
        worker.run_once(NOW)
        self.assertEqual(self.subscriptions.get(sub.id), requested)
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "completed")

    def test_exponential_backoff_is_capped(self):
        worker, _ = self.worker()
        self.assertEqual([worker.retry_delay(attempt) for attempt in (1, 5, 6, 7, 10000)], [10, 160, 300, 300, 300])

    def test_expired_fifth_claim_does_not_make_sixth_network_send(self):
        self.prepare()
        with self.database.connect() as connection:
            connection.execute("UPDATE outbox SET status='leased', attempts=5, lease_owner='crashed', lease_until=?",
                               ("2026-10-02T01:59:00.000000Z",))
        worker, transport = self.worker()
        worker.run_once(NOW)
        self.assertEqual(transport.messages, [])
        self.assertEqual(self.rows("outbox")[0]["status"], "failed")

    def test_permanent_payload_failure_is_isolated_and_ack_remains_independent(self):
        failed, _ = self.prepare("bad")
        succeeded, _ = self.prepare("good")
        self.outbox.enqueue("bad", "text", {"text": "已收到"}, "independent:ack")
        worker, transport = self.worker(Transport([failure(400, 230001)]))
        self.assertEqual(worker.run_once(NOW), 3)
        self.assertEqual([row["status"] for row in self.rows("outbox")], ["failed", "sent", "sent"])
        self.assertEqual(self.subscriptions.get(failed.id).consecutive_failures, 1)
        self.assertEqual(self.subscriptions.get(succeeded.id).consecutive_failures, 0)
        self.assertEqual([row["status"] for row in self.rows("deliveries")], ["failed", "sent"])
        self.assertEqual(len(transport.messages), 3)

    def test_invalid_local_payload_does_not_call_network_and_stale_failure_does_not_touch_edit(self):
        sub, _ = self.prepare()
        with self.database.connect() as connection:
            connection.execute("UPDATE outbox SET kind='unknown'")
        edited = self.subscriptions.update(sub.id, sub.version, topic="new topic", now=NOW)
        worker, transport = self.worker()
        worker.run_once(NOW)
        self.assertEqual(transport.messages, [])
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "failed")
        self.assertEqual(self.subscriptions.get(sub.id), edited)

    def test_failure_alert_once_after_three_then_success_resets(self):
        sub, _ = self.prepare()
        with self.database.connect() as connection:
            connection.execute("UPDATE subscriptions SET consecutive_failures=2 WHERE id=?", (sub.id,))
        worker, _ = self.worker(Transport([failure(400, 230001)]))
        worker.run_once(NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual((current.consecutive_failures, current.alerted), (3, True))
        self.assertEqual(len([row for row in self.rows("outbox") if row["idempotency_key"].startswith("alert:")]), 1)
        run = self.runs.claim_due("research", 1, NOW, 900)[0]
        self.runs.complete(run.id, "research", [], now=NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual((current.consecutive_failures, current.alerted), (0, False))

    def test_no_results_remains_silent_and_worker_stops_without_claim(self):
        sub = self.subscriptions.create("chat", "member", "AI", ["AI"], ["AI"],
                                        Schedule("interval", interval_minutes=5), now=NOW - timedelta(days=1))
        run = self.runs.claim_due("research", 1, NOW, 900)[0]
        self.runs.complete(run.id, "research", [], now=NOW)
        worker, transport = self.worker()
        self.assertEqual(worker.run_once(NOW), 0)
        self.assertEqual(transport.messages, [])
        self.outbox.enqueue("chat", "text", {"text": "later"}, "later")
        stop = threading.Event()
        stop.set()
        worker.run(stop)
        self.assertEqual(self.rows("outbox")[0]["status"], "pending")
        self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, 0)


class ServiceTests(unittest.TestCase):
    def test_excessive_content_length_returns_413_and_servers_keep_running(self):
        from hotnews.admin import server as admin_server
        from hotnews.admin.app import AdminApplication
        from hotnews.feishu import gateway
        from tests.integration.test_gateway import MemorySocket
        from tests.unit.test_feishu import config as feishu_config
        for module in (admin_server, gateway):
            with self.subTest(service=module.__name__), tempfile.TemporaryDirectory() as directory:
                settings = AppConfig(database_path=str(Path(directory) / "framing.db"))
                database = Database(settings.database_path)
                database.migrate()
                app = (gateway.GatewayApplication(settings, feishu_config(), database) if module is gateway
                       else AdminApplication(settings, SubscriptionRepository(database)))
                route = "/callbacks/feishu" if module is gateway else "/api/session"
                method = "POST" if module is gateway else "GET"
                huge = "9" * 5000
                headers = {"Host": "127.0.0.1:8081", "Content-Type": "application/json", "Content-Length": huge}
                stop = threading.Event()
                seen = []

                class ServerBoundary:
                    def __init__(inner, address, handler):
                        inner.handler = handler
                    def __enter__(inner):
                        return inner
                    def __exit__(inner, *args):
                        pass
                    def handle_request(inner):
                        # Emulate Python 3.11's digit-conversion limit on the
                        # current Python 3.8 host without mocking safe small ints.
                        original_int = int
                        def limited_int(value, *args):
                            if isinstance(value, str) and len(value) > 4300:
                                raise ValueError("Exceeds integer string conversion limit")
                            return original_int(value, *args)
                        request = ("%s %s HTTP/1.1\r\nHost: 127.0.0.1:8081\r\nContent-Type: application/json\r\n"
                                   "Content-Length: %s\r\n\r\n" % (method, route, huge)).encode()
                        socket = MemorySocket(request)
                        with patch.object(module, "int", limited_int, create=True), \
                             patch.object(gateway, "int", limited_int, create=True), \
                             patch("hotnews.http.int", limited_int, create=True), \
                             patch("hotnews.admin.app.int", limited_int, create=True):
                            try:
                                inner.handler(socket, ("127.0.0.1", 1234), inner)
                            except Exception:
                                inner.handle_error(None, ("127.0.0.1", 1234))
                        seen.append(bytes(socket.outgoing))
                        seen.append(stop.is_set())
                        stop.set()

                with patch.object(module, "ThreadingHTTPServer", ServerBoundary):
                    if module is gateway:
                        module.serve_gateway(settings, stop, feishu_config())
                    else:
                        module.serve_admin(settings, stop)
                self.assertIn(b"413 Request Entity Too Large", seen[0])
                self.assertFalse(seen[1])
                self.assertEqual(app.handle(method, route, headers, b"").status, 413)
    def test_http_client_disconnect_does_not_stop_or_dump_request_errors(self):
        from hotnews.http import supervise_request_errors
        stop = threading.Event()
        class ServerBoundary:
            def handle_error(self, request, address):
                raise AssertionError("framework fallback must not dump raw errors")
        server = ServerBoundary()
        failed = supervise_request_errors(server, stop)
        try:
            raise BrokenPipeError(SECRET)
        except BrokenPipeError:
            server.handle_error(None, ("127.0.0.1", 1234))
        self.assertFalse(stop.is_set())
        self.assertFalse(failed.is_set())

    def test_unexpected_http_request_thread_error_is_safe_and_stops_its_server(self):
        from hotnews.admin import server as admin_server
        from hotnews.feishu import gateway
        for module, error_type in ((admin_server, RuntimeError), (gateway, RuntimeError),
                                   (admin_server, OSError), (gateway, OSError)):
            with self.subTest(service=module.__name__, error=error_type), tempfile.TemporaryDirectory() as directory:
                stop = threading.Event()
                observed = {}

                class ServerBoundary:
                    def __init__(inner, address, handler):
                        pass
                    def __enter__(inner):
                        return inner
                    def __exit__(inner, *args):
                        observed["closed"] = True
                    def handle_error(inner, request, address):
                        observed["unsafe_fallback"] = True
                    def handle_request(inner):
                        try:
                            raise error_type(SECRET)
                        except Exception:
                            inner.handle_error(None, ("127.0.0.1", 1234))
                        # Keep the red test bounded even if no stop propagation.
                        observed["propagated_stop"] = stop.is_set()
                        stop.set()

                config = AppConfig(database_path=str(Path(directory) / "http.db"))
                with patch.object(module, "ThreadingHTTPServer", ServerBoundary):
                    try:
                        if module is gateway:
                            module.serve_gateway(config, stop, FeishuConfig("app", SECRET, "v", bot_open_id="ou_bot"))
                        else:
                            module.serve_admin(config, stop)
                    except RuntimeError as error:
                        self.assertNotIn(SECRET, str(error))
                    else:
                        self.fail("request thread failure must surface to the runtime supervisor")
                self.assertTrue(observed["closed"])
                self.assertTrue(observed["propagated_stop"])
                self.assertNotIn("unsafe_fallback", observed)

    def test_service_migrates_before_threads_resolves_identity_once_and_stops_all(self):
        from hotnews.runtime import run_service
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(database_path=str(Path(directory) / "new.db"),
                               admin=ServerConfig("127.0.0.1", 8181))
            transport = Transport()
            client = FeishuClient(FeishuConfig("app", SECRET), transport)
            stop = threading.Event()
            started = []
            ready = threading.Event()
            lock = threading.Lock()

            def boundary(name, received_stop):
                with Database(config.database_path).connect() as connection:
                    self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 2)
                self.assertIs(received_stop, stop)
                with lock:
                    started.append(name)
                    if len(started) == 2:
                        ready.set()
                        stop.set()
                stop.wait(2)

            class Connection:
                def __init__(inner, feishu_config, intake, status):
                    self.assertEqual(feishu_config.bot_open_id, "ou_bot")
                    self.assertEqual(intake.bot_open_id, "ou_bot")
                    self.assertEqual(status.snapshot().state, "starting")
                def run(inner, received_stop):
                    boundary("connection", received_stop)

            with patch("hotnews.runtime.load_feishu_config", return_value=client.config), \
                 patch("hotnews.runtime.FeishuClient", return_value=client), \
                 patch("hotnews.runtime.FeishuLongConnection", Connection), \
                 patch("hotnews.runtime.serve_admin", side_effect=lambda c, s, provider: (
                     self.assertEqual(provider().state, "starting"), boundary("admin", s))[-1]):
                run_service(config, stop)
            self.assertTrue(ready.is_set())
            self.assertEqual(sorted(started), ["admin", "connection"])
            self.assertEqual(transport.bot_requests, 1)
            self.assertTrue(stop.is_set())

    def test_thread_exception_stops_peers_and_raises_safe_observable_error(self):
        from hotnews.runtime import RuntimeServiceError, run_service
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(database_path=str(Path(directory) / "new.db"))
            stop = threading.Event()
            finished = threading.Event()

            def peer(c, s, provider):
                s.wait(2)
                finished.set()

            class BrokenConnection:
                def __init__(inner, *args):
                    pass
                def run(inner, stop_event):
                    raise RuntimeError(SECRET)

            with patch("hotnews.runtime.load_feishu_config", return_value=FeishuConfig("app", SECRET, bot_open_id="ou_bot")), \
                 patch("hotnews.runtime.FeishuLongConnection", BrokenConnection), \
                 patch("hotnews.runtime.serve_admin", side_effect=peer):
                with self.assertLogs("hotnews.runtime", level="ERROR") as logs:
                    with self.assertRaises(RuntimeServiceError) as caught:
                        run_service(config, stop)
            self.assertTrue(stop.is_set())
            self.assertTrue(finished.is_set())
            self.assertNotIn(SECRET, str(caught.exception) + str(logs.output))
            self.assertIn("connection", str(caught.exception))

    def test_nonlocal_admin_is_rejected_before_startup(self):
        from hotnews.runtime import run_service
        config = replace(AppConfig(), admin=ServerConfig("0.0.0.0", 8081))
        with self.assertRaises(ValidationError):
            run_service(config, threading.Event())
