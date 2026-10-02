"""The real CLI exchanges one JSON document and rejects untrusted input."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hotnews.storage.database import Database
from hotnews.storage.events import EventRepository


SECRET = "never-echo-this-credential"
CREATE = {"action": "create_subscription", "topic": "AI Agent", "keywords": ["AI"],
          "search_terms": ["AI agents"]}


class AgentCLIEventTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.database = Database(str(self.path / "events.db"))
        self.database.migrate()
        self.config = self.path / "config.json"
        self.config.write_text(json.dumps({"database_path": self.database.path}), encoding="utf-8")
        self.events = EventRepository(self.database)

    def event(self, number=1, chat_id="chat-a", sender_id="member-a"):
        self.events.insert({"event_id": "event-%d" % number, "message_id": "message-%d" % number,
                            "chat_id": chat_id, "sender_id": sender_id, "text": "订阅 AI",
                            "received_at": datetime.now(timezone.utc)})

    def invoke(self, action, value=None, raw=None, config=None):
        environment = dict(os.environ, PYTHONPATH="src", FEISHU_APP_SECRET=SECRET,
                           FEISHU_VERIFICATION_TOKEN=SECRET, FEISHU_ENCRYPT_KEY=SECRET)
        return subprocess.run([sys.executable, "-m", "hotnews.cli", "--config", str(config or self.config),
                               "agent", action], input=raw if raw is not None else json.dumps(value),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
                              timeout=10)

    def success(self, process):
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        value = json.loads(process.stdout)
        self.assertIsInstance(value, dict)
        return value

    def validation_failure(self, process):
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"error": "validation_error"})
        self.assertTrue(process.stderr.strip())
        self.assertNotIn(SECRET, process.stderr)
        self.assertNotIn(SECRET, process.stdout)
        self.assertNotIn("Traceback", process.stderr)

    def rows(self, table):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM %s ORDER BY rowid" % table).fetchall()

    def test_claim_apply_and_duplicate_replay_use_json_without_feishu_secrets(self):
        self.event()
        claimed = self.success(self.invoke("claim-events", {"owner": "codex-run"}))["events"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual((claimed[0]["event_id"], claimed[0]["text"], claimed[0]["lease_owner"]),
                         ("event-1", "订阅 AI", "codex-run"))
        lease_span = (datetime.fromisoformat(claimed[0]["lease_until"].replace("Z", "+00:00"))
                      - datetime.now(timezone.utc))
        self.assertGreater(lease_span.total_seconds(), 890)
        first = self.success(self.invoke("apply-intent", {"event_id": "event-1", "owner": "codex-run", "intent": CREATE}))
        subscription = first["result"]["subscription"]
        self.assertEqual(subscription["keywords"], ["AI"])
        self.assertEqual(subscription["search_terms"], ["AI agents"])
        self.assertEqual(subscription["schedule"], {"kind": "daily", "daily_at": "09:00", "interval_minutes": None})
        replayed = self.success(self.invoke("apply-intent", {"event_id": "event-1", "owner": "another-run", "intent": CREATE}))
        self.assertEqual(replayed, first)
        self.assertEqual(len(self.rows("subscriptions")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_default_claim_limit_leaves_extra_events_pending(self):
        for number in range(1, 23):
            self.event(number)
        claimed = self.success(self.invoke("claim-events", {"owner": "codex-run"}))["events"]
        self.assertEqual(len(claimed), 20)
        self.assertEqual(sum(row["status"] == "pending" for row in self.rows("inbound_events")), 2)

    def test_custom_claim_limit_and_empty_queue(self):
        self.event()
        self.event(2)
        self.assertEqual(len(self.success(self.invoke("claim-events", {"owner": "a", "limit": 1}))["events"]), 1)
        self.assertEqual(len(self.success(self.invoke("claim-events", {"owner": "b", "limit": 1}))["events"]), 1)
        self.assertEqual(self.success(self.invoke("claim-events", {"owner": "c"})), {"events": []})

    def test_invalid_envelopes_and_schemas_exit_two_without_leaking_values(self):
        inputs = [("claim-events", {"owner": True}), ("claim-events", {"owner": " "}),
                  ("claim-events", {"owner": "worker", "limit": True}),
                  ("claim-events", {"owner": "worker", "limit": 0}),
                  ("claim-events", {"owner": "worker", "limit": 21}),
                  ("claim-events", {"owner": "worker", SECRET: SECRET}),
                  ("apply-intent", {"event_id": "event-1", "owner": "worker", "intent": dict(CREATE, topic=False)}),
                  ("apply-intent", {"event_id": "event-1", "owner": "worker", "intent": dict(CREATE, sql=SECRET)}),
                  ("apply-intent", {"event_id": "event-1", "owner": "worker"}),
                  ("fail-event", {"event_id": "event-1", "owner": "worker", "error": []})]
        for action, value in inputs:
            with self.subTest(action=action, value=value):
                self.validation_failure(self.invoke(action, value))
        self.assertEqual(self.rows("subscriptions"), [])

    def test_malformed_duplicate_nonfinite_and_deep_json_are_rejected(self):
        samples = ["", "{", "[]", '{"owner":"a"} {"owner":"b"}',
                   '{"owner":"first","owner":"second"}', '{"owner":"worker","limit":NaN}',
                   '{"owner":"worker","limit":Infinity}', '{"owner":' + "[" * 1500 + "0" + "]" * 1500 + "}"]
        for raw in samples:
            with self.subTest(raw=raw[:80]):
                self.validation_failure(self.invoke("claim-events", raw=raw))

    def test_wrong_owner_pending_and_expired_events_exit_two_without_side_effects(self):
        self.event()
        self.validation_failure(self.invoke("apply-intent", {"event_id": "event-1", "owner": "worker", "intent": CREATE}))
        self.events.claim_pending("worker", 1, datetime.now(timezone.utc) - timedelta(minutes=16), 900)
        for owner in ("worker", "stranger"):
            self.validation_failure(self.invoke("apply-intent", {"event_id": "event-1", "owner": owner, "intent": CREATE}))
        self.assertEqual(self.rows("subscriptions"), [])

    def test_group_commands_can_be_applied_by_any_member(self):
        self.event()
        self.success(self.invoke("claim-events", {"owner": "worker"}))
        self.success(self.invoke("apply-intent", {"event_id": "event-1", "owner": "worker", "intent": CREATE}))
        self.event(2, sender_id="another-member")
        self.success(self.invoke("claim-events", {"owner": "worker"}))
        value = self.success(self.invoke("apply-intent", {"event_id": "event-2", "owner": "worker",
                                                          "intent": {"action": "cancel_subscription", "subscription_number": 1}}))
        self.assertEqual(value["result"]["subscription"]["state"], "cancelled")

    def test_invalid_business_targets_reply_complete_and_replay_without_mutation(self):
        self.event(chat_id="chat-b")
        self.success(self.invoke("claim-events", {"owner": "worker"}))
        self.success(self.invoke("apply-intent", {"event_id": "event-1", "owner": "worker", "intent": CREATE}))
        before = [dict(row) for row in self.rows("subscriptions")]
        for index, (action, number) in enumerate((
                ("cancel_subscription", 999), ("run_subscription_now", 999),
                ("cancel_subscription", 1), ("run_subscription_now", 1)), start=2):
            with self.subTest(action=action, number=number):
                self.event(index, chat_id="chat-a")
                self.success(self.invoke("claim-events", {"owner": "worker"}))
                value = {"event_id": "event-%d" % index, "owner": "worker",
                         "intent": {"action": action, "subscription_number": number}}
                count = len(self.rows("outbox"))
                first = self.success(self.invoke("apply-intent", value))
                self.assertIsNone(first["result"]["subscription"])
                self.assertIn("查看订阅", first["result"]["message"])
                event = self.events.get(value["event_id"])
                self.assertEqual(event.status, "completed")
                self.assertIsNone(event.lease_owner)
                queued = self.rows("outbox")
                self.assertEqual(len(queued), count + 1)
                self.assertEqual((queued[-1]["chat_id"], queued[-1]["idempotency_key"]),
                                 ("chat-a", "event:%s:result" % value["event_id"]))
                self.assertEqual(json.loads(queued[-1]["content_json"])["elements"][0]["text"]["content"],
                                 first["result"]["message"])
                replayed = self.success(self.invoke("apply-intent", dict(value, owner="later-worker")))
                self.assertEqual(replayed, first)
                self.assertEqual(len(self.rows("outbox")), count + 1)
                self.assertEqual([dict(row) for row in self.rows("subscriptions")], before)
                self.assertEqual(self.rows("subscription_runs"), [])

    def test_fail_event_is_owner_checked_and_stores_safe_diagnostic(self):
        self.event()
        self.success(self.invoke("claim-events", {"owner": "worker"}))
        self.validation_failure(self.invoke("fail-event", {"event_id": "event-1", "owner": "stranger", "error": SECRET}))
        value = self.success(self.invoke("fail-event", {"event_id": "event-1", "owner": "worker", "error": SECRET}))
        self.assertEqual(value, {"event_id": "event-1", "status": "failed"})
        row = self.rows("inbound_events")[0]
        self.assertEqual(row["status"], "failed")
        self.assertNotIn(SECRET, row["last_error"])
        self.assertIsNone(row["lease_owner"])
        self.assertEqual(self.success(self.invoke("claim-events", {"owner": "other"}))["events"], [])

    def test_defer_event_strict_json_releases_for_the_next_tick_without_reply(self):
        self.event()
        self.success(self.invoke("claim-events", {"owner": "worker"}))
        for value in ({"event_id": "event-1", "owner": "stranger"},
                      {"event_id": "event-1", "owner": "worker", "error": SECRET},
                      {"event_id": "event-1", "owner": True}, {"owner": "worker"}):
            self.validation_failure(self.invoke("defer-event", value))
        result = self.success(self.invoke("defer-event", {"event_id": "event-1", "owner": "worker"}))
        self.assertEqual(result, {"event_id": "event-1", "status": "pending"})
        self.assertEqual(self.rows("outbox"), [])
        claimed = self.success(self.invoke("claim-events", {"owner": "next-tick"}))["events"][0]
        self.assertEqual((claimed["event_id"], claimed["attempts"]), ("event-1", 2))
        with self.database.connect() as connection:
            connection.execute("UPDATE inbound_events SET lease_until = ?", ("2000-01-01T00:00:00Z",))
        self.validation_failure(self.invoke("defer-event", {"event_id": "event-1", "owner": "next-tick"}))

    def test_invalid_config_errors_are_sanitized(self):
        self.config.write_text(json.dumps({"database_path": SECRET, "timezone": SECRET}), encoding="utf-8")
        self.validation_failure(self.invoke("claim-events", {"owner": "worker"}))
        self.validation_failure(self.invoke("claim-events", {"owner": "worker"}, config=self.path / SECRET))

    def test_unrecognized_command_arguments_do_not_echo_sensitive_values(self):
        process = subprocess.run([sys.executable, "-m", "hotnews.cli", "--config", str(self.config),
                                  "agent", "claim-events", "--" + SECRET], input='{"owner":"worker"}',
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=dict(os.environ, PYTHONPATH="src"), timeout=10)
        self.validation_failure(process)


if __name__ == "__main__":
    unittest.main()
