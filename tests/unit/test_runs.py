"""Run claiming and durable delivery preparation use real SQLite transactions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hotnews.domain import LeaseConflict, NewsResult, Schedule, ValidationError, VersionConflict
from hotnews.storage.database import Database
from hotnews.storage.runs import RunRepository, canonicalize_url
from hotnews.storage.subscriptions import SubscriptionRepository


NOW = datetime(2026, 10, 2, 2, tzinfo=timezone.utc)


class RunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(str(Path(directory.name) / "runs.db"))
        self.database.migrate()
        self.subscriptions = SubscriptionRepository(self.database)
        self.runs = RunRepository(self.database)

    def create(self, **changes):
        values = dict(chat_id="chat", creator_id="member", topic="AI", keywords=["AI"],
                      search_terms=["artificial intelligence"],
                      schedule=Schedule("interval", interval_minutes=5), now=NOW - timedelta(days=3))
        values.update(changes)
        return self.subscriptions.create(**values)

    def result(self, **changes):
        values = dict(title="A new model", url="https://news.example/item?utm_source=search#top",
                      source="Official", published_at=NOW - timedelta(hours=2),
                      summary="发布了新模型。性能有所提升。", event_key="model release", references=())
        values.update(changes)
        return NewsResult(**values)

    def rows(self, table):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]

    def claim(self, owner="worker", now=NOW):
        return self.runs.claim_due(owner, 3, now, 900)

    def confirm(self, run_id):
        # Simulate the external confirmation boundary that Task 12 will own.
        with self.database.connect() as connection:
            connection.execute("UPDATE deliveries SET status = 'sent', sent_at = ? WHERE run_id = ?",
                               ("2026-10-02T02:00:00.000000Z", run_id))
            connection.execute("UPDATE subscription_runs SET status = 'completed' WHERE id = ?", (run_id,))
            connection.execute("UPDATE outbox SET status = 'sent' WHERE run_id = ?", (run_id,))

    def test_due_filters_and_only_one_catchup_for_each_subscription(self):
        due = self.create()
        self.create(now=NOW)
        paused = self.create()
        self.subscriptions.pause(paused.id, paused.version, now=NOW)
        self.create(search_terms=[])
        cancelled = self.create()
        self.subscriptions.cancel(cancelled.id, cancelled.version, now=NOW)
        claimed = self.claim()
        self.assertEqual([run.subscription_id for run in claimed], [due.id])
        self.assertEqual(claimed[0].trigger, "scheduled")
        self.assertEqual(self.claim("another"), [])
        self.assertEqual(len(self.rows("subscription_runs")), 1)
        self.assertEqual(self.subscriptions.get(due.id).next_run_at,
                         NOW - timedelta(days=3) + timedelta(minutes=5))

    def test_manual_on_paused_is_claimed_once_and_keeps_regular_schedule(self):
        sub = self.create()
        paused = self.subscriptions.pause(sub.id, sub.version, now=NOW)
        run_id = self.subscriptions.request_manual_run(sub.id, paused.version, now=NOW)
        claimed = self.claim()
        self.assertEqual([(run.id, run.trigger) for run in claimed], [(run_id, "manual")])
        self.runs.complete(run_id, "worker", [], now=NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual((current.state, current.next_run_at), ("paused", paused.next_run_at))
        self.assertEqual(current.last_success_at, NOW)

    def test_expired_run_reclaimed_with_same_id_and_old_owner_cannot_complete(self):
        self.create()
        run = self.claim()[0]
        self.assertEqual(self.claim("other", NOW + timedelta(seconds=899)), [])
        reclaimed = self.claim("other", NOW + timedelta(seconds=900))[0]
        self.assertEqual((reclaimed.id, reclaimed.lease_owner), (run.id, "other"))
        with self.assertRaises(LeaseConflict):
            self.runs.complete(run.id, "worker", [], now=NOW + timedelta(seconds=900))
        self.runs.complete(run.id, "other", [], now=NOW + timedelta(seconds=900))
        self.assertEqual(len(self.rows("subscription_runs")), 1)

    def test_expired_owner_and_changed_subscription_cannot_commit(self):
        sub = self.create()
        run = self.claim()[0]
        with self.assertRaises(LeaseConflict):
            self.runs.complete(run.id, "worker", [], now=NOW + timedelta(seconds=900))
        self.subscriptions.update(sub.id, sub.version, keywords=["energy"], now=NOW)
        with self.assertRaises(VersionConflict):
            self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        self.assertEqual(self.rows("articles"), [])
        self.assertEqual(self.rows("outbox"), [])

    def test_empty_success_advances_from_now_and_resets_failure_latch_without_outbox(self):
        sub = self.create()
        with self.database.connect() as connection:
            connection.execute("UPDATE subscriptions SET consecutive_failures = 4, alerted = 1 WHERE id = ?", (sub.id,))
        run = self.claim()[0]
        completed = self.runs.complete(run.id, "worker", [], now=NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(current.next_run_at, datetime(2026, 10, 2, 2, 5, tzinfo=timezone.utc))
        self.assertEqual((current.consecutive_failures, current.alerted, current.last_success_at), (0, False, NOW))
        self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.claim(), [])

    def test_daily_catchup_advances_to_next_local_day_boundary(self):
        sub = self.create(schedule=Schedule("daily", daily_at="09:00"))
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [], now=NOW)
        self.assertEqual(self.subscriptions.get(sub.id).next_run_at,
                         datetime(2026, 10, 3, 1, tzinfo=timezone.utc))

    def test_nonempty_success_is_atomic_and_waits_for_confirmation(self):
        sub = self.create()
        run = self.claim()[0]
        completed = self.runs.complete(run.id, "worker", [self.result()], now=NOW, search_window_days=1)
        self.assertEqual(completed.status, "awaiting_delivery")
        article, delivery, outbox = (self.rows(table)[0] for table in ("articles", "deliveries", "outbox"))
        self.assertEqual(article["url"], "https://news.example/item")
        self.assertEqual((delivery["run_id"], delivery["outbox_id"], outbox["run_id"]),
                         (run.id, outbox["id"], run.id))
        self.assertEqual((delivery["status"], outbox["status"], outbox["kind"]), ("pending", "pending", "card"))
        self.assertEqual(self.subscriptions.get(sub.id).next_run_at, sub.next_run_at)
        self.assertEqual(self.runs.history(sub.id), [])
        self.assertEqual(self.claim("another"), [])
        self.assertEqual(self.runs.complete(run.id, "worker", [self.result()], now=NOW).status, "awaiting_delivery")
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_completed_replay_ignores_elapsed_search_window(self):
        self.create()
        run = self.claim()[0]
        item = self.result()
        self.runs.complete(run.id, "worker", [item], now=NOW)
        replay = self.runs.complete(run.id, "worker", [item], now=NOW + timedelta(days=31))
        self.assertEqual(replay.status, "awaiting_delivery")
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_concurrent_claimers_never_create_two_active_runs(self):
        self.create()
        barrier = threading.Barrier(2)
        def claim(owner):
            barrier.wait()
            return self.runs.claim_due(owner, 3, NOW, 900)
        with ThreadPoolExecutor(max_workers=2) as workers:
            claims = list(workers.map(claim, ("first", "second")))
        self.assertEqual(sorted(len(batch) for batch in claims), [0, 1])
        self.assertEqual(len(self.rows("subscription_runs")), 1)

    def test_stale_version_failure_closes_run_without_altering_new_subscription(self):
        sub = self.create()
        run = self.claim()[0]
        current = self.subscriptions.update(sub.id, sub.version, keywords=["energy"], now=NOW)
        failed = self.runs.fail(run.id, "worker", "subscription changed", now=NOW)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(self.subscriptions.get(sub.id), current)
        self.assertEqual(self.rows("outbox"), [])

    def test_failed_delivery_can_be_prepared_again_with_same_article_without_duplicate_pair(self):
        self.create()
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        with self.database.connect() as connection:
            connection.execute("UPDATE deliveries SET status = 'failed' WHERE run_id = ?", (run.id,))
            connection.execute("UPDATE subscription_runs SET status = 'failed' WHERE id = ?", (run.id,))
            connection.execute("UPDATE outbox SET status = 'failed' WHERE run_id = ?", (run.id,))
        retry = self.claim()[0]
        self.runs.complete(retry.id, "worker", [self.result()], now=NOW)
        self.assertEqual(len(self.rows("articles")), 1)
        self.assertEqual(len(self.rows("deliveries")), 1)
        self.assertEqual(self.rows("deliveries")[0]["run_id"], retry.id)
        self.assertEqual(self.rows("deliveries")[0]["status"], "pending")

    def test_failed_card_preparation_rolls_back_articles_and_deliveries(self):
        self.create()
        run = self.claim()[0]
        with patch("hotnews.storage.runs.render_digest", side_effect=RuntimeError("render failure")):
            with self.assertRaises(RuntimeError):
                self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        for table in ("articles", "deliveries", "outbox"):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(self.rows("subscription_runs")[0]["status"], "leased")

    def test_batch_url_and_event_duplicates_are_excluded(self):
        self.create()
        run = self.claim()[0]
        results = [self.result(), self.result(event_key="other event", url="https://news.example/item#two"),
                   self.result(url="https://other.example/story", event_key="  MODEL   Release "),
                   self.result(url="https://other.example/unique", event_key="unique event")]
        self.runs.complete(run.id, "worker", results, now=NOW)
        self.assertEqual(len(self.rows("deliveries")), 2)
        self.assertEqual(self.rows("subscription_runs")[0]["selected_count"], 2)

    def test_sent_history_blocks_duplicate_urls_and_event_reports(self):
        sub = self.create()
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        self.confirm(run.id)
        history = self.runs.history(sub.id)
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["url"], history[0]["event_key"]),
                         ("https://news.example/item", "model release"))
        next_run = self.claim()[0]
        self.runs.complete(next_run.id, "worker",
                           [self.result(url="https://other.example/item", event_key="MODEL release"),
                            self.result(event_key="different")], now=NOW)
        self.assertEqual(self.rows("subscription_runs")[-1]["status"], "completed")
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_recreated_same_topic_inherits_history_but_other_topic_and_group_do_not(self):
        sub = self.create(keywords=[" AI ", "Models"])
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        self.confirm(run.id)
        self.subscriptions.cancel(sub.id, sub.version, now=NOW)
        recreated = self.create(keywords=["models", "ai"])
        other = self.create(keywords=["energy"])
        another_group = self.create(chat_id="another", keywords=["models", "AI"])
        self.assertEqual(len(self.runs.history(recreated.id)), 1)
        self.assertEqual(self.runs.history(other.id), [])
        self.assertEqual(self.runs.history(another_group.id), [])

    def test_recreated_original_topic_keeps_delivery_identity_after_edit_and_cancel(self):
        original = self.create(keywords=[" AI ", "Large    Models"])
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        self.confirm(run.id)
        edited = self.subscriptions.update(original.id, original.version,
                                           keywords=["energy"], now=NOW)
        # A subscription keeps its own history even when its current topic changes.
        self.assertEqual(len(self.runs.history(original.id)), 1)
        self.subscriptions.cancel(original.id, edited.version, now=NOW)
        recreated = self.create(keywords=["large models", "ai"])
        self.assertEqual([item["event_key"] for item in self.runs.history(recreated.id)], ["model release"])
        repeat = self.claim()[0]
        self.runs.complete(repeat.id, "worker", [self.result(url="https://other.example/same-event")], now=NOW)
        self.assertEqual(len(self.rows("outbox")), 1)
        self.assertEqual(self.rows("subscription_runs")[-1]["status"], "completed")

    def test_new_edited_topic_does_not_inherit_pre_edit_deliveries_of_another_subscription(self):
        original = self.create(keywords=["AI"])
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [self.result()], now=NOW)
        self.confirm(run.id)
        edited = self.subscriptions.update(original.id, original.version,
                                           keywords=["energy"], now=NOW)
        self.subscriptions.cancel(original.id, edited.version, now=NOW)
        new_topic = self.create(keywords=[" ENERGY "])
        self.assertEqual(self.runs.history(new_topic.id), [])

    def test_global_url_reuse_keeps_each_deliveries_actual_event_key(self):
        first = self.create(keywords=["AI"])
        first_run = self.claim()[0]
        self.runs.complete(first_run.id, "worker", [self.result(event_key="first event")], now=NOW)
        self.confirm(first_run.id)
        second = self.create(keywords=["energy"])
        second_run = next(run for run in self.claim() if run.subscription_id == second.id)
        self.runs.complete(second_run.id, "worker", [self.result(event_key=" ENERGY   LAUNCH ")], now=NOW)
        self.confirm(second_run.id)
        self.assertEqual(len(self.rows("articles")), 1)
        self.assertEqual([item["event_key"] for item in self.runs.history(first.id)], ["first event"])
        self.assertEqual([item["event_key"] for item in self.runs.history(second.id)], ["energy launch"])
        repeat = next(run for run in self.claim() if run.subscription_id == second.id)
        self.runs.complete(repeat.id, "worker",
                           [self.result(url="https://another.example/event", event_key="energy launch")], now=NOW)
        self.assertEqual(len(self.rows("outbox")), 2)
        self.assertEqual(len(self.rows("deliveries")), 2)
        # A different event with the first subscription's label is still new to
        # the second subscription; global article reuse must not suppress it.
        current = self.subscriptions.get(second.id)
        manual_id = self.subscriptions.request_manual_run(second.id, current.version, now=NOW)
        next_run = next(run for run in self.claim() if run.subscription_id == second.id)
        self.assertEqual(next_run.id, manual_id)
        self.runs.complete(next_run.id, "worker",
                           [self.result(url="https://another.example/different", event_key="first event")], now=NOW)
        self.assertEqual(len(self.rows("outbox")), 3)

    def test_invalid_dates_count_window_references_reject_without_mutation(self):
        self.create()
        run = self.claim()[0]
        cases = [[self.result()] * 11,
                 [self.result(published_at=NOW.replace(tzinfo=None))],
                 [self.result(published_at=NOW - timedelta(days=30, seconds=1))],
                 [self.result(published_at=NOW + timedelta(seconds=1))],
                 [self.result(references=("file:///secret",))], [None]]
        for results in cases:
            with self.subTest(results=results):
                with self.assertRaises(ValidationError):
                    self.runs.complete(run.id, "worker", results, now=NOW)
        with self.assertRaises(ValidationError):
            self.runs.complete(run.id, "worker", [self.result(published_at=NOW - timedelta(days=2))],
                               now=NOW, search_window_days=1)
        with self.assertRaises(ValidationError):
            self.runs.complete(run.id, "worker", [], now=NOW, search_window_days=True)
        self.assertEqual(self.rows("articles"), [])
        self.assertEqual(self.rows("outbox"), [])

    def test_boundary_thirty_day_result_is_accepted(self):
        self.create()
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [self.result(published_at=NOW - timedelta(days=30))], now=NOW)
        self.assertEqual(len(self.rows("deliveries")), 1)

    def test_three_failures_alert_once_and_success_resets_latch(self):
        sub = self.create()
        for count in range(1, 5):
            run = self.claim()[0]
            failed = self.runs.fail(run.id, "worker", "search failed", now=NOW)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, count)
            self.assertEqual(len(self.rows("outbox")), int(count >= 3))
            self.runs.fail(run.id, "worker", "search failed", now=NOW)
            self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, count)
        run = self.claim()[0]
        self.runs.complete(run.id, "worker", [], now=NOW)
        current = self.subscriptions.get(sub.id)
        self.assertEqual((current.consecutive_failures, current.alerted), (0, False))
        with self.assertRaises(LeaseConflict):
            self.runs.fail(run.id, "stranger", "failure", now=NOW)

    def test_dry_run_list_does_not_create_or_lease_work(self):
        due = self.create()
        paused = self.create()
        paused = self.subscriptions.pause(paused.id, paused.version, now=NOW)
        self.subscriptions.request_manual_run(paused.id, paused.version, now=NOW)
        before = self.rows("subscriptions"), self.rows("subscription_runs")
        self.assertEqual({sub.id for sub in self.runs.list_due(NOW, 3)}, {due.id, paused.id})
        self.assertEqual((self.rows("subscriptions"), self.rows("subscription_runs")), before)

    def test_pending_manual_queue_serializes_each_subscription(self):
        sub = self.create()
        self.subscriptions.request_manual_run(sub.id, sub.version, now=NOW)
        current = self.subscriptions.get(sub.id)
        self.subscriptions.request_manual_run(sub.id, current.version, now=NOW)
        claimed = self.claim()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].trigger, "manual")
        self.assertEqual(self.claim("another"), [])
        self.runs.complete(claimed[0].id, "worker", [], now=NOW)
        self.assertEqual(len(self.claim()), 1)


class URLTests(unittest.TestCase):
    def test_canonicalization_removes_tracking_and_fragments_preserving_meaningful_query(self):
        self.assertEqual(canonicalize_url("HTTPS://News.Example:443/story?z=2&utm_source=x&a=1&fbclid=y#top"),
                         "https://news.example/story?a=1&z=2")
        self.assertEqual(canonicalize_url("http://NEWS.example:80"), "http://news.example/")
        self.assertEqual(canonicalize_url("https://news.example:8443/a?id=3&id=1"),
                         "https://news.example:8443/a?id=1&id=3")

    def test_malformed_or_credential_urls_are_rejected(self):
        for value in ("file:///tmp/a", "https:///a", "https://user:secret@example.com", "https://host:bad/a"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    canonicalize_url(value)


if __name__ == "__main__":
    unittest.main()
