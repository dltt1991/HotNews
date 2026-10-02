"""Configured SQLite connections and atomic, forward-only schema migrations."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 5000

_VERSION_1 = (
    """CREATE TABLE chats (
        chat_id TEXT PRIMARY KEY NOT NULL,
        name TEXT,
        next_display_number INTEGER NOT NULL DEFAULT 1 CHECK (next_display_number > 0)
    )""",
    """CREATE TABLE inbound_events (
        id TEXT PRIMARY KEY NOT NULL,
        event_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        chat_id TEXT NOT NULL REFERENCES chats(chat_id),
        sender_id TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        text TEXT NOT NULL,
        mentions_json TEXT NOT NULL DEFAULT '[]',
        received_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'leased', 'completed', 'failed')),
        lease_owner TEXT,
        lease_until TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        result_summary TEXT
    )""",
    "CREATE UNIQUE INDEX inbound_events_event_id ON inbound_events(event_id)",
    "CREATE UNIQUE INDEX inbound_events_message_id ON inbound_events(message_id)",
    "CREATE INDEX inbound_events_queue ON inbound_events(status, lease_until, received_at)",
    """CREATE TABLE subscriptions (
        id TEXT PRIMARY KEY NOT NULL,
        chat_id TEXT NOT NULL REFERENCES chats(chat_id),
        display_number INTEGER NOT NULL CHECK (display_number > 0),
        creator_id TEXT NOT NULL,
        topic TEXT NOT NULL,
        keywords_json TEXT NOT NULL,
        search_terms_json TEXT NOT NULL DEFAULT '[]',
        schedule_kind TEXT NOT NULL,
        daily_at TEXT,
        interval_minutes INTEGER,
        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        next_run_at TEXT,
        state TEXT NOT NULL DEFAULT 'ready'
            CHECK (state IN ('ready', 'search_terms_pending', 'paused', 'cancelled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        cancelled_at TEXT,
        last_success_at TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        alerted INTEGER NOT NULL DEFAULT 0,
        last_alert_at TEXT,
        lease_owner TEXT,
        lease_until TEXT
    )""",
    # Include cancelled history: a partial active-only index would permit number reuse.
    "CREATE UNIQUE INDEX subscriptions_chat_number ON subscriptions(chat_id, display_number)",
    "CREATE INDEX subscriptions_due ON subscriptions(next_run_at) WHERE state = 'ready'",
    "CREATE INDEX subscriptions_term_refresh ON subscriptions(lease_until) WHERE state = 'search_terms_pending'",
    """CREATE TABLE subscription_runs (
        id TEXT PRIMARY KEY NOT NULL,
        subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
        trigger TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'leased', 'awaiting_delivery', 'completed', 'failed')),
        created_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_until TEXT,
        search_window_days INTEGER,
        started_at TEXT,
        completed_at TEXT,
        last_error TEXT,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        selected_count INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX subscription_runs_queue ON subscription_runs(status, lease_until, created_at)",
    "CREATE INDEX subscription_runs_subscription ON subscription_runs(subscription_id)",
    """CREATE TABLE articles (
        id TEXT PRIMARY KEY NOT NULL,
        url_hash TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        source TEXT NOT NULL,
        published_at TEXT NOT NULL,
        summary TEXT NOT NULL,
        event_key TEXT NOT NULL,
        references_json TEXT NOT NULL DEFAULT '[]',
        first_seen_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX articles_url_hash ON articles(url_hash)",
    "CREATE INDEX articles_event_key ON articles(event_key)",
    """CREATE TABLE outbox (
        id TEXT PRIMARY KEY NOT NULL,
        chat_id TEXT NOT NULL REFERENCES chats(chat_id),
        kind TEXT NOT NULL,
        content_json TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_until TEXT,
        next_attempt_at TEXT,
        last_error TEXT,
        run_id TEXT REFERENCES subscription_runs(id),
        feishu_message_id TEXT,
        sent_at TEXT
    )""",
    "CREATE UNIQUE INDEX outbox_idempotency_key ON outbox(idempotency_key)",
    "CREATE INDEX outbox_queue ON outbox(status, next_attempt_at, lease_until, created_at)",
    """CREATE TABLE deliveries (
        id TEXT PRIMARY KEY NOT NULL,
        subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
        article_id TEXT NOT NULL REFERENCES articles(id),
        run_id TEXT NOT NULL REFERENCES subscription_runs(id),
        outbox_id TEXT REFERENCES outbox(id),
        feishu_message_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        sent_at TEXT,
        last_error TEXT
    )""",
    "CREATE UNIQUE INDEX deliveries_subscription_article ON deliveries(subscription_id, article_id)",
    "CREATE INDEX deliveries_run ON deliveries(run_id)",
    "CREATE INDEX deliveries_outbox ON deliveries(outbox_id)",
    """CREATE TABLE agent_leases (
        name TEXT PRIMARY KEY NOT NULL,
        owner TEXT NOT NULL,
        lease_until TEXT NOT NULL
    )""",
)
_MIGRATIONS = ((1, _VERSION_1),)


class Database:
    """Open a file-backed database; each connection scope is one transaction.

    A successful scope commits, an exceptional scope rolls back, and the
    connection always closes. Repositories may use BEGIN IMMEDIATE at the
    start of a scope when claiming work or allocating chat display numbers.
    """

    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = %d" % _BUSY_TIMEOUT_MS)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        """Apply outstanding DDL and version markers in one write transaction."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )""")
            current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RuntimeError("database schema is newer than this application supports")
            for version, statements in _MIGRATIONS:
                if version <= current:
                    continue
                # executescript implicitly commits; individual execute calls preserve atomic DDL.
                for statement in statements:
                    connection.execute(statement)
                applied_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                                   (version, applied_at))
