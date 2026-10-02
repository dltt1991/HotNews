"""Durable inbound event queue and global agent coordination leases."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import List, Mapping, Optional, Union
from uuid import uuid4

from hotnews.domain import InboundEvent, LeaseConflict, NormalizedEvent
from hotnews.storage.database import Database


def _utc_text(value: datetime) -> str:
    """Use fixed-width RFC3339 UTC text so SQL ordering is chronological."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None


def _lease_times(owner: str, now: datetime, lease_seconds: int):
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("owner must be a non-empty string")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    timestamp = _utc_text(now)
    return timestamp, _utc_text(now + timedelta(seconds=lease_seconds))


def _claim_times(owner: str, limit: int, now: datetime, lease_seconds: int):
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    return _lease_times(owner, now, lease_seconds)


def _inbound_event(row) -> InboundEvent:
    return InboundEvent(
        id=row["id"], event_id=row["event_id"], message_id=row["message_id"],
        chat_id=row["chat_id"], sender_id=row["sender_id"], text=row["text"],
        received_at=_datetime(row["received_at"]), status=row["status"],
        lease_owner=row["lease_owner"], lease_until=_datetime(row["lease_until"]),
        attempts=row["attempts"], last_error=row["last_error"],
    )


class EventRepository:
    """Queue events deduplicated independently by external event and message ID."""

    def __init__(self, database: Database):
        self.database = database

    def insert(self, event: Union[NormalizedEvent, Mapping[str, object]],
               connection: Optional[sqlite3.Connection] = None) -> bool:
        """Insert an event, optionally participating in the caller's transaction."""
        if connection is None:
            with self.database.connect() as owned_connection:
                return self.insert(event, connection=owned_connection)
        if isinstance(event, NormalizedEvent):
            values = vars(event)
        else:
            values = event
        received_at = _utc_text(values["received_at"])
        mentions = values.get("mentions", [])
        if not isinstance(mentions, list):
            raise TypeError("event mentions must be a list")
        mentions_json = json.dumps(mentions, ensure_ascii=True, sort_keys=True, allow_nan=False,
                                   separators=(",", ":"))
        if json.loads(mentions_json) != mentions:
            raise ValueError("event mentions must round-trip through JSON without changes")
        connection.execute("INSERT INTO chats (chat_id) VALUES (?) ON CONFLICT DO NOTHING",
                           (values["chat_id"],))
        cursor = connection.execute(
            "INSERT INTO inbound_events "
            "(id, event_id, message_id, chat_id, sender_id, raw_text, text, mentions_json, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (str(uuid4()), values["event_id"], values["message_id"], values["chat_id"],
             values["sender_id"], values.get("raw_text", values["text"]), values["text"],
             mentions_json, received_at),
        )
        return cursor.rowcount == 1

    def claim_pending(self, owner: str, limit: int, now: datetime,
                      lease_seconds: int) -> List[InboundEvent]:
        """Claim FIFO pending/expired work and count each processing attempt."""
        timestamp, deadline = _claim_times(owner, limit, now, lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM inbound_events WHERE status = 'pending' "
                "OR (status = 'leased' AND lease_until <= ?) "
                "ORDER BY received_at, rowid LIMIT ?", (timestamp, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                connection.execute(
                    "UPDATE inbound_events SET status = 'leased', lease_owner = ?, "
                    "lease_until = ?, attempts = attempts + 1 WHERE id = ?",
                    (owner, deadline, row["id"]),
                )
                claimed.append(_inbound_event(connection.execute(
                    "SELECT * FROM inbound_events WHERE id = ?", (row["id"],),
                ).fetchone()))
            return claimed

    def get(self, event_id: str, connection: Optional[sqlite3.Connection] = None) -> Optional[InboundEvent]:
        if connection is None:
            with self.database.connect() as owned_connection:
                return self.get(event_id, connection=owned_connection)
        row = connection.execute("SELECT * FROM inbound_events WHERE event_id = ?", (event_id,)).fetchone()
        return _inbound_event(row) if row is not None else None

    def get_result(self, event_id: str, connection: Optional[sqlite3.Connection] = None) -> Optional[str]:
        if connection is None:
            with self.database.connect() as owned_connection:
                return self.get_result(event_id, connection=owned_connection)
        row = connection.execute("SELECT result_summary FROM inbound_events WHERE event_id = ?", (event_id,)).fetchone()
        return row["result_summary"] if row is not None else None

    def complete(self, event_id: str, owner: str, result: str,
                 connection: Optional[sqlite3.Connection] = None,
                 now: Optional[datetime] = None) -> None:
        """Complete by external Feishu event_id, only while owned and leased."""
        if connection is None:
            with self.database.connect() as owned_connection:
                return self.complete(event_id, owner, result, connection=owned_connection, now=now)
        condition = " AND lease_until > ?" if now is not None else ""
        parameters = (result, event_id, owner) + ((_utc_text(now),) if now is not None else ())
        cursor = connection.execute(
            "UPDATE inbound_events SET status = 'completed', result_summary = ?, "
            "last_error = NULL, lease_owner = NULL, lease_until = NULL "
            "WHERE event_id = ? AND status = 'leased' AND lease_owner = ?" + condition, parameters)
        if cursor.rowcount != 1:
            raise LeaseConflict("event is not leased by this owner")

    def fail(self, event_id: str, owner: str, error: str,
             connection: Optional[sqlite3.Connection] = None,
             now: Optional[datetime] = None) -> None:
        """Permanently fail an owned event, identified by external event_id."""
        if connection is None:
            with self.database.connect() as owned_connection:
                return self.fail(event_id, owner, error, connection=owned_connection, now=now)
        condition = " AND lease_until > ?" if now is not None else ""
        parameters = (error, event_id, owner) + ((_utc_text(now),) if now is not None else ())
        cursor = connection.execute(
            "UPDATE inbound_events SET status = 'failed', last_error = ?, "
            "lease_owner = NULL, lease_until = NULL "
            "WHERE event_id = ? AND status = 'leased' AND lease_owner = ?" + condition, parameters)
        if cursor.rowcount != 1:
            raise LeaseConflict("event is not leased by this owner")


class LeaseRepository:
    """Global coordination predicates: unavailable/unowned leases return False."""

    def __init__(self, database: Database):
        self.database = database

    def acquire(self, name: str, owner: str, now: datetime, lease_seconds: int) -> bool:
        """Acquire an absent/expired lease; same-owner reacquisition is idempotent."""
        timestamp, deadline = _lease_times(owner, now, lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM agent_leases WHERE name = ?", (name,)).fetchone()
            if row is not None and row["lease_until"] > timestamp:
                return row["owner"] == owner
            connection.execute(
                "INSERT INTO agent_leases (name, owner, lease_until) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, lease_until = excluded.lease_until",
                (name, owner, deadline),
            )
            return True

    def renew(self, name: str, owner: str, now: datetime, lease_seconds: int) -> bool:
        """Extend an unexpired lease owned by this caller; never revive expiry."""
        timestamp, deadline = _lease_times(owner, now, lease_seconds)
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE agent_leases SET lease_until = ? "
                "WHERE name = ? AND owner = ? AND lease_until > ?",
                (deadline, name, owner, timestamp),
            )
            return cursor.rowcount == 1

    def release(self, name: str, owner: str) -> bool:
        """Return True only when this caller actually releases its lease."""
        with self.database.connect() as connection:
            return connection.execute("DELETE FROM agent_leases WHERE name = ? AND owner = ?",
                                      (name, owner)).rowcount == 1
