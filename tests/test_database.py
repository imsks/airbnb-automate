"""Tests for the database module."""

import os
import sqlite3
import tempfile

import pytest

from app.database import (
    init_db,
    create_search,
    get_connection,
    get_listing,
    get_search,
    get_searches,
    update_search_status,
    reset_pipeline,
    save_listings,
    get_listings,
)
from app.leads import upsert_lead
from app.models import Listing, Search, SearchStatus


@pytest.fixture
def db_path():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def test_init_db(db_path):
    """Test database initialization creates tables."""
    # Should not raise
    init_db(db_path)


def test_init_db_migrates_legacy_listings_campaign_id_to_search_id():
    """Old DBs had listings.campaign_id; init_db must add search_id before indexing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                campaign_id INTEGER,
                url TEXT
            );
        """
        )
        conn.close()

        init_db(path)

        conn = sqlite3.connect(path)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
        assert "search_id" in columns
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_listings_search'"
        ).fetchone()
        assert idx is not None
        conn.close()
    finally:
        os.unlink(path)


def test_create_and_get_search(db_path):
    """Test creating and retrieving a search."""
    search = Search(
        location="Goa, India",
        checkin="2026-06-01",
        checkout="2026-06-07",
        guests=2,
        min_price=30,
        max_price=150,
    )
    search_id = create_search(search, db_path)
    assert search_id is not None
    assert search_id > 0

    retrieved = get_search(search_id, db_path)
    assert retrieved is not None
    assert retrieved.location == "Goa, India"
    assert retrieved.guests == 2


def test_get_searches(db_path):
    """Test listing all searches."""
    s1 = Search(location="Goa, India", checkin="2026-01-01", checkout="2026-01-07")
    s2 = Search(location="Bali, Indonesia", checkin="2026-02-01", checkout="2026-02-07")
    create_search(s1, db_path)
    create_search(s2, db_path)

    searches = get_searches(db_path)
    assert len(searches) == 2


def test_update_search_status(db_path):
    """Test updating search status."""
    search = Search(location="Paris", checkin="2026-01-01", checkout="2026-01-07")
    sid = create_search(search, db_path)

    update_search_status(sid, SearchStatus.COMPLETED, 10, db_path)
    updated = get_search(sid, db_path)
    assert updated.status == SearchStatus.COMPLETED
    assert updated.listings_count == 10


def test_save_and_get_listings(db_path):
    """Test saving and retrieving listings."""
    search = Search(location="Goa", checkin="2026-01-01", checkout="2026-01-07")
    sid = create_search(search, db_path)

    listings = [
        Listing(id="123", title="Beach House", host_name="Alice", price_per_night=50, rating=4.8),
        Listing(id="456", title="Mountain Cabin", host_name="Bob", price_per_night=75, rating=4.5),
    ]

    saved = save_listings(listings, sid, db_path)
    assert saved == 2

    retrieved = get_listings(sid, db_path)
    assert len(retrieved) == 2
    # Sorted by rating DESC
    assert retrieved[0].title == "Beach House"
    assert retrieved[1].title == "Mountain Cabin"


def test_duplicate_listings_ignored(db_path):
    """Test that duplicate listings are not inserted."""
    search = Search(location="X", checkin="2026-01-01", checkout="2026-01-07")
    sid = create_search(search, db_path)

    listing = Listing(id="123", title="Test Place", host_name="Host")
    assert save_listings([listing], sid, db_path) == 1
    assert save_listings([listing], sid, db_path) == 0  # INSERT OR IGNORE

    retrieved = get_listings(sid, db_path)
    assert len(retrieved) == 1


def test_get_listings_resolves_rows_claimed_by_an_earlier_search(db_path):
    """A listing rediscovered by a later search keeps the first search's id on
    the row, so the lead is what ties it to the new run.
    """
    sid1 = create_search(Search(location="First"), db_path)
    sid2 = create_search(Search(location="Second"), db_path)

    listing = Listing(
        id="room-999",
        title="Cottage",
        host_name="Jamie",
        location="Second",
        rating=4.2,
    )
    assert save_listings([listing], sid1, db_path) == 1
    assert save_listings([listing], sid2, db_path) == 0  # row still has sid1

    assert get_listings(sid2, db_path) == []

    upsert_lead("room-999", search_id=sid2, db_path=db_path)

    combined = get_listings(sid2, db_path)
    assert len(combined) == 1
    assert combined[0].id == "room-999"
    assert combined[0].title == "Cottage"
def test_search_with_optional_fields(db_path):
    """Test creating a search with only location (no dates or price)."""
    search = Search(location="Tokyo, Japan")
    sid = create_search(search, db_path)

    retrieved = get_search(sid, db_path)
    assert retrieved.location == "Tokyo, Japan"
    assert retrieved.checkin == ""
    assert retrieved.min_price is None
    assert retrieved.max_price is None


def test_get_listing_fetches_one_by_id(db_path):
    sid = create_search(Search(location="Goa"), db_path)
    save_listings([Listing(id="L9", title="Villa", host_name="Zoe")], sid, db_path)
    assert get_listing("L9", db_path).title == "Villa"
    assert get_listing("nope", db_path) is None


def test_reset_pipeline_wipes_data_but_keeps_schema_and_policy(db_path):
    from app import policy as policy_mod

    sid = create_search(Search(location="Goa"), db_path)
    save_listings([Listing(id="L1", title="Villa", host_name="Zoe")], sid, db_path)
    upsert_lead("L1", db_path=db_path)
    policy_mod.freeze_sending("kill switch on", db_path)  # a guardrail to preserve

    wiped = reset_pipeline(db_path)

    assert "listings" in wiped and "leads" in wiped and "searches" in wiped
    assert "policy" not in wiped and "schema_migrations" not in wiped
    assert get_listing("L1", db_path) is None
    assert get_searches(db_path) == []
    # guardrail/kill switch survives the wipe
    assert policy_mod.sending_enabled(db_path) is False

    conn = get_connection(db_path)
    try:
        # schema still intact: tables are queryable after the wipe
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    finally:
        conn.close()
