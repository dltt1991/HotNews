"""Structured commands stay strict, group-scoped, atomic and idempotent."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hotnews.domain import Intent, LeaseConflict, Schedule, ValidationError, VersionConflict
from hotnews.commands.schema import parse_intent
from hotnews.commands.service import CommandService
from hotnews.storage.database import Database
from hotnews.storage.events import EventRepository
from hotnews.storage.outbox import OutboxRepository
from hotnews.storage.subscriptions import SubscriptionRepository


NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
CREATE = {"action": "create_subscription", "topic": "AI Agent", "keywords": ["AI", "大模型"],
          "search_terms": ["AI agents", "large language models"]}


class IntentSchemaTests(unittest.TestCase):
    def test_all_six_actions_and_default_schedule(self):
        created = parse_intent(CREATE)
        self.assertEqual(created.keywords, ("AI", "大模型"))
        self.assertEqual(created.search_terms, ("AI agents", "large language models"))
        self.assertEqual(created.schedule, Schedule("daily", daily_at="09:00"))
        for action in ("list_subscriptions", "show_help", "clarification_required"):
            self.assertEqual(parse_intent({"action": action}).action, action)
        for action in ("cancel_subscription", "run_subscription_now"):
            self.assertEqual(parse_intent({"action": action, "subscription_number": 2}).subscription_number, 2)

    def test_daily_and_interval_schedules(self):
        for value, want in (({"kind": "daily", "daily_at": "21:30"}, Schedule("daily", daily_at="21:30")),
                            ({"kind": "interval", "interval_minutes": 120}, Schedule("interval", interval_minutes=120))):
            self.assertEqual(parse_intent(dict(CREATE, schedule=value)).schedule, want)

    def test_rejects_unknown_or_action_inappropriate_fields(self):
        values = [dict(CREATE, sql="DROP TABLE subscriptions"),
                  dict(CREATE, subscription_number=1),
                  {"action": "show_help", "keywords": ["AI"]},
                  {"action": "list_subscriptions", "chat_id": "other-chat"},
                  {"action": "cancel_subscription", "subscription_number": 1, "topic": "AI"},
                  dict(CREATE, schedule={"kind": "daily", "daily_at": "09:00", "timezone": "UTC"})]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_intent(value)

    def test_rejects_missing_or_wrong_json_types_without_coercion(self):
        values = [None, [], "create_subscription", {}, {"action": "unknown"}, {"action": True},
                  {"action": "create_subscription", "keywords": ["AI"]},
                  dict(CREATE, keywords=[]), dict(CREATE, keywords="AI"), dict(CREATE, keywords=("AI",)),
                  dict(CREATE, keywords=[True]), dict(CREATE, keywords=[" "]), dict(CREATE, topic=False),
                  dict(CREATE, search_terms="AI"), dict(CREATE, search_terms=[None]),
                  dict(CREATE, schedule=None)]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_intent(value)

    def test_rejects_invalid_numbers_and_missing_number(self):
        for action in ("cancel_subscription", "run_subscription_now"):
            for number in (None, True, False, 0, -1, 1.0, "1"):
                value = {"action": action}
                if number is not None:
                    value["subscription_number"] = number
                with self.subTest(action=action, number=number), self.assertRaises(ValidationError):
                    parse_intent(value)

    def test_rejects_invalid_time_interval_and_manual_creation(self):
        schedules = [{"kind": "daily"}, {"kind": "daily", "daily_at": "9:00"},
                     {"kind": "daily", "daily_at": "24:00"}, {"kind": "daily", "daily_at": 900},
                     {"kind": "interval", "interval_minutes": True},
                     {"kind": "interval", "interval_minutes": 4},
                     {"kind": "interval", "interval_minutes": 5.0},
                     {"kind": "interval", "interval_minutes": "5"}, {"kind": "manual"}]
        for schedule in schedules:
            with self.subTest(schedule=schedule), self.assertRaises(ValidationError):
                parse_intent(dict(CREATE, schedule=schedule))

    def test_topic_and_keyword_limits(self):
        for value in (dict(CREATE, topic="x" * 201), dict(CREATE, keywords=["x"] * 21),
                      dict(CREATE, keywords=["x" * 81])):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                parse_intent(value)
        self.assertEqual(parse_intent(dict(CREATE, keywords=["x" * 80] * 20)).keywords[0], "x" * 80)


class CommandServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(str(Path(directory.name) / "commands.db"))
        self.database.migrate()
        self.events = EventRepository(self.database)
        self.subscriptions = SubscriptionRepository(self.database)
        self.service = CommandService(self.database, clock=lambda: NOW)
        self.counter = 0

    def event(self, chat_id="chat-a", sender_id="member-a", claim=True):
        self.counter += 1
        event_id = "event-%d" % self.counter
        self.events.insert({"event_id": event_id, "message_id": "message-%d" % self.counter,
                            "chat_id": chat_id, "sender_id": sender_id, "text": "@bot 订阅 AI",
                            "received_at": NOW})
        if claim:
            self.events.claim_pending("worker", 20, NOW, 900)
        return event_id

    def rows(self, table):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM %s ORDER BY rowid" % table).fetchall()

    def apply(self, value=CREATE, **event_options):
        return self.service.apply(self.event(**event_options), "worker", parse_intent(value))

    def business_result(self, event_id, intent):
        try:
            return self.service.apply(event_id, "worker", intent)
        except ValidationError:
            self.fail("A business-invalid target must return help instead of a validation error")

    def test_create_preserves_original_and_expanded_keywords_with_local_next_time(self):
        result = self.apply()
        saved = result.subscription
        self.assertEqual((saved.chat_id, saved.creator_id, saved.display_number), ("chat-a", "member-a", 1))
        self.assertEqual(saved.keywords, ("AI", "大模型"))
        self.assertEqual(saved.search_terms, ("AI agents", "large language models"))
        self.assertEqual(saved.next_run_at, datetime(2026, 10, 2, 1, tzinfo=timezone.utc))
        self.assertIn("09:00", result.message)
        self.assertIn("2026-10-02", result.message)
        self.assertEqual(self.rows("inbound_events")[0]["status"], "completed")
        queued = self.rows("outbox")
        self.assertEqual(len(queued), 1)
        self.assertEqual((queued[0]["chat_id"], queued[0]["kind"]), ("chat-a", "card"))
        self.assertIn(result.message, json.dumps(json.loads(queued[0]["content_json"]), ensure_ascii=False))

    def test_multiple_subscriptions_and_group_scoped_shared_listing(self):
        first = self.apply().subscription
        second = self.apply(dict(CREATE, topic="新能源汽车", keywords=["新能源汽车"])).subscription
        self.apply(chat_id="chat-b")
        listed = self.apply({"action": "list_subscriptions"}, sender_id="another-member")
        self.assertEqual([value.id for value in listed.subscriptions], [first.id, second.id])
        self.assertIn("#1", listed.message)
        self.assertIn("#2", listed.message)

    def test_other_member_can_cancel_and_list_excludes_cancelled(self):
        saved = self.apply().subscription
        cancelled = self.apply({"action": "cancel_subscription", "subscription_number": 1}, sender_id="other")
        self.assertEqual(cancelled.subscription.state, "cancelled")
        self.assertEqual(self.subscriptions.get(saved.id).state, "cancelled")
        self.assertEqual(self.apply({"action": "list_subscriptions"}).subscriptions, ())

    def test_cross_group_number_completes_with_one_help_reply_without_target_mutation(self):
        saved = self.apply(chat_id="chat-b").subscription
        for action in ("cancel_subscription", "run_subscription_now"):
            event_id = self.event(chat_id="chat-a")
            intent = parse_intent({"action": action, "subscription_number": 1})
            count = len(self.rows("outbox"))
            with self.subTest(action=action):
                result = self.business_result(event_id, intent)
                self.assertIsNone(result.subscription)
                self.assertIn("查看订阅", result.message)
                self.assertIn("取消订阅 2", result.message)
                self.assertEqual(self.events.get(event_id).status, "completed")
                self.assertEqual(self.subscriptions.get(saved.id), saved)
                replies = self.rows("outbox")
                self.assertEqual(len(replies), count + 1)
                self.assertEqual((replies[-1]["chat_id"], replies[-1]["idempotency_key"]),
                                 ("chat-a", "event:%s:result" % event_id))
                self.assertEqual(json.loads(replies[-1]["content_json"])["elements"][0]["text"]["content"],
                                 result.message)
                self.assertEqual(self.service.apply(event_id, "later-worker", intent), result)
                self.assertEqual(len(self.rows("outbox")), count + 1)
        self.assertEqual(self.rows("subscription_runs"), [])

    def test_missing_and_cancelled_targets_complete_with_help_without_mutation(self):
        saved = self.apply().subscription
        cancelled = self.subscriptions.cancel(saved.id, saved.version, now=NOW)
        for action in ("cancel_subscription", "run_subscription_now"):
            for number in (999, 1):
                with self.subTest(action=action, number=number):
                    event_id = self.event()
                    count = len(self.rows("outbox"))
                    result = self.business_result(event_id, parse_intent(
                        {"action": action, "subscription_number": number}))
                    self.assertIsNone(result.subscription)
                    self.assertIn("查看订阅", result.message)
                    self.assertEqual(self.events.get(event_id).status, "completed")
                    self.assertEqual(len(self.rows("outbox")), count + 1)
                    self.assertEqual(self.subscriptions.get(cancelled.id), cancelled)
        self.assertEqual(self.rows("subscription_runs"), [])

    def test_same_display_number_in_different_groups_targets_event_group(self):
        other = self.apply(chat_id="chat-b").subscription
        here = self.apply().subscription
        cancelled = self.apply({"action": "cancel_subscription", "subscription_number": 1}, sender_id="other")
        self.assertEqual(cancelled.subscription.id, here.id)
        self.assertEqual(self.subscriptions.get(other.id).state, "ready")

    def test_run_now_is_shared_and_preserves_paused_schedule(self):
        saved = self.apply().subscription
        paused = self.subscriptions.pause(saved.id, saved.version, now=NOW)
        result = self.apply({"action": "run_subscription_now", "subscription_number": 1}, sender_id="other")
        current = self.subscriptions.get(saved.id)
        self.assertEqual((current.state, current.next_run_at), ("paused", paused.next_run_at))
        runs = self.rows("subscription_runs")
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["trigger"], runs[0]["subscription_id"]), ("manual", saved.id))
        self.assertEqual(result.subscription.id, saved.id)

    def test_empty_expansions_return_help_and_complete_event_without_manual_run(self):
        saved = self.apply(dict(CREATE, search_terms=[])).subscription
        self.assertEqual(saved.state, "search_terms_pending")
        for paused in (False, True):
            if paused:
                saved = self.subscriptions.pause(saved.id, saved.version, now=NOW)
            event_id = self.event()
            count = len(self.rows("outbox"))
            with self.subTest(paused=paused):
                result = self.business_result(event_id, parse_intent(
                    {"action": "run_subscription_now", "subscription_number": 1}))
                self.assertIn("等待", result.message)
                self.assertIn("搜索词", result.message)
                self.assertIn("立即推送 1", result.message)
                self.assertEqual(self.events.get(event_id).status, "completed")
                self.assertEqual(self.subscriptions.get(saved.id), saved)
                self.assertEqual(len(self.rows("outbox")), count + 1)
        self.assertEqual(self.rows("subscription_runs"), [])

    def test_invalid_target_reply_storage_failure_rolls_back_event_completion(self):
        event_id = self.event()
        with patch.object(OutboxRepository, "enqueue", side_effect=RuntimeError("injected storage failure")):
            with self.assertRaises(RuntimeError):
                self.business_result(event_id, parse_intent(
                    {"action": "cancel_subscription", "subscription_number": 999}))
        self.assertEqual(self.events.get(event_id).status, "leased")
        self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.rows("subscriptions"), [])

    def test_unexpected_repository_errors_do_not_become_business_help(self):
        saved = self.apply().subscription
        for error in (VersionConflict("changed"), RuntimeError("storage failed"), ValidationError("unexpected")):
            event_id = self.event()
            with self.subTest(error=type(error).__name__):
                with patch.object(SubscriptionRepository, "request_manual_run", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.service.apply(event_id, "worker", parse_intent(
                            {"action": "run_subscription_now", "subscription_number": 1}))
                self.assertEqual(self.events.get(event_id).status, "leased")
                self.assertEqual(self.subscriptions.get(saved.id), saved)
                self.assertEqual(len(self.rows("outbox")), 1)
                self.assertEqual(self.rows("subscription_runs"), [])

    def test_help_and_ambiguous_intent_produce_examples_without_subscription(self):
        for action in ("show_help", "clarification_required"):
            result = self.apply({"action": action})
            self.assertIn("订阅", result.message)
            self.assertIn("取消订阅 2", result.message)
            self.assertIn("立即推送 1", result.message)
        self.assertEqual(self.rows("subscriptions"), [])
        self.assertEqual(len(self.rows("outbox")), 2)

    def test_missing_pending_wrong_owner_and_expired_events_cannot_apply(self):
        pending = self.event(claim=False)
        for event_id, owner in (("missing", "worker"), (pending, "worker")):
            with self.subTest(event_id=event_id), self.assertRaises(LeaseConflict):
                self.service.apply(event_id, owner, parse_intent(CREATE))
        self.events.claim_pending("worker", 20, NOW, 1)
        with self.assertRaises(LeaseConflict):
            self.service.apply(pending, "stranger", parse_intent(CREATE))
        later = CommandService(self.database, clock=lambda: NOW + timedelta(seconds=1))
        with self.assertRaises(LeaseConflict):
            later.apply(pending, "worker", parse_intent(CREATE))
        self.assertEqual(self.rows("subscriptions"), [])
        self.assertEqual(self.rows("outbox"), [])

    def test_duplicate_application_returns_persisted_snapshot_even_after_changes(self):
        event_id = self.event()
        first = self.service.apply(event_id, "worker", parse_intent(CREATE))
        self.subscriptions.cancel(first.subscription.id, first.subscription.version, now=NOW)
        duplicate = self.service.apply(event_id, "later-worker", parse_intent(dict(CREATE, topic="changed")))
        self.assertEqual(duplicate, first)
        self.assertEqual(len(self.rows("subscriptions")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_replaying_list_and_manual_run_does_not_reexecute(self):
        self.apply()
        for value in ({"action": "list_subscriptions"}, {"action": "run_subscription_now", "subscription_number": 1}):
            event_id = self.event()
            first = self.service.apply(event_id, "worker", parse_intent(value))
            self.assertEqual(self.service.apply(event_id, "worker", parse_intent(value)), first)
        self.assertEqual(len(self.rows("subscription_runs")), 1)
        self.assertEqual(len(self.rows("outbox")), 3)

    def test_corrupt_completed_result_fails_safely_without_reapplying(self):
        event_id = self.event()
        self.service.apply(event_id, "worker", parse_intent(CREATE))
        for corrupted in ("{", '{"message": "forged", "extra": true}',
                          '{"message": 123, "subscription": null, "subscriptions": []}'):
            with self.database.connect() as connection:
                connection.execute("UPDATE inbound_events SET result_summary = ? WHERE event_id = ?", (corrupted, event_id))
            with self.subTest(corrupted=corrupted), self.assertRaises(ValidationError):
                self.service.apply(event_id, "worker", parse_intent(CREATE))
        self.assertEqual(len(self.rows("subscriptions")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_duplicate_fields_in_completed_result_are_rejected(self):
        event_id = self.event()
        self.service.apply(event_id, "worker", parse_intent({"action": "show_help"}))
        raw = '{"message":"first","message":"second","subscription":null,"subscriptions":[]}'
        with self.database.connect() as connection:
            connection.execute("UPDATE inbound_events SET result_summary = ? WHERE event_id = ?", (raw, event_id))
        with self.assertRaises(ValidationError):
            self.service.apply(event_id, "worker", parse_intent(CREATE))
        self.assertEqual(self.rows("subscriptions"), [])
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_list_handles_existing_manual_schedule_without_error(self):
        saved = self.subscriptions.create("chat-a", "member", "AI", ["AI"], ["AI"], Schedule("manual"), now=NOW)
        result = self.apply({"action": "list_subscriptions"})
        self.assertEqual(result.subscriptions[0].id, saved.id)
        self.assertIn("手动", result.message)

    def test_outbox_error_rolls_back_number_subscription_and_event(self):
        event_id = self.event()
        with patch.object(OutboxRepository, "enqueue", side_effect=RuntimeError("injected storage failure")):
            with self.assertRaises(RuntimeError):
                self.service.apply(event_id, "worker", parse_intent(CREATE))
        self.assertEqual(self.rows("subscriptions"), [])
        self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.rows("inbound_events")[0]["status"], "leased")
        retried = self.service.apply(event_id, "worker", parse_intent(CREATE))
        self.assertEqual(retried.subscription.display_number, 1)

    def test_completion_error_rolls_back_manual_run_and_outbox(self):
        saved = self.apply().subscription
        event_id = self.event()
        with patch.object(EventRepository, "complete", side_effect=RuntimeError("injected completion failure")):
            with self.assertRaises(RuntimeError):
                self.service.apply(event_id, "worker", parse_intent({"action": "run_subscription_now", "subscription_number": 1}))
        self.assertEqual(self.rows("subscription_runs"), [])
        self.assertEqual(len(self.rows("outbox")), 1)
        self.assertEqual(self.subscriptions.get(saved.id).version, saved.version)

    def test_concurrent_duplicate_apply_has_one_mutation_and_result(self):
        event_id = self.event()
        barrier = threading.Barrier(2)

        def apply():
            service = CommandService(Database(self.database.path), clock=lambda: NOW)
            barrier.wait()
            return service.apply(event_id, "worker", parse_intent(CREATE))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(apply) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.rows("subscriptions")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_service_rejects_action_inappropriate_direct_intent(self):
        event_id = self.event()
        with self.assertRaises(ValidationError):
            self.service.apply(event_id, "worker", Intent("show_help", subscription_number=1))
        self.assertEqual(self.rows("outbox"), [])

    def test_repository_caller_connection_rolls_back_all_nested_work(self):
        event_id = self.event()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                created = self.subscriptions.create("chat-a", "member", "AI", ["AI"], ["AI agents"], now=NOW,
                                                    connection=connection)
                self.assertEqual(self.subscriptions.get(created.id, connection=connection), created)
                self.assertEqual(self.subscriptions.get_by_number("chat-a", 1, connection=connection), created)
                self.assertEqual(self.subscriptions.list("chat-a", connection=connection), [created])
                self.subscriptions.request_manual_run(created.id, created.version, now=NOW, connection=connection)
                current = self.subscriptions.get(created.id, connection=connection)
                self.subscriptions.cancel(current.id, current.version, now=NOW, connection=connection)
                self.events.complete(event_id, "worker", "completed", connection=connection, now=NOW)
                raise RuntimeError("rollback")
        self.assertEqual(self.rows("subscriptions"), [])
        self.assertEqual(self.rows("subscription_runs"), [])
        self.assertEqual(self.rows("inbound_events")[0]["status"], "leased")

    def test_event_failure_participates_in_caller_transaction_and_checks_expiry(self):
        event_id = self.event()
        with self.assertRaises(RuntimeError):
            with self.database.connect() as connection:
                self.events.fail(event_id, "worker", "failed", connection=connection, now=NOW)
                raise RuntimeError("rollback")
        self.assertEqual(self.rows("inbound_events")[0]["status"], "leased")
        with self.assertRaises(LeaseConflict):
            self.events.fail(event_id, "worker", "failed", now=NOW + timedelta(seconds=900))


if __name__ == "__main__":
    unittest.main()
