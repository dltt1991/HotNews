import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from hotnews.config import AppConfig, load_config, load_feishu_config
from hotnews.domain import Intent, NewsResult, Schedule, Subscription, ValidationError


class ConfigDomainTests(unittest.TestCase):
    def write_config(self, value):
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        with handle:
            json.dump(value, handle)
        return handle.name

    def test_secrets_come_only_from_environment(self):
        path = self.write_config({"feishu": {"app_id": "file-id", "app_secret": "file-secret"}})
        config = load_config(path)
        self.assertFalse(hasattr(config, "feishu"))
        feishu = load_feishu_config({
            "FEISHU_APP_ID": "env-id",
            "FEISHU_APP_SECRET": "env-secret",
            "FEISHU_WS_PROXY": "http://proxy-user:proxy-password@127.0.0.1:7890",
        })
        self.assertEqual(feishu.app_id, "env-id")
        self.assertEqual(feishu.app_secret, "env-secret")
        self.assertEqual(feishu.ws_proxy, "http://proxy-user:proxy-password@127.0.0.1:7890")
        self.assertNotIn("env-secret", repr(feishu))
        self.assertNotIn("proxy-password", repr(feishu))

    def test_missing_required_secret_is_rejected_by_runtime_loader(self):
        with self.assertRaises(ValidationError):
            load_feishu_config({})
        with self.assertRaises(ValidationError):
            load_feishu_config({"FEISHU_APP_ID": "app"})

    def test_callback_secrets_are_not_required_for_long_connection(self):
        feishu = load_feishu_config({
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
        })
        self.assertEqual((feishu.app_id, feishu.app_secret, feishu.ws_proxy),
                         ("app", "secret", None))

    def test_agent_config_load_does_not_read_secrets(self):
        path = self.write_config({"database_path": "state.sqlite"})
        config = load_config(path)
        self.assertEqual(config.database_path, "state.sqlite")
        self.assertFalse(hasattr(config, "app_secret"))
        self.assertFalse(hasattr(config, "verification_token"))

    def test_default_ports_timezone_and_operational_limits(self):
        self.assertEqual(AppConfig().callback.max_body_bytes, 1024 * 1024)
        config = load_config(self.write_config({}))
        self.assertEqual((config.callback.host, config.callback.port), ("127.0.0.1", 8080))
        self.assertEqual((config.admin.host, config.admin.port), ("127.0.0.1", 8081))
        self.assertEqual(config.timezone, "Asia/Shanghai")
        self.assertEqual(config.max_results, 10)
        self.assertEqual(config.callback.max_body_bytes, 1024 * 1024)
        self.assertEqual(config.worker.max_queued_events, 20)
        self.assertEqual(config.worker.max_due_subscriptions, 3)
        self.assertEqual(config.worker.lease_seconds, 15 * 60)
        self.assertEqual(config.worker.soft_budget_seconds, 4 * 60)
        self.assertEqual(config.worker.outbox_max_attempts, 5)
        self.assertEqual(config.worker.outbox_backoff_seconds, 10)
        self.assertEqual(config.worker.outbox_backoff_max_seconds, 5 * 60)
        self.assertEqual(load_config(self.write_config({"max_results": 7})).max_results, 7)
        with self.assertRaises(ValidationError):
            load_config(self.write_config({"max_results": 11}))

    def make_subscription(self, topic="AI", keywords=("AI",)):
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        return Subscription("id", "chat", 1, "creator", topic, keywords, (),
                            Schedule("daily", daily_at="09:00"), "ready", None, 1,
                            now, now)

    def test_topic_and_keyword_limits(self):
        self.assertEqual(len(Intent("create_subscription", topic="x" * 200,
                                    keywords=("k" * 80,)).topic), 200)
        with self.assertRaises(ValidationError):
            Intent("create_subscription", topic="x" * 201, keywords=("AI",))
        with self.assertRaises(ValidationError):
            Intent("create_subscription", topic="AI", keywords=())
        with self.assertRaises(ValidationError):
            Intent("create_subscription", topic="AI", keywords=("x" * 81,))
        with self.assertRaises(ValidationError):
            Intent("create_subscription", topic="AI", keywords=tuple("k" for _ in range(21)))
        self.assertEqual(len(self.make_subscription(topic="x" * 200).topic), 200)
        with self.assertRaises(ValidationError):
            self.make_subscription(topic="x" * 201)
        with self.assertRaises(ValidationError):
            self.make_subscription(keywords=())
        with self.assertRaises(ValidationError):
            self.make_subscription(keywords=("x" * 81,))
        with self.assertRaises(ValidationError):
            self.make_subscription(keywords=tuple("k" for _ in range(21)))

    def test_daily_schedule_requires_hh_mm(self):
        self.assertEqual(Schedule("daily", daily_at="09:00").daily_at, "09:00")
        for value in ("9:00", "24:00", "12:60", "noon"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Schedule("daily", daily_at=value)

    def test_interval_minimum_is_five_minutes(self):
        self.assertEqual(Schedule("interval", interval_minutes=5).interval_minutes, 5)
        with self.assertRaises(ValidationError):
            Schedule("interval", interval_minutes=4)

    def test_news_result_requires_date_http_url_and_event_key(self):
        valid = NewsResult("title", "https://example.com/story", "Example",
                           datetime(2026, 10, 2, tzinfo=timezone.utc), "summary", "evt-1")
        self.assertEqual(valid.event_key, "evt-1")
        invalid_values = (
            ("title", "https://example.com/story", "Example", None, "summary", "evt-1"),
            ("title", "ftp://example.com/story", "Example", datetime.now(timezone.utc), "summary", "evt-1"),
            ("title", "https://example.com/story", "Example", datetime.now(timezone.utc), "summary", ""),
        )
        for args in invalid_values:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                NewsResult(*args)


if __name__ == "__main__":
    unittest.main()
