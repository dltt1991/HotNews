"""Group-scoped subscriptions, optimistic mutations and term-refresh leases."""

from datetime import datetime, timedelta, timezone
import json
from typing import List, Optional
from uuid import uuid4

from hotnews.domain import Intent, LeaseConflict, Schedule, Subscription, ValidationError, VersionConflict
from hotnews.storage.database import Database
from hotnews.storage.events import _claim_times, _datetime, _utc_text


SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def next_run(schedule: Schedule, now: datetime) -> Optional[datetime]:
    """Return a strictly future UTC occurrence, skipping missed periods."""
    _utc_text(now)
    if not isinstance(schedule, Schedule):
        raise ValidationError("schedule must be a Schedule")
    current = now.astimezone(timezone.utc)
    if schedule.kind == "manual":
        return None
    if schedule.kind == "interval":
        return current + timedelta(minutes=schedule.interval_minutes)
    local = current.astimezone(SHANGHAI)
    hour, minute = (int(part) for part in schedule.daily_at.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _now(value: Optional[datetime]) -> datetime:
    return datetime.now(timezone.utc) if value is None else value


def _strings(values, name: str):
    if not isinstance(values, (list, tuple)):
        raise ValidationError("%s must be a list or tuple" % name)
    return tuple(values)


def _validate(topic, keywords, terms) -> None:
    Intent(action="create_subscription", topic=topic, keywords=keywords, search_terms=terms)


def _subscription(row) -> Subscription:
    return Subscription(
        id=row["id"], chat_id=row["chat_id"], display_number=row["display_number"],
        creator_id=row["creator_id"], topic=row["topic"],
        keywords=tuple(json.loads(row["keywords_json"])),
        search_terms=tuple(json.loads(row["search_terms_json"])),
        schedule=Schedule(row["schedule_kind"], daily_at=row["daily_at"],
                          interval_minutes=row["interval_minutes"]),
        state=row["state"], next_run_at=_datetime(row["next_run_at"]), version=row["version"],
        created_at=_datetime(row["created_at"]), updated_at=_datetime(row["updated_at"]),
        cancelled_at=_datetime(row["cancelled_at"]), last_success_at=_datetime(row["last_success_at"]),
        consecutive_failures=row["consecutive_failures"], alerted=bool(row["alerted"]),
    )


class SubscriptionRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, chat_id: str, creator_id: str, topic: str, keywords,
               search_terms=(), schedule: Optional[Schedule] = None,
               now: Optional[datetime] = None) -> Subscription:
        """Allocate the next chat number and insert in a single write transaction."""
        for name, value in (("chat_id", chat_id), ("creator_id", creator_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("%s must be a non-empty string" % name)
        keywords, terms = _strings(keywords, "keywords"), _strings(search_terms, "search_terms")
        _validate(topic, keywords, terms)
        schedule = Schedule("daily", daily_at="09:00") if schedule is None else schedule
        instant = _now(now)
        timestamp = _utc_text(instant)
        due = next_run(schedule, instant)
        subscription_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO chats (chat_id) VALUES (?) ON CONFLICT DO NOTHING", (chat_id,))
            number = connection.execute("SELECT next_display_number FROM chats WHERE chat_id = ?",
                                        (chat_id,)).fetchone()[0]
            connection.execute("UPDATE chats SET next_display_number = next_display_number + 1 WHERE chat_id = ?",
                               (chat_id,))
            connection.execute(
                "INSERT INTO subscriptions (id, chat_id, display_number, creator_id, topic, keywords_json, "
                "search_terms_json, schedule_kind, daily_at, interval_minutes, next_run_at, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subscription_id, chat_id, number, creator_id, topic, json.dumps(keywords), json.dumps(terms),
                 schedule.kind, schedule.daily_at, schedule.interval_minutes,
                 _utc_text(due) if due is not None else None,
                 "ready" if terms else "search_terms_pending", timestamp, timestamp),
            )
            return _subscription(connection.execute("SELECT * FROM subscriptions WHERE id = ?",
                                                    (subscription_id,)).fetchone())

    def list(self, chat_id: Optional[str] = None, include_cancelled: bool = False) -> List[Subscription]:
        conditions, parameters = [], []
        if chat_id is not None:
            conditions.append("chat_id = ?")
            parameters.append(chat_id)
        if not include_cancelled:
            conditions.append("state != 'cancelled'")
        query = "SELECT * FROM subscriptions"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        with self.database.connect() as connection:
            return [_subscription(row) for row in connection.execute(
                query + " ORDER BY chat_id, display_number", parameters).fetchall()]

    def get(self, id: str) -> Optional[Subscription]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM subscriptions WHERE id = ?", (id,)).fetchone()
            return _subscription(row) if row is not None else None

    @staticmethod
    def _current(connection, id: str, expected_version: int):
        row = connection.execute("SELECT * FROM subscriptions WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise KeyError(id)
        if (not isinstance(expected_version, int) or isinstance(expected_version, bool)
                or row["version"] != expected_version):
            raise VersionConflict("subscription version has changed")
        if row["state"] == "cancelled":
            raise ValidationError("subscription is cancelled")
        return row

    @staticmethod
    def _save(connection, id: str, timestamp: str, **fields) -> Subscription:
        fields.update(updated_at=timestamp, lease_owner=None, lease_until=None)
        assignments = [name + " = ?" for name in fields]
        connection.execute("UPDATE subscriptions SET " + ", ".join(assignments) +
                           ", version = version + 1 WHERE id = ?", tuple(fields.values()) + (id,))
        return _subscription(connection.execute("SELECT * FROM subscriptions WHERE id = ?", (id,)).fetchone())

    def update(self, id: str, expected_version: int, *, topic: Optional[str] = None,
               keywords=None, schedule: Optional[Schedule] = None,
               now: Optional[datetime] = None) -> Subscription:
        """Recompute the next time; changed keywords invalidate expansions."""
        instant = _now(now)
        timestamp = _utc_text(instant)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current(connection, id, expected_version)
            current = _subscription(row)
            topic = current.topic if topic is None else topic
            keywords = current.keywords if keywords is None else _strings(keywords, "keywords")
            terms = () if keywords != current.keywords else current.search_terms
            _validate(topic, keywords, terms)
            schedule = current.schedule if schedule is None else schedule
            due = next_run(schedule, instant)
            state = "paused" if current.state == "paused" else ("ready" if terms else "search_terms_pending")
            return self._save(connection, id, timestamp, topic=topic, keywords_json=json.dumps(keywords),
                              search_terms_json=json.dumps(terms), schedule_kind=schedule.kind,
                              daily_at=schedule.daily_at, interval_minutes=schedule.interval_minutes,
                              next_run_at=_utc_text(due) if due is not None else None, state=state)

    def pause(self, id: str, expected_version: int, now: Optional[datetime] = None) -> Subscription:
        timestamp = _utc_text(_now(now))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._current(connection, id, expected_version)
            return self._save(connection, id, timestamp, state="paused")

    def resume(self, id: str, expected_version: int, now: Optional[datetime] = None) -> Subscription:
        instant = _now(now)
        timestamp = _utc_text(instant)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current(connection, id, expected_version)
            current = _subscription(row)
            due = next_run(current.schedule, instant)
            return self._save(connection, id, timestamp,
                              state="ready" if current.search_terms else "search_terms_pending",
                              next_run_at=_utc_text(due) if due is not None else None)

    def cancel(self, id: str, expected_version: int, now: Optional[datetime] = None) -> Subscription:
        timestamp = _utc_text(_now(now))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._current(connection, id, expected_version)
            return self._save(connection, id, timestamp, state="cancelled", cancelled_at=timestamp)

    def claim_pending_terms(self, owner: str, limit: int, now: datetime,
                            lease_seconds: int) -> List[Subscription]:
        """Claim pending expansions, including paused rows whose terms were cleared."""
        timestamp, deadline = _claim_times(owner, limit, now, lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM subscriptions WHERE "
                "(state = 'search_terms_pending' OR (state = 'paused' AND search_terms_json = '[]')) "
                "AND (lease_owner IS NULL OR lease_until <= ?) ORDER BY created_at, rowid LIMIT ?",
                (timestamp, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                connection.execute(
                    "UPDATE subscriptions SET lease_owner = ?, lease_until = ?, updated_at = ?, "
                    "version = version + 1 WHERE id = ?", (owner, deadline, timestamp, row["id"]),
                )
                claimed.append(_subscription(connection.execute("SELECT * FROM subscriptions WHERE id = ?",
                                                                (row["id"],)).fetchone()))
            return claimed

    def _owned_terms(self, connection, id: str, owner: str, expected_version: int):
        row = self._current(connection, id, expected_version)
        if (not isinstance(owner, str) or not owner.strip() or row["lease_owner"] != owner
                or row["state"] not in ("search_terms_pending", "paused")):
            raise LeaseConflict("term refresh is not leased by this owner")
        return row

    def complete_search_terms(self, id: str, owner: str, expected_version: int,
                              terms: List[str], now: Optional[datetime] = None) -> Subscription:
        timestamp = _utc_text(_now(now))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_terms(connection, id, owner, expected_version)
            terms = _strings(terms, "search_terms")
            if not terms:
                raise ValidationError("search_terms must not be empty")
            _validate(row["topic"], tuple(json.loads(row["keywords_json"])), terms)
            return self._save(connection, id, timestamp, search_terms_json=json.dumps(terms),
                              state="paused" if row["state"] == "paused" else "ready")

    def fail_search_terms(self, id: str, owner: str, expected_version: int,
                          error: str, now: Optional[datetime] = None) -> Subscription:
        """Release failed work for another attempt; callers retain diagnostic errors."""
        timestamp = _utc_text(_now(now))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_terms(connection, id, owner, expected_version)
            return self._save(connection, id, timestamp)

    def request_manual_run(self, id: str, expected_version: int,
                           now: Optional[datetime] = None) -> str:
        """Queue one manual run while preserving pause and the regular schedule."""
        timestamp = _utc_text(_now(now))
        run_id = str(uuid4())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._current(connection, id, expected_version)
            if row["state"] not in ("ready", "paused") or not json.loads(row["search_terms_json"]):
                raise ValidationError("manual run requires available search terms")
            connection.execute(
                "INSERT INTO subscription_runs (id, subscription_id, trigger, created_at) VALUES (?, ?, 'manual', ?)",
                (run_id, id, timestamp),
            )
            self._save(connection, id, timestamp)
        return run_id
