from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hotnews.feishu.intake import ACKNOWLEDGEMENT, EventIntake
from hotnews.storage.database import Database
from tests.unit.test_feishu import message_event


class FeishuIntakeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Database(str(Path(directory.name) / "intake.db"))
        self.database.migrate()
        self.intake = EventIntake(self.database, "ou_bot")

    def rows(self, table):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM %s ORDER BY rowid" % table).fetchall()

    def test_mentioned_group_text_is_persisted_with_one_ack(self):
        self.assertTrue(self.intake.handle(message_event()))
        event = self.rows("inbound_events")[0]
        self.assertEqual((event["event_id"], event["message_id"], event["raw_text"], event["text"]),
                         ("evt_1", "om_1", "@_user_1 订阅 AI，每天 9 点", "订阅 AI，每天 9 点"))
        self.assertEqual(json.loads(event["mentions_json"]),
                         message_event()["event"]["message"]["mentions"])
        outbox = self.rows("outbox")[0]
        self.assertEqual(json.loads(outbox["content_json"]), {"text": ACKNOWLEDGEMENT})
        self.assertEqual(outbox["idempotency_key"], "event:evt_1:ack")

    def test_irrelevant_or_partially_missing_event_is_acknowledged_without_rows(self):
        cases = []
        private = message_event()
        private["event"]["message"]["chat_type"] = "p2p"
        cases.append(private)
        no_mention = message_event()
        no_mention["event"]["message"]["mentions"] = []
        cases.append(no_mention)
        cases.extend(({}, {"header": {}}, {"header": {"event_type": "im.message.receive_v1"}}))
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertTrue(self.intake.handle(payload))
        self.assertEqual(self.rows("inbound_events"), [])
        self.assertEqual(self.rows("outbox"), [])

    def test_duplicate_event_or_message_creates_only_one_event_and_ack(self):
        first = message_event()
        same_message = message_event()
        same_message["header"]["event_id"] = "evt_retry"
        same_event = message_event()
        same_event["event"]["message"]["message_id"] = "om_retry"
        for payload in (first, first, same_message, same_event):
            self.assertTrue(self.intake.handle(payload))
        self.assertEqual(len(self.rows("inbound_events")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_concurrent_duplicate_delivery_is_idempotent(self):
        def receive(_):
            return EventIntake(Database(self.database.path), "ou_bot").handle(message_event())

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(receive, range(2))), [True, True])
        self.assertEqual(len(self.rows("inbound_events")), 1)
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_failed_ack_insert_rolls_back_event_and_raises_for_redelivery(self):
        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER fail_ack BEFORE INSERT ON outbox "
                               "BEGIN SELECT RAISE(ABORT, 'storage unavailable'); END")
        with self.assertRaises(sqlite3.Error):
            self.intake.handle(message_event())
        self.assertEqual(self.rows("inbound_events"), [])
        self.assertEqual(self.rows("outbox"), [])


if __name__ == "__main__":
    unittest.main()
