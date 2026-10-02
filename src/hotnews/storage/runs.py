"""Lease due work and atomically prepare deduplicated news for the outbox.

Nonempty completions stop at awaiting_delivery. The network worker owns
confirmation of the linked outbox, deliveries and run, and schedule advancement.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from hotnews.domain import LeaseConflict, NewsResult, SubscriptionRun, ValidationError, VersionConflict
from hotnews.feishu.cards import render_command_result, render_digest
from hotnews.storage.database import Database
from hotnews.storage.events import _claim_times, _datetime, _utc_text
from hotnews.storage.identity import normalized_key as _key, topic_fingerprint
from hotnews.storage.outbox import OutboxRepository
from hotnews.storage.subscriptions import _subscription, next_run


_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi"}


def canonicalize_url(value: str) -> str:
    """Strip known tracking and fragments, retaining meaningful query values."""
    try:
        if not isinstance(value, str) or not value or any(char.isspace() for char in value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError()
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        scheme, host, port = parsed.scheme.lower(), parsed.hostname.lower(), parsed.port
        if ":" in host:
            host = "[" + host + "]"
        if port is not None and (scheme, port) not in (("http", 80), ("https", 443)):
            host += ":%d" % port
        query = sorted((key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                       if not key.lower().startswith("utm_") and key.lower() not in _TRACKING)
        return urlunsplit((scheme, host, parsed.path or "/", urlencode(query), ""))
    except (ValueError, TypeError, UnicodeError):
        raise ValidationError("invalid news URL") from None


def _run(row) -> SubscriptionRun:
    return SubscriptionRun(
        id=row["id"], subscription_id=row["subscription_id"], trigger=row["trigger"],
        status=row["status"], created_at=_datetime(row["created_at"]),
        lease_owner=row["lease_owner"], lease_until=_datetime(row["lease_until"]),
        search_window_days=row["search_window_days"], started_at=_datetime(row["started_at"]),
        completed_at=_datetime(row["completed_at"]), last_error=row["last_error"],
    )


def _instant(value):
    return datetime.now(timezone.utc) if value is None else value


class RunRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _candidates(connection, timestamp, limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValidationError("limit must be a non-negative integer")
        candidates = []
        for sub in connection.execute("SELECT * FROM subscriptions WHERE state IN ('ready', 'paused')"):
            if not json.loads(sub["search_terms_json"]):
                continue
            active = connection.execute(
                "SELECT * FROM subscription_runs WHERE subscription_id = ? "
                "AND status IN ('pending', 'leased', 'awaiting_delivery') ORDER BY created_at, rowid",
                (sub["id"],),
            ).fetchall()
            if any(row["status"] == "awaiting_delivery" or
                   (row["status"] == "leased" and row["lease_until"] > timestamp) for row in active):
                continue
            scheduled = (sub["state"] == "ready" and sub["next_run_at"] is not None
                         and sub["next_run_at"] <= timestamp)
            available = [row for row in active if row["trigger"] == "manual" or scheduled]
            if available:
                candidates.append((available[0]["created_at"], sub["id"], sub, available[0]))
            elif scheduled and not active:
                candidates.append((sub["next_run_at"], sub["id"], sub, None))
        return sorted(candidates, key=lambda item: item[:2])[:limit]

    def list_due(self, now: datetime, limit: int = 3):
        """Return the same eligible subscriptions as claim_due, without writes."""
        with self.database.connect() as connection:
            return [_subscription(item[2]) for item in self._candidates(connection, _utc_text(now), limit)]

    def claim_due(self, owner: str, limit: int, now: datetime, lease_seconds: int) -> List[SubscriptionRun]:
        timestamp, deadline = _claim_times(owner, limit, now, lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claimed = []
            for _, _, sub, row in self._candidates(connection, timestamp, limit):
                if row is None:
                    run_id = str(uuid4())
                    connection.execute(
                        "INSERT INTO subscription_runs (id, subscription_id, trigger, created_at, subscription_version) "
                        "VALUES (?, ?, 'scheduled', ?, ?)", (run_id, sub["id"], timestamp, sub["version"]),
                    )
                else:
                    run_id = row["id"]
                connection.execute(
                    "UPDATE subscription_runs SET status = 'leased', lease_owner = ?, lease_until = ?, "
                    "started_at = ?, last_error = NULL, subscription_version = ? WHERE id = ?",
                    (owner, deadline, timestamp, sub["version"], run_id),
                )
                claimed.append(_run(connection.execute("SELECT * FROM subscription_runs WHERE id = ?",
                                                       (run_id,)).fetchone()))
            return claimed

    @staticmethod
    def _owned(connection, run_id, owner, timestamp, replay_statuses, check_version=True):
        row = connection.execute("SELECT * FROM subscription_runs WHERE id = ?", (run_id,)).fetchone()
        if (row is None or not isinstance(owner, str) or not owner.strip() or row["lease_owner"] != owner):
            raise LeaseConflict("run is not leased by this owner")
        if row["status"] in replay_statuses:
            return row, None
        if row["status"] != "leased" or row["lease_until"] is None or row["lease_until"] <= timestamp:
            raise LeaseConflict("run lease is not active")
        sub = connection.execute("SELECT * FROM subscriptions WHERE id = ?", (row["subscription_id"],)).fetchone()
        if check_version and sub["version"] != row["subscription_version"]:
            raise VersionConflict("subscription changed during research")
        return row, sub

    def _history(self, connection, subscription_id, include_pending=False):
        current = connection.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        if current is None:
            raise ValidationError("unknown subscription")
        identity = topic_fingerprint(json.loads(current["keywords_json"]))
        statuses = "('sent', 'pending')" if include_pending else "('sent')"
        return [dict(row) for row in connection.execute(
            "SELECT DISTINCT a.url_hash, d.event_key, a.url, a.title, a.source, a.published_at "
            "FROM deliveries d JOIN articles a ON a.id = d.article_id "
            "JOIN subscriptions s ON s.id = d.subscription_id "
            "WHERE (d.subscription_id = ? OR (s.chat_id = ? AND d.topic_fingerprint = ?)) "
            "AND d.status IN " + statuses + " ORDER BY a.published_at DESC, a.url_hash, d.event_key",
            (subscription_id, current["chat_id"], identity))]

    def history(self, subscription_id: str):
        """Only confirmed deliveries enter the model's pushed-news history."""
        with self.database.connect() as connection:
            return self._history(connection, subscription_id)

    @staticmethod
    def _results(results, now, search_window_days):
        if type(search_window_days) is not int or search_window_days not in (1, 7, 30):
            raise ValidationError("search window must be 1, 7 or 30 days")
        if not isinstance(results, (list, tuple)) or len(results) > 10:
            raise ValidationError("results must contain at most ten news items")
        validated = []
        for item in results:
            if not isinstance(item, NewsResult):
                raise ValidationError("result must be a NewsResult")
            try:
                published = _datetime(_utc_text(item.published_at))
            except ValueError:
                raise ValidationError("publication date must include a timezone") from None
            if not now - timedelta(days=search_window_days) <= published <= now:
                raise ValidationError("publication date is outside the search window")
            if len(item.references) > 2:
                raise ValidationError("at most two cross-reference URLs are allowed")
            validated.append(replace(item, url=canonicalize_url(item.url), published_at=published,
                                     event_key=_key(item.event_key),
                                     references=tuple(canonicalize_url(url) for url in item.references)))
        return validated

    def complete(self, run_id: str, owner: str, results, now: Optional[datetime] = None,
                 search_window_days: int = 30) -> SubscriptionRun:
        instant = _instant(now)
        timestamp = _utc_text(instant)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, sub = self._owned(connection, run_id, owner, timestamp, ("awaiting_delivery", "completed"))
            if sub is None:
                return _run(row)
            results = self._results(results, instant, search_window_days)
            history = self._history(connection, sub["id"], include_pending=True)
            urls = {item["url_hash"] for item in history}
            events = {_key(item["event_key"]) for item in history}
            selected = []
            for item in results:
                url_hash = hashlib.sha256(item.url.encode("utf-8")).hexdigest()
                if url_hash in urls or item.event_key in events:
                    continue
                urls.add(url_hash)
                events.add(item.event_key)
                article_id = str(uuid4())
                connection.execute(
                    "INSERT INTO articles (id, url_hash, title, url, source, published_at, summary, event_key, "
                    "references_json, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(url_hash) DO NOTHING",
                    (article_id, url_hash, item.title, item.url, item.source, _utc_text(item.published_at),
                     item.summary, item.event_key, json.dumps(item.references), timestamp),
                )
                article_id = connection.execute("SELECT id FROM articles WHERE url_hash = ?", (url_hash,)).fetchone()[0]
                selected.append((item, article_id))
            if selected:
                card = render_digest(_subscription(sub), [item for item, _ in selected], search_window_days, now=instant)
                outbox_id = OutboxRepository(self.database).enqueue(sub["chat_id"], "card", card,
                                                                  "digest:" + run_id, connection=connection)
                connection.execute("UPDATE outbox SET run_id = ? WHERE id = ?", (run_id, outbox_id))
                identity = topic_fingerprint(json.loads(sub["keywords_json"]))
                for item, article_id in selected:
                    connection.execute(
                        "INSERT INTO deliveries (id, subscription_id, article_id, run_id, outbox_id, topic_fingerprint, event_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(subscription_id, article_id) DO UPDATE SET "
                        "run_id = excluded.run_id, outbox_id = excluded.outbox_id, status = 'pending', "
                        "topic_fingerprint = excluded.topic_fingerprint, event_key = excluded.event_key, "
                        "attempts = 0, sent_at = NULL, feishu_message_id = NULL, last_error = NULL",
                        (str(uuid4()), sub["id"], article_id, run_id, outbox_id, identity, item.event_key),
                    )
                status, finished = "awaiting_delivery", None
            else:
                due = next_run(_subscription(sub).schedule, instant) if row["trigger"] == "scheduled" else _datetime(sub["next_run_at"])
                connection.execute(
                    "UPDATE subscriptions SET last_success_at = ?, consecutive_failures = 0, alerted = 0, "
                    "last_alert_at = NULL, next_run_at = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (timestamp, _utc_text(due) if due is not None else None, timestamp, sub["id"]),
                )
                status, finished = "completed", timestamp
            # Keep the owning identity as a replay fingerprint; a stale/replaced owner cannot acknowledge.
            connection.execute(
                "UPDATE subscription_runs SET status = ?, lease_until = NULL, completed_at = ?, "
                "search_window_days = ?, candidate_count = ?, selected_count = ?, last_error = NULL WHERE id = ?",
                (status, finished, search_window_days, len(results), len(selected), run_id),
            )
            return _run(connection.execute("SELECT * FROM subscription_runs WHERE id = ?", (run_id,)).fetchone())

    def fail(self, run_id: str, owner: str, error: str, now: Optional[datetime] = None) -> SubscriptionRun:
        if not isinstance(error, str) or not error.strip():
            raise ValidationError("error must be a non-empty string")
        timestamp = _utc_text(_instant(now))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, sub = self._owned(connection, run_id, owner, timestamp, ("failed",), check_version=False)
            if sub is None:
                return _run(row)
            connection.execute(
                "UPDATE subscription_runs SET status = 'failed', last_error = ?, completed_at = ?, "
                "lease_until = NULL WHERE id = ?", (error, timestamp, run_id),
            )
            if sub["version"] != row["subscription_version"]:
                return _run(connection.execute("SELECT * FROM subscription_runs WHERE id = ?", (run_id,)).fetchone())
            count = sub["consecutive_failures"] + 1
            alert = count >= 3 and not sub["alerted"]
            connection.execute(
                "UPDATE subscriptions SET consecutive_failures = ?, alerted = ?, last_alert_at = ?, "
                "updated_at = ?, version = version + 1 WHERE id = ?",
                (count, int(bool(sub["alerted"]) or alert), timestamp if alert else sub["last_alert_at"],
                 timestamp, sub["id"]),
            )
            if alert:
                card = render_command_result("订阅 #%d（%s）连续运行失败，请检查服务状态。" %
                                             (sub["display_number"], sub["topic"]))
                OutboxRepository(self.database).enqueue(sub["chat_id"], "card", card, "alert:" + run_id,
                                                       connection=connection)
            return _run(connection.execute("SELECT * FROM subscription_runs WHERE id = ?", (run_id,)).fetchone())
