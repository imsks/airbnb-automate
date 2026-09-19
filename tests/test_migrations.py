"""Tests for the migration runner."""

import os
import sqlite3
import tempfile

import pytest

from app.database import init_db
from app.migrations import applied_versions, discover_migrations, run_migrations


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_migration_filenames_are_well_formed():
    versions = [v for v, _, _ in discover_migrations()]
    assert versions == sorted(versions)
    assert versions[0] == 1


def test_init_db_creates_v2_tables(db_path):
    init_db(db_path)
    tables = _tables(db_path)
    for expected in (
        "territories",
        "territory_profiles",
        "campaigns",
        "campaign_stops",
        "leads",
        "deals",
        "deal_events",
        "messages",
        "jobs",
        "agent_runs",
        "policy",
    ):
        assert expected in tables


def test_migrations_are_recorded_and_not_reapplied(db_path):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        first = applied_versions(conn)
        assert first
        assert run_migrations(conn) == []
    finally:
        conn.close()


def test_init_db_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)
    init_db(db_path)
    assert "deals" in _tables(db_path)


def test_legacy_database_adopts_migrations_without_data_loss():
    """A v1 database must upgrade in place, keeping its rows."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL
            );
            CREATE TABLE listings (
                id TEXT PRIMARY KEY,
                campaign_id INTEGER,
                url TEXT
            );
            INSERT INTO searches (location) VALUES ('Goa, India');
            INSERT INTO listings (id, campaign_id, url) VALUES ('L1', 1, 'http://x');
            """
        )
        conn.commit()
        conn.close()

        init_db(path)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            assert conn.execute("SELECT COUNT(*) c FROM searches").fetchone()["c"] == 1
            listing = conn.execute("SELECT * FROM listings").fetchone()
            assert listing["search_id"] == 1
            assert "deals" in {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
    finally:
        os.unlink(path)


def test_backfill_converts_outreach_history_into_deals():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL
            );
            CREATE TABLE listings (
                id TEXT PRIMARY KEY,
                search_id INTEGER,
                url TEXT
            );
            CREATE TABLE outreach_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                host_name TEXT DEFAULT '',
                place_name TEXT DEFAULT '',
                location TEXT DEFAULT '',
                message TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            );
            INSERT INTO searches (location) VALUES ('Goa, India');
            INSERT INTO listings (id, search_id) VALUES ('L1', 1), ('L2', 1);
            INSERT INTO outreach_messages
                (search_id, listing_id, host_name, place_name, location, message, status, sent_at)
            VALUES
                (1, 'L1', 'Asha', 'Sea Villa', 'Goa, India', 'hello', 'sent', '2026-01-01T00:00:00'),
                (1, 'L2', 'Ravi', 'Hill Hut', 'Goa, India', 'hi', 'skipped', NULL);
            """
        )
        conn.commit()
        conn.close()

        init_db(path)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            deals = {r["listing_id"]: r for r in conn.execute("SELECT * FROM deals")}
            assert deals["L1"]["state"] == "contacted"
            assert deals["L1"]["host_name"] == "Asha"
            assert deals["L2"]["state"] == "disqualified"

            messages = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
            assert len(messages) == 2
            assert messages[0]["body"] == "hello"
            assert messages[0]["status"] == "sent"
            assert messages[1]["status"] == "failed"

            events = conn.execute("SELECT * FROM deal_events").fetchall()
            assert len(events) == 2
            assert all(e["actor"] == "migration" for e in events)

            territory = conn.execute(
                "SELECT * FROM territories WHERE name = 'Goa, India'"
            ).fetchone()
            assert territory is not None
            assert territory["messages_sent"] == 1
        finally:
            conn.close()
    finally:
        os.unlink(path)


def test_backfill_skips_orphaned_listing_references():
    """Historical outreach rows can point at listings that were never saved."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL
            );
            CREATE TABLE listings (id TEXT PRIMARY KEY, search_id INTEGER);
            CREATE TABLE outreach_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                host_name TEXT DEFAULT '',
                place_name TEXT DEFAULT '',
                location TEXT DEFAULT '',
                message TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            );
            INSERT INTO searches (location) VALUES ('Goa, India');
            INSERT INTO outreach_messages (search_id, listing_id, message, status)
            VALUES (1, 'GHOST', 'hello', 'sent');
            """
        )
        conn.commit()
        conn.close()

        init_db(path)

        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        os.unlink(path)
