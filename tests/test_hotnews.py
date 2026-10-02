"""One full long-connection event → research → confirmed send acceptance flow."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from hotnews.commands.schema import parse_intent
from hotnews.commands.service import CommandService
from hotnews.config import AppConfig
from hotnews.domain import NewsResult
from hotnews.feishu.client import FeishuClient
from hotnews.feishu.intake import EventIntake
from hotnews.runtime import OutboxWorker
from hotnews.storage.database import Database
from hotnews.storage.events import EventRepository
from hotnews.storage.runs import RunRepository
from hotnews.storage.subscriptions import SubscriptionRepository
from tests.integration.test_runtime import Transport
from tests.unit.test_feishu import config, message_event


class HotNewsAcceptanceTests(unittest.TestCase):
    def test_group_pipeline_multi_subscription_shared_manual_run_dedupe_and_soft_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            app_config = AppConfig(database_path=str(Path(directory) / "pipeline.db"))
            database = Database(app_config.database_path)
            database.migrate()
            intake = EventIntake(database, "ou_bot")
            events = EventRepository(database)
            subscriptions = SubscriptionRepository(database)
            runs = RunRepository(database)
            now = datetime.now(timezone.utc)
            commands = CommandService(database, clock=lambda: now)

            def command(number, intent, sender="ou_member"):
                payload = message_event()
                payload["header"]["event_id"] = "evt_%d" % number
                payload["event"]["message"]["message_id"] = "om_%d" % number
                payload["event"]["sender"]["sender_id"]["open_id"] = sender
                self.assertTrue(intake.handle(payload))
                self.assertTrue(intake.handle(payload))
                claimed = events.claim_pending("research", 20, now, 900)
                self.assertEqual(len(claimed), 1)
                return commands.apply(claimed[0].event_id, "research", parse_intent(intent))

            first = command(1, {"action": "create_subscription", "topic": "AI", "keywords": ["AI"],
                                "search_terms": ["AI", "artificial intelligence"]}).subscription
            ignored = message_event()
            ignored["header"]["event_id"] = "evt_unmentioned"
            ignored["event"]["message"].update(message_id="om_unmentioned", mentions=[])
            self.assertTrue(intake.handle(ignored))
            self.assertEqual(len(events.claim_pending("ignored-check", 20, now, 900)), 0)
            second = command(2, {"action": "create_subscription", "topic": "能源", "keywords": ["能源"],
                                 "search_terms": ["能源", "energy"],
                                 "schedule": {"kind": "interval", "interval_minutes": 120}}).subscription
            self.assertEqual((first.display_number, second.display_number), (1, 2))
            command(3, {"action": "run_subscription_now", "subscription_number": 1}, sender="ou_other_member")
            batch = runs.claim_due("research", 3, now, 900)
            run = next(item for item in batch if item.subscription_id == first.id)
            self.assertEqual(run.trigger, "manual")
            news = NewsResult("官方新模型", "https://official.example/model", "官方",
                              now - timedelta(hours=25), "机构公布了新模型。支持复杂任务。", "official:model:v1")
            runs.complete(run.id, "research", [news], now=now, search_window_days=7)
            self.assertEqual(runs.history(first.id), [])
            transport = Transport()
            worker = OutboxWorker(app_config, FeishuClient(config(), transport), clock=lambda: 0)
            worker.run_once(now)
            self.assertEqual(len(transport.messages), 7)
            self.assertEqual(runs.history(first.id)[0]["event_key"], "official:model:v1")
            card = json.loads(transport.messages[-1]["content"])
            self.assertIn("历史补充", card["elements"][0]["text"]["content"])
            self.assertEqual(worker.run_once(now), 0)
            command(4, {"action": "cancel_subscription", "subscription_number": 1})
            self.assertEqual(subscriptions.get(first.id).state, "cancelled")
            self.assertEqual(subscriptions.get(second.id).state, "ready")
            recreated = command(5, {"action": "create_subscription", "topic": "AI", "keywords": ["AI"],
                                    "search_terms": ["AI", "artificial intelligence"]}).subscription
            self.assertEqual(recreated.display_number, 3)
            self.assertEqual(runs.history(recreated.id)[0]["event_key"], "official:model:v1")
            database = Database(app_config.database_path)
            database.migrate()
            self.assertEqual(len(SubscriptionRepository(database).list("oc_group")), 2)
            self.assertEqual(RunRepository(database).history(recreated.id)[0]["event_key"], "official:model:v1")
