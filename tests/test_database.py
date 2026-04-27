"""Tests for the database module."""

import os
import sqlite3
import tempfile

import pytest

from app.database import (
    init_db,
    create_search,
    get_search,
    get_searches,
    update_search_status,
    save_listings,
    get_listings,
    has_sent_outreach_to_listing,
    create_outreach_messages,
    get_outreach_messages,
    update_outreach_status,
)
from app.models import Listing, OutreachMessage, OutreachStatus, Search, SearchStatus


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


def test_get_listings_includes_rows_referenced_by_outreach_only(db_path):
    """When the same Airbnb listing id was stored under another search, outreach
    for a new search still resolves listing rows (INSERT OR IGNORE leaves the
    old search_id on the row).
    """
    search1 = Search(location="First", checkin="2026-01-01", checkout="2026-01-07")
    search2 = Search(location="Second", checkin="2026-01-01", checkout="2026-01-07")
    sid1 = create_search(search1, db_path)
    sid2 = create_search(search2, db_path)

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

    template = "Hi {host_name}, love {place_name} in {location}"
    create_outreach_messages(sid2, [listing], template, db_path)

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


def test_has_sent_outreach_to_listing(db_path):
    """After one message is SENT, we treat the listing as already contacted (any search)."""
    search = Search(location="A", checkin="2026-01-01", checkout="2026-01-08")
    sid = create_search(search, db_path)
    listings = [Listing(id="L1", title="T", host_name="H")]
    save_listings(listings, sid, db_path)
    template = "Hi {host_name} - {place_name} in {location}"
    create_outreach_messages(sid, listings, template, db_path)
    assert has_sent_outreach_to_listing("L1", db_path) is False
    msgs = get_outreach_messages(sid, db_path)
    update_outreach_status(msgs[0].id, OutreachStatus.SENT, "", db_path)
    assert has_sent_outreach_to_listing("L1", db_path) is True
    assert has_sent_outreach_to_listing("", db_path) is False


def test_create_outreach_skips_listing_globally_sent_in_other_search(db_path):
    """Do not create a new PENDING row if we already SENT to that listing_id elsewhere."""
    s1 = create_search(Search(location="A", checkin="2026-01-01", checkout="2026-01-08"), db_path)
    s2 = create_search(Search(location="B", checkin="2026-01-01", checkout="2026-01-08"), db_path)
    template = "Hi {host_name} - {place_name} in {location}"
    l = Listing(id="shared-room", title="Cottage", host_name="Jo", location="A")
    save_listings([l], s1, db_path)
    create_outreach_messages(s1, [l], template, db_path)
    m1 = get_outreach_messages(s1, db_path)
    assert len(m1) == 1
    update_outreach_status(m1[0].id, OutreachStatus.SENT, "", db_path)
    save_listings([l], s2, db_path)
    created2 = create_outreach_messages(s2, [l], template, db_path)
    assert created2 == []
    assert get_outreach_messages(s2, db_path) == []


def test_create_outreach_messages(db_path):
    """Test creating outreach messages for listings."""
    search = Search(location="Goa, India")
    sid = create_search(search, db_path)

    listings = [
        Listing(id="111", title="Beach Villa", host_name="Alice", location="Goa, India"),
        Listing(id="222", title="Mountain Lodge", host_name="Bob", location="Goa, India"),
    ]
    save_listings(listings, sid, db_path)

    template = "Hi {host_name}, I love {place_name} in {location}!"
    messages = create_outreach_messages(sid, listings, template, db_path)

    assert len(messages) == 2
    assert messages[0].host_name == "Alice"
    assert "Beach Villa" in messages[0].message
    assert messages[1].host_name == "Bob"
    assert "Mountain Lodge" in messages[1].message
    assert all(m.status == OutreachStatus.PENDING for m in messages)


def test_get_outreach_messages(db_path):
    """Test retrieving outreach messages for a search."""
    search = Search(location="Bali")
    sid = create_search(search, db_path)

    listings = [
        Listing(id="333", title="Treehouse", host_name="Charlie", location="Bali"),
    ]
    save_listings(listings, sid, db_path)

    template = "Hi {host_name}!"
    create_outreach_messages(sid, listings, template, db_path)

    messages = get_outreach_messages(sid, db_path)
    assert len(messages) == 1
    assert messages[0].listing_id == "333"
    assert messages[0].host_name == "Charlie"


def test_update_outreach_status(db_path):
    """Test updating outreach message status."""
    search = Search(location="Paris")
    sid = create_search(search, db_path)

    listings = [
        Listing(id="444", title="Parisian Flat", host_name="Diana", location="Paris"),
    ]
    save_listings(listings, sid, db_path)

    template = "Hi {host_name}!"
    messages = create_outreach_messages(sid, listings, template, db_path)

    # Update to sent
    update_outreach_status(messages[0].id, OutreachStatus.SENT, "", db_path)
    updated = get_outreach_messages(sid, db_path)
    assert updated[0].status == OutreachStatus.SENT
    assert updated[0].sent_at is not None


def test_update_outreach_status_failed(db_path):
    """Test updating outreach message status to failed with error."""
    search = Search(location="Tokyo")
    sid = create_search(search, db_path)

    listings = [
        Listing(id="555", title="Tokyo Apartment", host_name="Eve", location="Tokyo"),
    ]
    save_listings(listings, sid, db_path)

    template = "Hi {host_name}!"
    messages = create_outreach_messages(sid, listings, template, db_path)

    update_outreach_status(messages[0].id, OutreachStatus.FAILED, "Connection timeout", db_path)
    updated = get_outreach_messages(sid, db_path)
    assert updated[0].status == OutreachStatus.FAILED
    assert updated[0].error == "Connection timeout"


def test_duplicate_outreach_messages_skipped(db_path):
    """Test that duplicate outreach messages are not created."""
    search = Search(location="Goa")
    sid = create_search(search, db_path)

    listings = [
        Listing(id="666", title="Beach Shack", host_name="Frank", location="Goa"),
    ]
    save_listings(listings, sid, db_path)

    template = "Hi {host_name}!"
    first = create_outreach_messages(sid, listings, template, db_path)
    second = create_outreach_messages(sid, listings, template, db_path)

    assert len(first) == 1
    assert len(second) == 0  # Already exists, skipped

    all_messages = get_outreach_messages(sid, db_path)
    assert len(all_messages) == 1
