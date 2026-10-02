"""Behavioral contracts for durable queues and global coordination leases."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from hotnews.domain import InboundEvent, LeaseConflict, NormalizedEvent, OutboxItem
from hotnews.storage.database import Database
from hotnews.storage.events import EventRepository, LeaseRepository
from hotnews.storage.outbox import OutboxRepository


NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


class QueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(str(Path(directory.name) / "queue.sqlite"))
        self.database.migrate()
        self.events = EventRepository(self.database)
        self.outbox = OutboxRepository(self.database)
        self.leases = LeaseRepository(self.database)

    def event(self, number=1, **changes):
        value = {
            "event_id": "event-%d" % number,
            "message_id": "message-%d" % number,
            "chat_id": "chat-a", "sender_id": "member",
            "raw_text": "@bot 新闻", "text": "新闻",
            "received_at": NOW + timedelta(seconds=number),
        }
        value.update(changes)
        return value

    def row(self, table, item_id):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM %s WHERE id = ?" % table,
                                      (item_id,)).fetchone()

    def test_duplicate_event_and_message_ids_are_ignored(self):
        self.assertTrue(self.events.insert(self.event()))
        self.assertFalse(self.events.insert(self.event()))
        self.assertFalse(self.events.insert(self.event(2, message_id="message-1")))
        self.assertFalse(self.events.insert(self.event(2, event_id="event-1")))
        claimed = self.events.claim_pending("worker", 10, NOW, 60)
        self.assertEqual([event.event_id for event in claimed], ["event-1"])
        self.assertEqual(self.row("inbound_events", claimed[0].id)["raw_text"], "@bot 新闻")

    def test_normalized_dataclass_event_is_supported(self):
        event = NormalizedEvent("event-a", "message-a", "chat-a", "member", "新闻", NOW)
        self.assertTrue(self.events.insert(event))
        self.assertEqual(self.events.claim_pending("worker", 1, NOW, 60)[0].text, "新闻")

    def test_event_mentions_are_archived_as_stable_json_without_changing_business_records(self):
        mentions = [{"name": '机器人 "中文"', "id": {"open_id": "ou_bot"}, "key": "@_user_1"}]
        self.events.insert(self.event(mentions=mentions))
        claimed = self.events.claim_pending("worker", 1, NOW, 60)[0]
        serialized = self.row("inbound_events", claimed.id)["mentions_json"]
        self.assertEqual(json.loads(serialized), mentions)
        reordered = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": '机器人 "中文"'}]
        self.events.insert(self.event(2, mentions=reordered))
        other = self.events.claim_pending("worker", 1, NOW, 60)[0]
        self.assertEqual(self.row("inbound_events", other.id)["mentions_json"], serialized)
        self.assertEqual(claimed.text, "新闻")

    def test_event_without_mentions_has_an_empty_archive(self):
        self.events.insert(self.event())
        claimed = self.events.claim_pending("worker", 1, NOW, 60)[0]
        self.assertEqual(self.row("inbound_events", claimed.id)["mentions_json"], "[]")

    def test_invalid_mentions_archive_is_rejected_without_inserting_an_event(self):
        for mentions in (None, {}, [{"id": object()}], [{"name": float("nan")}], [{1: "bad key"}]):
            with self.subTest(mentions=mentions), self.assertRaises((TypeError, ValueError)):
                self.events.insert(self.event(mentions=mentions))
        self.assertEqual(self.events.claim_pending("worker", 10, NOW, 60), [])

    def test_event_claim_is_fifo_and_returns_current_lease(self):
        for number in (3, 1, 2):
            self.events.insert(self.event(number))
        claimed = self.events.claim_pending("worker", 2, NOW, 60)
        self.assertEqual([event.event_id for event in claimed], ["event-1", "event-2"])
        self.assertIsInstance(claimed[0], InboundEvent)
        self.assertEqual(claimed[0].received_at, NOW + timedelta(seconds=1))
        self.assertEqual(claimed[0].status, "leased")
        self.assertEqual(claimed[0].lease_owner, "worker")
        self.assertEqual(claimed[0].lease_until, NOW + timedelta(seconds=60))
        self.assertEqual(claimed[0].attempts, 1)

    def test_event_reclaim_excludes_unexpired_lease(self):
        self.events.insert(self.event())
        first = self.events.claim_pending("first", 1, NOW, 60)[0]
        self.assertEqual(self.events.claim_pending("second", 1, NOW + timedelta(seconds=59), 60), [])
        reclaimed = self.events.claim_pending("second", 1, NOW + timedelta(seconds=60), 60)[0]
        self.assertEqual(reclaimed.id, first.id)
        self.assertEqual(reclaimed.attempts, 2)
        with self.assertRaises(LeaseConflict):
            self.events.complete("event-1", "first", "stale")

    def test_complete_uses_external_event_id_and_checks_owner(self):
        self.events.insert(self.event())
        claimed = self.events.claim_pending("worker", 1, NOW, 60)[0]
        with self.assertRaises(LeaseConflict):
            self.events.complete("event-1", "stranger", "wrong")
        self.events.complete("event-1", "worker", "handled 新闻")
        row = self.row("inbound_events", claimed.id)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["result_summary"], "handled 新闻")
        self.assertIsNone(row["lease_owner"])
        self.assertEqual(self.events.claim_pending("other", 10, NOW + timedelta(days=1), 60), [])

    def test_event_failure_checks_owner_and_is_terminal(self):
        self.events.insert(self.event())
        claimed = self.events.claim_pending("worker", 1, NOW, 60)[0]
        with self.assertRaises(LeaseConflict):
            self.events.fail("event-1", "stranger", "wrong")
        self.events.fail("event-1", "worker", "invalid intent")
        row = self.row("inbound_events", claimed.id)
        self.assertEqual((row["status"], row["last_error"]), ("failed", "invalid intent"))
        self.assertIsNone(row["lease_until"])
        self.assertEqual(self.events.claim_pending("other", 10, NOW + timedelta(days=1), 60), [])

    def test_unclaimed_and_missing_events_cannot_be_completed(self):
        self.events.insert(self.event())
        for event_id in ("event-1", "missing"):
            with self.assertRaises(LeaseConflict):
                self.events.complete(event_id, "worker", "result")

    def test_global_lease_acquire_renew_and_release_are_owner_checked(self):
        self.assertTrue(self.leases.acquire("agent", "first", NOW, 60))
        self.assertTrue(self.leases.acquire("agent", "first", NOW, 60))
        self.assertFalse(self.leases.acquire("agent", "other", NOW, 60))
        self.assertFalse(self.leases.renew("agent", "other", NOW, 120))
        self.assertFalse(self.leases.release("agent", "other"))
        self.assertTrue(self.leases.renew("agent", "first", NOW + timedelta(seconds=30), 60))
        self.assertFalse(self.leases.acquire("agent", "other", NOW + timedelta(seconds=60), 60))
        self.assertTrue(self.leases.release("agent", "first"))
        self.assertFalse(self.leases.release("agent", "first"))
        self.assertTrue(self.leases.acquire("agent", "other", NOW, 60))

    def test_expired_global_lease_allows_takeover_but_not_stale_renewal(self):
        self.assertFalse(self.leases.renew("missing", "first", NOW, 60))
        self.leases.acquire("agent", "first", NOW, 60)
        later = NOW + timedelta(seconds=60)
        self.assertFalse(self.leases.renew("agent", "first", later, 60))
        self.assertTrue(self.leases.acquire("agent", "second", later, 60))
        self.assertFalse(self.leases.renew("agent", "first", later, 60))
        self.assertFalse(self.leases.release("agent", "first"))

    def test_outbox_idempotency_preserves_original_content(self):
        original = {"text": "中文\n'quoted'", "nested": [True, None, {"count": 2}]}
        item_id = self.outbox.enqueue("chat-a", "text", original, "key-a")
        duplicate = self.outbox.enqueue("chat-b", "card", {"text": "changed"}, "key-a")
        self.assertEqual(item_id, duplicate)
        claimed = self.outbox.claim("worker", 10, datetime.now(timezone.utc), 60)
        self.assertEqual(len(claimed), 1)
        self.assertIsInstance(claimed[0], OutboxItem)
        self.assertEqual(claimed[0].content, original)
        self.assertEqual((claimed[0].chat_id, claimed[0].kind), ("chat-a", "text"))
        self.assertEqual(claimed[0].attempts, 1)

    def test_outbox_claim_is_fifo(self):
        first = self.outbox.enqueue("chat-a", "text", {"text": "first"}, "key-first")
        second = self.outbox.enqueue("chat-a", "text", {"text": "second"}, "key-second")
        with self.database.connect() as connection:
            connection.execute("UPDATE outbox SET created_at = ? WHERE id = ?",
                               ("2026-10-02T00:00:02.000000Z", first))
            connection.execute("UPDATE outbox SET created_at = ? WHERE id = ?",
                               ("2026-10-02T00:00:01.000000Z", second))
        self.assertEqual([item.id for item in self.outbox.claim("worker", 2, NOW, 60)],
                         [second, first])

    def test_outbox_retry_delays_reclaim_and_increments_attempts_on_claim(self):
        item_id = self.outbox.enqueue("chat-a", "text", {"text": "hello"}, "key-a")
        first = self.outbox.claim("worker", 1, NOW, 60)[0]
        with self.assertRaises(LeaseConflict):
            self.outbox.retry(item_id, "stranger", "wrong", NOW)
        due = NOW + timedelta(minutes=5)
        self.outbox.retry(item_id, "worker", "rate limited", due)
        row = self.row("outbox", item_id)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "rate limited")
        self.assertIsNone(row["lease_owner"])
        self.assertEqual(self.outbox.claim("other", 1, due - timedelta(microseconds=1), 60), [])
        retried = self.outbox.claim("other", 1, due, 60)[0]
        self.assertEqual(retried.id, first.id)
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(retried.idempotency_key, "key-a")

    def test_outbox_expired_lease_is_reclaimed(self):
        self.outbox.enqueue("chat-a", "text", {}, "key-a")
        first = self.outbox.claim("first", 1, NOW, 60)[0]
        self.assertEqual(self.outbox.claim("second", 1, NOW + timedelta(seconds=59), 60), [])
        reclaimed = self.outbox.claim("second", 1, NOW + timedelta(seconds=60), 60)[0]
        self.assertEqual((reclaimed.id, reclaimed.attempts), (first.id, 2))
        with self.assertRaises(LeaseConflict):
            self.outbox.sent(first.id, "first", "message-a", NOW)

    def test_sent_outbox_is_terminal_and_records_feishu_confirmation(self):
        item_id = self.outbox.enqueue("chat-a", "text", {}, "key-a")
        self.outbox.claim("worker", 1, NOW, 60)
        with self.assertRaises(LeaseConflict):
            self.outbox.sent(item_id, "stranger", "wrong", NOW)
        self.outbox.sent(item_id, "worker", "feishu-message-a", NOW)
        row = self.row("outbox", item_id)
        self.assertEqual((row["status"], row["feishu_message_id"]), ("sent", "feishu-message-a"))
        self.assertEqual(row["sent_at"], "2026-10-02T00:00:00.000000Z")
        self.assertIsNone(row["lease_until"])
        self.assertEqual(self.outbox.claim("other", 10, NOW + timedelta(days=1), 60), [])
        with self.assertRaises(LeaseConflict):
            self.outbox.retry(item_id, "worker", "again", NOW)

    def test_unclaimed_or_missing_outbox_cannot_be_updated(self):
        item_id = self.outbox.enqueue("chat-a", "text", {}, "key-a")
        for candidate in (item_id, "missing"):
            with self.assertRaises(LeaseConflict):
                self.outbox.sent(candidate, "worker", "message-a", NOW)
            with self.assertRaises(LeaseConflict):
                self.outbox.retry(candidate, "worker", "error", NOW)

    def test_outbox_rejects_non_round_tripping_json_before_inserting(self):
        for content in ({"value": float("nan")}, {1: "bad key"}, {"value": (1, 2)}, {"value": object()}):
            with self.subTest(content=content), self.assertRaises((ValueError, TypeError)):
                self.outbox.enqueue("chat-a", "text", content, "bad")
        self.assertEqual(self.outbox.claim("worker", 10, NOW, 60), [])

    def test_outbox_round_trips_escaped_unicode_without_sqlite_encoding_errors(self):
        content = {"text": "中文\ud800\u0000"}
        self.outbox.enqueue("chat-a", "text", content, "unicode")
        self.assertEqual(self.outbox.claim("worker", 1, NOW, 60)[0].content, content)

    def test_times_are_normalized_to_utc_without_losing_microseconds(self):
        local = datetime(2026, 10, 2, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
        self.events.insert(self.event(received_at=local))
        claimed = self.events.claim_pending("worker", 1, local, 60)[0]
        self.assertEqual(claimed.received_at, datetime(2026, 10, 2, 0, 0, 0, 123456, tzinfo=timezone.utc))
        row = self.row("inbound_events", claimed.id)
        self.assertEqual(row["received_at"], "2026-10-02T00:00:00.123456Z")
        self.assertEqual(row["lease_until"], "2026-10-02T00:01:00.123456Z")

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            self.events.insert(self.event(received_at=datetime(2026, 10, 2)))

    def test_zero_limit_does_not_claim_and_invalid_claim_parameters_are_rejected(self):
        self.events.insert(self.event())
        self.outbox.enqueue("chat-a", "text", {}, "key-a")
        for claim in (self.events.claim_pending, self.outbox.claim):
            self.assertEqual(claim("worker", 0, NOW, 60), [])
            for owner, limit, seconds in (("", 1, 60), ("worker", -1, 60), ("worker", 1, 0)):
                with self.assertRaises(ValueError):
                    claim(owner, limit, NOW, seconds)

    def test_claims_do_not_duplicate_work_under_concurrent_connections(self):
        for number in range(20):
            self.events.insert(self.event(number))
            self.outbox.enqueue("chat-a", "text", {"number": number}, "key-%d" % number)
        for repository, method in ((EventRepository, "claim_pending"), (OutboxRepository, "claim")):
            barrier = threading.Barrier(2)

            def claim(owner):
                independent = repository(Database(self.database.path))
                barrier.wait()
                return getattr(independent, method)(owner, 20, NOW, 60)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(claim, owner) for owner in ("first", "second")]
                batches = [future.result(timeout=10) for future in futures]
            ids = [item.id for batch in batches for item in batch]
            self.assertEqual(len(ids), 20)
            self.assertEqual(len(set(ids)), 20)

    def test_global_acquisition_has_one_winner_under_concurrent_connections(self):
        barrier = threading.Barrier(2)

        def acquire(owner):
            independent = LeaseRepository(Database(self.database.path))
            barrier.wait()
            return independent.acquire("agent", owner, NOW, 60)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(acquire, owner) for owner in ("first", "second")]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(outcomes), [False, True])


if __name__ == "__main__":
    unittest.main()
