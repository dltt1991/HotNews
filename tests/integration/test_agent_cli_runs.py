"""Restricted agent commands exchange JSON through the real Python CLI."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hotnews.domain import Schedule
from hotnews.storage.database import Database
from hotnews.storage.events import LeaseRepository
from hotnews.storage.subscriptions import SubscriptionRepository


SECRET = "do-not-echo-model-diagnostics"


class AgentCLIRunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.database = Database(str(self.path / "runs.db"))
        self.database.migrate()
        self.subscriptions = SubscriptionRepository(self.database)
        self.config = self.path / "config.json"
        self.config.write_text(json.dumps({"database_path": self.database.path}), encoding="utf-8")

    def invoke(self, action, value):
        return subprocess.run([sys.executable, "-m", "hotnews.cli", "--config", str(self.config), "agent", action],
                              input=json.dumps(value), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=dict(os.environ, PYTHONPATH="src", FEISHU_APP_SECRET=SECRET), timeout=10)

    def success(self, process):
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(SECRET, process.stderr)
        self.assertNotIn(SECRET, process.stdout)
        return json.loads(process.stdout)

    def invalid(self, process):
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"error": "validation_error"})
        self.assertNotIn(SECRET, process.stderr + process.stdout)
        self.assertNotIn("Traceback", process.stderr)

    def create(self, **changes):
        values = dict(chat_id="chat", creator_id="member", topic="AI", keywords=["AI"], search_terms=["AI models"],
                      schedule=Schedule("interval", interval_minutes=5), now=datetime.now(timezone.utc) - timedelta(days=3))
        values.update(changes)
        return self.subscriptions.create(**values)

    def rows(self, table):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]

    def result(self, **changes):
        value = {"title": "新模型", "url": "https://official.example/model?utm_source=test", "source": "官方",
                 "published_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                 "summary": "发布了新模型。能够处理复杂任务。", "event_key": "new model"}
        value.update(changes)
        return value

    def claim(self):
        return self.success(self.invoke("claim-due", {"owner": "worker"}))["runs"]

    def test_global_lease_acquire_renew_release_owner_checks_and_replays(self):
        self.assertEqual(self.success(self.invoke("acquire-run-lease", {"owner": "first"})), {"acquired": True})
        self.assertEqual(self.success(self.invoke("acquire-run-lease", {"owner": "first"})), {"acquired": True})
        self.assertEqual(self.success(self.invoke("acquire-run-lease", {"owner": "other"})), {"acquired": False})
        self.assertEqual(self.success(self.invoke("renew-run-lease", {"owner": "other"})), {"renewed": False})
        self.assertEqual(self.success(self.invoke("release-run-lease", {"owner": "other"})), {"released": False})
        self.assertEqual(self.success(self.invoke("renew-run-lease", {"owner": "first"})), {"renewed": True})
        self.assertEqual(self.success(self.invoke("release-run-lease", {"owner": "first"})), {"released": True})
        self.assertEqual(self.success(self.invoke("release-run-lease", {"owner": "first"})), {"released": False})
        self.assertEqual(self.success(self.invoke("acquire-run-lease", {"owner": "other"})), {"acquired": True})

    def test_expired_global_lease_cannot_be_renewed_but_can_be_reclaimed(self):
        LeaseRepository(self.database).acquire("hotnews-agent", "old", datetime.now(timezone.utc) - timedelta(minutes=16), 900)
        self.assertEqual(self.success(self.invoke("renew-run-lease", {"owner": "old"})), {"renewed": False})
        self.assertEqual(self.success(self.invoke("acquire-run-lease", {"owner": "new"})), {"acquired": True})

    def test_list_due_is_readonly_and_claim_returns_search_context_with_bounded_batch(self):
        for _ in range(4):
            self.create()
        before = self.rows("subscriptions"), self.rows("subscription_runs")
        listed = self.success(self.invoke("list-due", {}))["subscriptions"]
        self.assertEqual(len(listed), 3)
        self.assertEqual((self.rows("subscriptions"), self.rows("subscription_runs")), before)
        claimed = self.claim()
        self.assertEqual(len(claimed), 3)
        self.assertEqual(claimed[0]["subscription"]["search_terms"], ["AI models"])
        self.assertEqual(claimed[0]["run"]["lease_owner"], "worker")
        self.assertEqual(len(self.claim()), 1)

    def test_complete_run_prepares_one_digest_and_duplicate_completion_is_idempotent(self):
        sub = self.create()
        run_id = self.claim()[0]["run"]["id"]
        value = {"run_id": run_id, "owner": "worker", "results": [self.result()], "search_window_days": 1}
        first = self.success(self.invoke("complete-run", value))
        second = self.success(self.invoke("complete-run", value))
        self.assertEqual(first, second)
        self.assertEqual(first["run"]["status"], "awaiting_delivery")
        self.assertEqual(len(self.rows("outbox")), 1)
        self.assertEqual(self.success(self.invoke("history", {"subscription_id": sub.id})), {"history": []})
        self.assertEqual(self.subscriptions.get(sub.id).next_run_at, sub.next_run_at)

    def test_complete_run_wrong_owner_expiry_and_version_conflict_are_rejected(self):
        sub = self.create()
        run_id = self.claim()[0]["run"]["id"]
        value = {"run_id": run_id, "owner": "other", "results": []}
        self.invalid(self.invoke("complete-run", value))
        with self.database.connect() as connection:
            connection.execute("UPDATE subscription_runs SET lease_until = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (run_id,))
        self.invalid(self.invoke("complete-run", dict(value, owner="worker")))
        reclaimed = self.success(self.invoke("claim-due", {"owner": "new"}))["runs"][0]
        self.assertEqual(reclaimed["run"]["id"], run_id)
        self.subscriptions.update(sub.id, sub.version, keywords=["energy"])
        self.invalid(self.invoke("complete-run", dict(value, owner="new")))
        self.assertEqual(self.rows("outbox"), [])

    def test_complete_run_rejects_unreliable_dates_and_unknown_result_fields(self):
        self.create()
        run_id = self.claim()[0]["run"]["id"]
        invalid = [self.result(published_at=None), self.result(published_at="unknown"),
                   self.result(published_at="2026-10-02T00:00:00"),
                   self.result(published_at=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()),
                   self.result(published_at=True), self.result(sql=SECRET), self.result(references=SECRET),
                   self.result(event_key=False), self.result(url="javascript:alert(1)")]
        for item in invalid:
            with self.subTest(item=item):
                self.invalid(self.invoke("complete-run", {"run_id": run_id, "owner": "worker", "results": [item]}))
        self.invalid(self.invoke("complete-run", {"run_id": run_id, "owner": "worker", "results": [self.result()] * 11}))
        self.assertEqual(self.rows("articles"), [])

    def test_term_refresh_claim_complete_and_fail_honor_versions_and_leases(self):
        sub = self.create(search_terms=[])
        claimed = self.success(self.invoke("claim-term-refresh", {"owner": "worker"}))["subscriptions"][0]
        value = {"subscription_id": sub.id, "owner": "worker", "expected_version": claimed["version"], "terms": ["AI", "人工智能"]}
        self.invalid(self.invoke("complete-term-refresh", dict(value, owner="other")))
        self.invalid(self.invoke("complete-term-refresh", dict(value, expected_version=1)))
        updated = self.success(self.invoke("complete-term-refresh", value))["subscription"]
        self.assertEqual((updated["state"], updated["search_terms"]), ("ready", ["AI", "人工智能"]))
        self.invalid(self.invoke("complete-term-refresh", value))
        edited = self.subscriptions.update(sub.id, updated["version"], keywords=["models"])
        claimed = self.success(self.invoke("claim-term-refresh", {"owner": "worker"}))["subscriptions"][0]
        fail = {"subscription_id": sub.id, "owner": "worker", "expected_version": claimed["version"], "error": SECRET}
        released = self.success(self.invoke("fail-term-refresh", fail))["subscription"]
        self.assertEqual(released["state"], "search_terms_pending")
        self.assertEqual(released["version"], edited.version + 2)
        self.assertIsNone(self.rows("subscriptions")[0]["lease_owner"])

    def test_expired_term_refresh_owner_cannot_complete_or_fail(self):
        sub = self.create(search_terms=[])
        claimed = self.subscriptions.claim_pending_terms("worker", 1, datetime.now(timezone.utc) - timedelta(minutes=16), 900)[0]
        common = {"subscription_id": sub.id, "owner": "worker", "expected_version": claimed.version}
        self.invalid(self.invoke("complete-term-refresh", dict(common, terms=["AI"])))
        self.invalid(self.invoke("fail-term-refresh", dict(common, error=SECRET)))

    def test_fail_run_stores_safe_reason_and_duplicate_failure_does_not_increment_twice(self):
        sub = self.create()
        run_id = self.claim()[0]["run"]["id"]
        value = {"run_id": run_id, "owner": "worker", "error": SECRET}
        self.invalid(self.invoke("fail-run", dict(value, owner="other")))
        first = self.success(self.invoke("fail-run", value))
        second = self.success(self.invoke("fail-run", value))
        self.assertEqual(first, second)
        self.assertEqual(first["run"]["status"], "failed")
        self.assertEqual(self.subscriptions.get(sub.id).consecutive_failures, 1)
        self.assertNotIn(SECRET, self.rows("subscription_runs")[0]["last_error"])

    def test_invalid_envelopes_and_bool_numeric_values_are_rejected(self):
        inputs = [("acquire-run-lease", {"owner": True}), ("renew-run-lease", {"owner": "a", "name": SECRET}),
                  ("release-run-lease", {"owner": " "}), ("claim-due", {"owner": "a", "limit": 4}),
                  ("claim-due", {"owner": "a", "limit": True}), ("list-due", {"limit": 0}),
                  ("list-due", {"owner": "a"}), ("history", {"subscription_id": []}),
                  ("claim-term-refresh", {"owner": "a", "limit": 4}),
                  ("complete-term-refresh", {"subscription_id": "x", "owner": "a", "expected_version": True, "terms": ["AI"]}),
                  ("complete-run", {"run_id": "x", "owner": "a", "results": {}, "search_window_days": 1}),
                  ("complete-run", {"run_id": "x", "owner": "a", "results": [], "search_window_days": True}),
                  ("fail-run", {"run_id": "x", "owner": "a", "error": []})]
        for action, value in inputs:
            with self.subTest(action=action):
                self.invalid(self.invoke(action, value))
        self.assertEqual(self.rows("subscription_runs"), [])
        self.assertEqual(self.rows("agent_leases"), [])

    def test_unknown_term_subscription_and_history_identifiers_are_validation_failures(self):
        self.invalid(self.invoke("history", {"subscription_id": "missing"}))
        self.invalid(self.invoke("complete-term-refresh", {"subscription_id": "missing", "owner": "worker",
                                                            "expected_version": 1, "terms": ["AI"]}))
        self.invalid(self.invoke("fail-term-refresh", {"subscription_id": "missing", "owner": "worker",
                                                        "expected_version": 1, "error": SECRET}))


if __name__ == "__main__":
    unittest.main()
