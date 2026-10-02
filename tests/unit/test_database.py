import sqlite3
import tempfile
import unittest
from pathlib import Path

from hotnews.storage.database import Database


NOW = "2026-10-02T00:00:00Z"


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "state.sqlite")
        self.database = Database(self.path)

    def create_chat(self, connection, chat_id="chat-a"):
        connection.execute("INSERT INTO chats (chat_id) VALUES (?)", (chat_id,))

    def create_subscription(self, connection, subscription_id="sub-a", chat_id="chat-a", number=1):
        connection.execute(
            "INSERT INTO subscriptions "
            "(id, chat_id, display_number, creator_id, topic, keywords_json, search_terms_json, "
            "schedule_kind, daily_at, state, created_at, updated_at) "
            "VALUES (?, ?, ?, 'member', 'AI', '[\"AI\"]', '[]', 'daily', '09:00', 'ready', ?, ?)",
            (subscription_id, chat_id, number, NOW, NOW),
        )

    def create_article(self, connection, article_id="article-a", url_hash="hash-a"):
        connection.execute(
            "INSERT INTO articles "
            "(id, url_hash, title, url, source, published_at, summary, event_key, "
            "references_json, first_seen_at) "
            "VALUES (?, ?, 'Title', 'https://example.com/news', 'Example', ?, 'Summary', "
            "'event-a', '[]', ?)", (article_id, url_hash, NOW, NOW),
        )

    def create_run(self, connection, run_id="run-a", subscription_id="sub-a"):
        connection.execute(
            "INSERT INTO subscription_runs (id, subscription_id, trigger, status, created_at) "
            "VALUES (?, ?, 'scheduled', 'pending', ?)", (run_id, subscription_id, NOW),
        )

    def test_migration_creates_all_tables(self):
        self.database.migrate()
        with self.database.connect() as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertTrue({
                "schema_migrations", "chats", "inbound_events", "subscriptions",
                "subscription_runs", "articles", "deliveries", "outbox", "agent_leases",
            }.issubset(tables))
            migration = connection.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchone()
            self.assertEqual(migration[0], 1)
            self.assertRegex(migration[1], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

    def test_migration_is_idempotent(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            self.create_subscription(connection)
            original = tuple(connection.execute("SELECT * FROM schema_migrations").fetchone())
        self.database.migrate()
        self.database.migrate()
        with self.database.connect() as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM schema_migrations")],
                             [original])
            self.assertEqual(connection.execute("SELECT topic FROM subscriptions").fetchone()[0], "AI")

    def test_connections_enable_wal_foreign_keys_and_busy_timeout(self):
        self.database.migrate()
        for _ in range(2):
            with self.database.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertGreater(connection.execute("PRAGMA busy_timeout").fetchone()[0], 0)

    def test_group_display_number_is_unique_and_never_reused(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            self.create_chat(connection, "chat-b")
            self.create_subscription(connection)
            with self.assertRaises(sqlite3.IntegrityError):
                self.create_subscription(connection, "duplicate")
            connection.execute("UPDATE subscriptions SET state = 'cancelled', cancelled_at = ? WHERE id = ?",
                               (NOW, "sub-a"))
            with self.assertRaises(sqlite3.IntegrityError):
                self.create_subscription(connection, "reused")
            self.create_subscription(connection, "next", number=2)
            self.create_subscription(connection, "other-chat", chat_id="chat-b")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT next_display_number FROM chats WHERE chat_id = 'chat-a'")
                             .fetchone()[0], 1)

    def test_foreign_keys_reject_orphan_delivery(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            self.create_subscription(connection)
            self.create_article(connection)
            self.create_run(connection)
            for subscription_id, article_id, run_id in (
                ("missing", "article-a", "run-a"),
                ("sub-a", "missing", "run-a"),
                ("sub-a", "article-a", "missing"),
            ):
                with self.subTest(target=(subscription_id, article_id, run_id)):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO deliveries (id, subscription_id, article_id, run_id, status) "
                            "VALUES ('delivery', ?, ?, ?, 'pending')",
                            (subscription_id, article_id, run_id),
                        )

    def test_inbound_event_and_message_ids_are_each_unique(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            sql = ("INSERT INTO inbound_events "
                   "(id, event_id, message_id, chat_id, sender_id, raw_text, text, received_at) "
                   "VALUES (?, ?, ?, 'chat-a', 'member', '@bot AI', 'AI', ?)")
            connection.execute(sql, ("inbound-a", "event-a", "message-a", NOW))
            for event_id, message_id in (("event-a", "message-b"), ("event-b", "message-a")):
                with self.subTest(event_id=event_id, message_id=message_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(sql, ("duplicate", event_id, message_id, NOW))

    def test_inbound_mentions_archive_defaults_to_an_empty_array_and_disallows_null(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            connection.execute(
                "INSERT INTO inbound_events "
                "(id, event_id, message_id, chat_id, sender_id, raw_text, text, received_at) "
                "VALUES ('inbound-a', 'event-a', 'message-a', 'chat-a', 'member', '@bot AI', 'AI', ?)",
                (NOW,),
            )
            self.assertEqual(connection.execute("SELECT mentions_json FROM inbound_events").fetchone()[0], "[]")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE inbound_events SET mentions_json = NULL")

    def test_article_url_hash_is_unique(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_article(connection)
            with self.assertRaises(sqlite3.IntegrityError):
                self.create_article(connection, "article-b", "hash-a")

    def test_delivery_pair_is_unique(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            self.create_subscription(connection)
            self.create_article(connection)
            self.create_run(connection)
            sql = ("INSERT INTO deliveries (id, subscription_id, article_id, run_id, status) "
                   "VALUES (?, 'sub-a', 'article-a', 'run-a', 'pending')")
            connection.execute(sql, ("delivery-a",))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, ("delivery-b",))

    def test_outbox_idempotency_key_is_unique(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            sql = ("INSERT INTO outbox (id, chat_id, kind, content_json, idempotency_key, created_at) "
                   "VALUES (?, 'chat-a', 'text', '{\"text\":\"Received\"}', 'ack:event-a', ?)")
            connection.execute(sql, ("outbox-a", NOW))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, ("outbox-b", NOW))

    def test_lease_and_run_metadata_are_durable(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
            self.create_subscription(connection)
            self.create_run(connection)
            connection.execute(
                "UPDATE subscription_runs SET status = 'leased', lease_owner = 'worker', lease_until = ?, "
                "search_window_days = 7, started_at = ?, candidate_count = 4, selected_count = 2 WHERE id = 'run-a'",
                (NOW, NOW),
            )
            connection.execute("INSERT INTO agent_leases (name, owner, lease_until) VALUES ('global', 'worker', ?)",
                               (NOW,))
            connection.execute("UPDATE subscriptions SET version = 2, lease_owner = 'worker', lease_until = ?, "
                               "last_alert_at = ?, alerted = 1 WHERE id = 'sub-a'", (NOW, NOW))
        with self.database.connect() as connection:
            row = connection.execute("SELECT status, lease_owner, lease_until, search_window_days, "
                                     "candidate_count, selected_count FROM subscription_runs").fetchone()
            self.assertEqual(tuple(row), ("leased", "worker", NOW, 7, 4, 2))
            self.assertEqual(tuple(connection.execute("SELECT name, owner, lease_until FROM agent_leases").fetchone()),
                             ("global", "worker", NOW))
            self.assertEqual(connection.execute("SELECT version FROM subscriptions").fetchone()[0], 2)

    def test_connection_commits_success_rolls_back_errors_and_closes(self):
        self.database.migrate()
        with self.database.connect() as connection:
            self.create_chat(connection)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with self.database.connect() as connection:
                self.create_chat(connection, "rolled-back")
                raise RuntimeError("stop")
        with self.database.connect() as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT chat_id FROM chats")], ["chat-a"])

    def test_migration_failure_rolls_back_ddl_and_version(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE articles (existing TEXT)")
            connection.execute("INSERT INTO articles VALUES ('preserved')")
        with self.assertRaises(sqlite3.OperationalError):
            self.database.migrate()
        with self.database.connect() as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")],
                             ["articles"])
            self.assertEqual(connection.execute("SELECT existing FROM articles").fetchone()[0], "preserved")

    def test_newer_schema_is_rejected_without_downgrade(self):
        self.database.migrate()
        with self.database.connect() as connection:
            connection.execute("UPDATE schema_migrations SET version = 2")
        with self.assertRaisesRegex(RuntimeError, "newer"):
            self.database.migrate()
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_migrations").fetchone()[0], 2)

    def test_missing_database_parent_directory_is_created(self):
        nested_path = str(Path(self.path).parent / "nested" / "data" / "state.sqlite")
        database = Database(nested_path)
        database.migrate()
        with database.connect() as connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_migrations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
