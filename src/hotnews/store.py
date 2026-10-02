import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DeliveryStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                "item_id TEXT NOT NULL, subscription TEXT NOT NULL, channel TEXT NOT NULL, "
                "delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (item_id, subscription, channel))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS dynamic_subscriptions ("
                "platform TEXT NOT NULL, conversation_id TEXT NOT NULL, rule_json TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (platform, conversation_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS processed_events ("
                "event_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def was_delivered(self, item_id: str, subscription: str, channel: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM deliveries WHERE item_id=? AND subscription=? AND channel=?",
                (item_id, subscription, channel),
            ).fetchone()
        return row is not None

    def mark_delivered(self, item_id: str, subscription: str, channel: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO deliveries(item_id, subscription, channel) VALUES (?, ?, ?)",
                (item_id, subscription, channel),
            )

    def save_subscription(self, platform: str, conversation_id: str, rule: dict) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO dynamic_subscriptions(platform, conversation_id, rule_json, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (platform, conversation_id, json.dumps(rule, ensure_ascii=False)),
            )

    def delete_subscription(self, platform: str, conversation_id: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM dynamic_subscriptions WHERE platform=? AND conversation_id=?",
                (platform, conversation_id),
            )
        return cursor.rowcount > 0

    def list_subscriptions(self):
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT platform, conversation_id, rule_json FROM dynamic_subscriptions"
            ).fetchall()
        return [(platform, conversation_id, json.loads(rule)) for platform, conversation_id, rule in rows]

    def get_subscription(self, platform: str, conversation_id: str):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT rule_json FROM dynamic_subscriptions WHERE platform=? AND conversation_id=?",
                (platform, conversation_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def claim_event(self, event_id: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO processed_events(event_id) VALUES (?)", (event_id,)
            )
        return cursor.rowcount > 0
