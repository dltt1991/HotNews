"""Group numbering, scheduling and optimistic subscription mutations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest

from hotnews.domain import LeaseConflict, Schedule, ValidationError, VersionConflict
from hotnews.storage.database import Database
from hotnews.storage.subscriptions import SubscriptionRepository, next_run


NOW = datetime(2026, 10, 2, 0, tzinfo=timezone.utc)  # 08:00 in Shanghai


class ScheduleTests(unittest.TestCase):
    def test_daily_next_time_is_strictly_future_at_local_boundary(self):
        schedule = Schedule("daily", daily_at="09:00")
        for instant, expected in (
            (NOW, datetime(2026, 10, 2, 1, tzinfo=timezone.utc)),
            (NOW + timedelta(hours=1), datetime(2026, 10, 3, 1, tzinfo=timezone.utc)),
            (NOW + timedelta(hours=2), datetime(2026, 10, 3, 1, tzinfo=timezone.utc)),
        ):
            with self.subTest(instant=instant):
                self.assertEqual(next_run(schedule, instant), expected)

    def test_interval_starts_from_current_time_after_missed_cycles(self):
        self.assertEqual(next_run(Schedule("interval", interval_minutes=5),
                                  NOW + timedelta(days=4)),
                         datetime(2026, 10, 6, 0, 5, tzinfo=timezone.utc))

    def test_unrepresentable_interval_next_time_is_validation_failure(self):
        for minutes in (10 ** 12, 10 ** 40):
            with self.subTest(minutes=minutes), self.assertRaises(ValidationError):
                next_run(Schedule("interval", interval_minutes=minutes), NOW)

    def test_interval_limit_is_derived_from_remaining_datetime_range(self):
        near_maximum = datetime(9999, 12, 31, 23, 50, tzinfo=timezone.utc)
        self.assertEqual(next_run(Schedule("interval", interval_minutes=9), near_maximum),
                         datetime(9999, 12, 31, 23, 59, tzinfo=timezone.utc))
        with self.assertRaises(ValidationError):
            next_run(Schedule("interval", interval_minutes=10), near_maximum)

    def test_interval_minimum_and_aware_timestamp_are_required(self):
        with self.assertRaises(ValidationError):
            Schedule("interval", interval_minutes=4)
        with self.assertRaises(ValueError):
            next_run(Schedule("daily", daily_at="09:00"), datetime(2026, 10, 2))

    def test_non_utc_input_returns_utc_and_manual_has_no_next_time(self):
        local = datetime(2026, 10, 2, 8, tzinfo=timezone(timedelta(hours=8)))
        scheduled = next_run(Schedule("daily", daily_at="09:00"), local)
        self.assertEqual(scheduled, datetime(2026, 10, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(scheduled.tzinfo, timezone.utc)
        self.assertIsNone(next_run(Schedule("manual"), NOW))


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(str(Path(directory.name) / "subscriptions.sqlite"))
        self.database.migrate()
        self.repository = SubscriptionRepository(self.database)

    def create(self, **changes):
        arguments = dict(chat_id="chat-a", creator_id="member", topic="AI 新闻",
                         keywords=["AI"], search_terms=["AI", "人工智能"], now=NOW)
        arguments.update(changes)
        return self.repository.create(**arguments)

    def row(self, subscription_id):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM subscriptions WHERE id = ?",
                                      (subscription_id,)).fetchone()

    def test_group_numbers_are_independent_and_cancelled_numbers_never_reused(self):
        first, second = self.create(), self.create(topic="能源", keywords=["能源"])
        other = self.create(chat_id="chat-b")
        self.assertEqual((first.display_number, second.display_number, other.display_number),
                         (1, 2, 1))
        self.repository.cancel(first.id, first.version, now=NOW)
        third = self.create()
        self.assertEqual(third.display_number, 3)
        self.assertEqual([s.id for s in self.repository.list("chat-a")], [second.id, third.id])
        self.assertEqual(len(self.repository.list(None, include_cancelled=True)), 4)
        self.assertIsNone(self.repository.get("missing"))

    def test_concurrent_creation_allocates_distinct_monotonic_numbers(self):
        barrier = threading.Barrier(4)
        def create_one(_):
            barrier.wait()
            return self.create().display_number
        with ThreadPoolExecutor(max_workers=4) as workers:
            numbers = list(workers.map(create_one, range(4)))
        self.assertEqual(sorted(numbers), [1, 2, 3, 4])

    def test_default_daily_nine_is_persisted_and_reloaded(self):
        subscription = self.create()
        self.assertEqual(subscription.schedule, Schedule("daily", daily_at="09:00"))
        self.assertEqual(subscription.next_run_at, datetime(2026, 10, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(subscription.version, 1)
        self.assertEqual(subscription.state, "ready")
        self.assertEqual(subscription.keywords, ("AI",))
        self.assertEqual(self.repository.get(subscription.id), subscription)
        row = self.row(subscription.id)
        self.assertEqual(row["created_at"], "2026-10-02T00:00:00.000000Z")
        self.assertEqual(row["next_run_at"], "2026-10-02T01:00:00.000000Z")

    def test_invalid_create_does_not_consume_number(self):
        with self.assertRaises(ValidationError):
            self.create(keywords=[])
        self.assertEqual(self.create().display_number, 1)

    def test_empty_expansions_create_pending_subscription(self):
        subscription = self.create(search_terms=[])
        self.assertEqual(subscription.state, "search_terms_pending")
        self.assertEqual(self.repository.claim_pending_terms("worker", 1, NOW, 60)[0].id,
                         subscription.id)

    def test_downtime_keeps_one_due_time_without_enqueuing_backfill(self):
        subscription = self.create(schedule=Schedule("interval", interval_minutes=5))
        restarted = SubscriptionRepository(self.database).get(subscription.id)
        self.assertEqual(restarted.next_run_at, datetime(2026, 10, 2, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(next_run(restarted.schedule, NOW + timedelta(days=3)),
                         datetime(2026, 10, 5, 0, 5, tzinfo=timezone.utc))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM subscription_runs").fetchone()[0], 0)

    def test_edit_recomputes_schedule_without_requesting_a_run(self):
        subscription = self.create()
        updated = self.repository.update(subscription.id, subscription.version,
                                         topic="科技", schedule=Schedule("interval", interval_minutes=30),
                                         now=NOW + timedelta(days=1))
        self.assertEqual(updated.topic, "科技")
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.next_run_at, datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(updated.search_terms, subscription.search_terms)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM subscription_runs").fetchone()[0], 0)

    def test_keyword_edit_clears_terms_and_invalidates_existing_refresh_lease(self):
        subscription = self.create(search_terms=[])
        claimed = self.repository.claim_pending_terms("worker", 1, NOW, 60)[0]
        updated = self.repository.update(subscription.id, claimed.version, keywords=["新能源"], now=NOW)
        self.assertEqual(updated.state, "search_terms_pending")
        self.assertEqual(updated.search_terms, ())
        self.assertIsNone(self.row(subscription.id)["lease_owner"])
        with self.assertRaises(VersionConflict):
            self.repository.complete_search_terms(subscription.id, "worker", claimed.version, ["AI"], now=NOW)

    def test_unchanged_keywords_preserve_terms(self):
        subscription = self.create()
        updated = self.repository.update(subscription.id, subscription.version, keywords=["AI"], now=NOW)
        self.assertEqual(updated.search_terms, ("AI", "人工智能"))
        self.assertEqual(updated.state, "ready")

    def test_stale_updates_reject_without_overwriting_current_row(self):
        subscription = self.create()
        updated = self.repository.update(subscription.id, 1, topic="新主题", now=NOW)
        for operation in (
            lambda: self.repository.update(subscription.id, 1, topic="旧主题", now=NOW),
            lambda: self.repository.pause(subscription.id, 1, now=NOW),
            lambda: self.repository.resume(subscription.id, 1, now=NOW),
            lambda: self.repository.cancel(subscription.id, 1, now=NOW),
            lambda: self.repository.request_manual_run(subscription.id, 1, now=NOW),
        ):
            with self.assertRaises(VersionConflict):
                operation()
            self.assertEqual(self.repository.get(subscription.id), updated)

    def test_pause_preserves_time_and_resume_recomputes_it(self):
        subscription = self.create()
        paused = self.repository.pause(subscription.id, 1, now=NOW)
        self.assertEqual((paused.state, paused.next_run_at, paused.version),
                         ("paused", subscription.next_run_at, 2))
        resumed = self.repository.resume(subscription.id, 2, now=NOW + timedelta(hours=2))
        self.assertEqual((resumed.state, resumed.version), ("ready", 3))
        self.assertEqual(resumed.next_run_at, datetime(2026, 10, 3, 1, tzinfo=timezone.utc))

    def test_cancel_is_soft_and_cannot_be_resumed_or_edited(self):
        subscription = self.create()
        cancelled = self.repository.cancel(subscription.id, 1, now=NOW + timedelta(seconds=1))
        self.assertEqual((cancelled.state, cancelled.version, cancelled.cancelled_at),
                         ("cancelled", 2, NOW + timedelta(seconds=1)))
        self.assertEqual(self.repository.get(subscription.id), cancelled)
        for operation in (
            lambda: self.repository.resume(subscription.id, 2, now=NOW),
            lambda: self.repository.pause(subscription.id, 2, now=NOW),
            lambda: self.repository.update(subscription.id, 2, topic="changed", now=NOW),
        ):
            with self.assertRaises(ValidationError):
                operation()

    def test_pending_terms_claim_is_fifo_limited_and_reclaims_exact_expiry(self):
        first = self.create(search_terms=[])
        second = self.create(search_terms=[], now=NOW + timedelta(seconds=1))
        self.create()  # ready rows cannot be claimed
        self.assertEqual(self.repository.claim_pending_terms("worker", 0, NOW, 60), [])
        claimed = self.repository.claim_pending_terms("first", 1, NOW, 60)[0]
        self.assertEqual((claimed.id, claimed.version), (first.id, 2))
        claimed_second = self.repository.claim_pending_terms("second", 10, NOW, 60)
        self.assertEqual([s.id for s in claimed_second], [second.id])
        self.assertEqual(self.repository.claim_pending_terms("second", 10, NOW + timedelta(seconds=59), 60), [])
        reclaimed = self.repository.claim_pending_terms("second", 10, NOW + timedelta(seconds=60), 60)
        self.assertEqual([s.id for s in reclaimed], [first.id, second.id])
        self.assertEqual(reclaimed[0].version, 3)
        with self.assertRaises(VersionConflict):
            self.repository.complete_search_terms(first.id, "first", 2, ["AI"], now=NOW)

    def test_term_claim_uses_fixed_width_fractional_timestamp_order(self):
        subscription = self.create(search_terms=[])
        claim_time = NOW + timedelta(microseconds=500000)
        claimed = self.repository.claim_pending_terms("first", 1, claim_time, 1)[0]
        self.assertEqual(self.repository.claim_pending_terms("other", 1, NOW + timedelta(seconds=1), 1), [])
        self.assertEqual(self.repository.claim_pending_terms("other", 1, NOW + timedelta(seconds=1, microseconds=500000), 1)[0].id,
                         subscription.id)
        self.assertEqual(claimed.version, 2)

    def test_term_completion_checks_owner_version_and_terms(self):
        subscription = self.create(search_terms=[])
        claimed = self.repository.claim_pending_terms("worker", 1, NOW, 60)[0]
        with self.assertRaises(LeaseConflict):
            self.repository.complete_search_terms(subscription.id, "other", claimed.version, ["AI"], now=NOW)
        with self.assertRaises(ValidationError):
            self.repository.complete_search_terms(subscription.id, "worker", claimed.version, [], now=NOW)
        completed = self.repository.complete_search_terms(subscription.id, "worker", claimed.version,
                                                         ["AI", "人工智能"], now=NOW)
        self.assertEqual((completed.state, completed.version, completed.search_terms),
                         ("ready", 3, ("AI", "人工智能")))
        self.assertIsNone(self.row(subscription.id)["lease_owner"])
        with self.assertRaises(VersionConflict):
            self.repository.complete_search_terms(subscription.id, "worker", claimed.version, ["old"], now=NOW)

    def test_fail_terms_releases_lease_and_keeps_retryable_pending_state(self):
        subscription = self.create(search_terms=[])
        claimed = self.repository.claim_pending_terms("worker", 1, NOW, 60)[0]
        with self.assertRaises(LeaseConflict):
            self.repository.fail_search_terms(subscription.id, "other", claimed.version, "failed", now=NOW)
        failed = self.repository.fail_search_terms(subscription.id, "worker", claimed.version, "failed", now=NOW)
        self.assertEqual((failed.state, failed.version), ("search_terms_pending", 3))
        self.assertIsNone(self.row(subscription.id)["lease_owner"])
        self.assertEqual(self.repository.claim_pending_terms("other", 1, NOW, 60)[0].version, 4)

    def test_expired_term_owner_cannot_complete_or_fail_at_deadline(self):
        subscription = self.create(search_terms=[])
        claimed = self.repository.claim_pending_terms("worker", 1, NOW, 60)[0]
        deadline = NOW + timedelta(seconds=60)
        with self.assertRaises(LeaseConflict):
            self.repository.complete_search_terms(subscription.id, "worker", claimed.version,
                                                  ["AI"], now=deadline)
        with self.assertRaises(LeaseConflict):
            self.repository.fail_search_terms(subscription.id, "worker", claimed.version,
                                              "failure", now=deadline)
        self.assertEqual(self.repository.get(subscription.id).search_terms, ())

    def test_paused_keyword_refresh_preserves_pause_and_resume_pending_semantics(self):
        subscription = self.create()
        paused = self.repository.pause(subscription.id, 1, now=NOW)
        edited = self.repository.update(subscription.id, paused.version, keywords=["能源"], now=NOW)
        self.assertEqual((edited.state, edited.search_terms), ("paused", ()))
        claimed = self.repository.claim_pending_terms("worker", 1, NOW, 60)[0]
        complete = self.repository.complete_search_terms(subscription.id, "worker", claimed.version,
                                                        ["energy"], now=NOW)
        self.assertEqual(complete.state, "paused")
        resumed = self.repository.resume(subscription.id, complete.version, now=NOW)
        self.assertEqual(resumed.state, "ready")
        paused = self.repository.pause(subscription.id, resumed.version, now=NOW)
        edited = self.repository.update(subscription.id, paused.version, keywords=["汽车"], now=NOW)
        resumed = self.repository.resume(subscription.id, edited.version, now=NOW)
        self.assertEqual(resumed.state, "search_terms_pending")

    def test_manual_run_keeps_paused_and_regular_plan_and_increments_version(self):
        subscription = self.create()
        paused = self.repository.pause(subscription.id, 1, now=NOW)
        run_id = self.repository.request_manual_run(subscription.id, paused.version, now=NOW)
        updated = self.repository.get(subscription.id)
        self.assertEqual((updated.state, updated.schedule, updated.next_run_at, updated.version),
                         ("paused", paused.schedule, paused.next_run_at, 3))
        with self.database.connect() as connection:
            run = connection.execute("SELECT * FROM subscription_runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual((run["subscription_id"], run["trigger"], run["status"], run["created_at"]),
                         (subscription.id, "manual", "pending", "2026-10-02T00:00:00.000000Z"))

    def test_manual_run_requires_terms_and_non_cancelled_state(self):
        pending = self.create(search_terms=[])
        with self.assertRaises(ValidationError):
            self.repository.request_manual_run(pending.id, pending.version, now=NOW)
        paused = self.repository.pause(pending.id, pending.version, now=NOW)
        with self.assertRaises(ValidationError):
            self.repository.request_manual_run(paused.id, paused.version, now=NOW)
        ready = self.create()
        cancelled = self.repository.cancel(ready.id, ready.version, now=NOW)
        with self.assertRaises(ValidationError):
            self.repository.request_manual_run(cancelled.id, cancelled.version, now=NOW)


if __name__ == "__main__":
    unittest.main()
