"""Idempotent durable outbound queue with owner-checked delivery updates."""

from datetime import datetime, timezone
import json
from typing import List
from uuid import uuid4

from hotnews.domain import LeaseConflict, OutboxItem
from hotnews.storage.database import Database
from hotnews.storage.events import _claim_times, _datetime, _utc_text


def _outbox_item(row) -> OutboxItem:
    return OutboxItem(
        id=row["id"], chat_id=row["chat_id"], kind=row["kind"],
        content=json.loads(row["content_json"]), idempotency_key=row["idempotency_key"],
        status=row["status"], attempts=row["attempts"], created_at=_datetime(row["created_at"]),
        lease_owner=row["lease_owner"], lease_until=_datetime(row["lease_until"]),
        last_error=row["last_error"],
    )


class OutboxRepository:
    def __init__(self, database: Database):
        self.database = database

    def enqueue(self, chat_id: str, kind: str, content: dict, idempotency_key: str) -> str:
        """Return the durable ID, preserving the first payload for a business key."""
        if not isinstance(content, dict):
            raise TypeError("outbox content must be a dict")
        # Escape Unicode so even JSON strings containing lone surrogates can be
        # stored as UTF-8 SQLite text without losing their round-trip value.
        content_json = json.dumps(content, ensure_ascii=True, allow_nan=False)
        if json.loads(content_json) != content:
            raise ValueError("outbox content must round-trip through JSON without changes")
        item_id = str(uuid4())
        created_at = _utc_text(datetime.now(timezone.utc))
        with self.database.connect() as connection:
            connection.execute("INSERT INTO chats (chat_id) VALUES (?) ON CONFLICT DO NOTHING", (chat_id,))
            connection.execute(
                "INSERT INTO outbox (id, chat_id, kind, content_json, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING",
                (item_id, chat_id, kind, content_json, idempotency_key, created_at),
            )
            return connection.execute("SELECT id FROM outbox WHERE idempotency_key = ?",
                                      (idempotency_key,)).fetchone()["id"]

    def claim(self, owner: str, limit: int, now: datetime, lease_seconds: int) -> List[OutboxItem]:
        """Atomically claim FIFO due/expired rows, incrementing send attempts."""
        timestamp, deadline = _claim_times(owner, limit, now, lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM outbox WHERE "
                "(status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                "OR (status = 'leased' AND lease_until <= ?) "
                "ORDER BY created_at, rowid LIMIT ?", (timestamp, timestamp, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                connection.execute(
                    "UPDATE outbox SET status = 'leased', lease_owner = ?, lease_until = ?, "
                    "attempts = attempts + 1 WHERE id = ?", (owner, deadline, row["id"]),
                )
                claimed.append(_outbox_item(connection.execute(
                    "SELECT * FROM outbox WHERE id = ?", (row["id"],),
                ).fetchone()))
            return claimed

    def sent(self, item_id: str, owner: str, feishu_message_id: str, now: datetime) -> None:
        """Record confirmed delivery; linked run/delivery finalization is a later task."""
        sent_at = _utc_text(now)
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status = 'sent', feishu_message_id = ?, sent_at = ?, "
                "lease_owner = NULL, lease_until = NULL, next_attempt_at = NULL, last_error = NULL "
                "WHERE id = ? AND status = 'leased' AND lease_owner = ?",
                (feishu_message_id, sent_at, item_id, owner),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("outbox item is not leased by this owner")

    def retry(self, item_id: str, owner: str, error: str, next_attempt_at: datetime) -> None:
        """Schedule another send; its next claim increments attempts exactly once."""
        due = _utc_text(next_attempt_at)
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status = 'pending', next_attempt_at = ?, last_error = ?, "
                "lease_owner = NULL, lease_until = NULL "
                "WHERE id = ? AND status = 'leased' AND lease_owner = ?",
                (due, error, item_id, owner),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("outbox item is not leased by this owner")
