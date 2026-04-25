"""Tests for the database module."""

import os
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
)
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
    save_listings([listing], sid, db_path)
    save_listings([listing], sid, db_path)  # Duplicate

    retrieved = get_listings(sid, db_path)
    assert len(retrieved) == 1


def test_search_with_optional_fields(db_path):
    """Test creating a search with only location (no dates or price)."""
    search = Search(location="Tokyo, Japan")
    sid = create_search(search, db_path)

    retrieved = get_search(sid, db_path)
    assert retrieved.location == "Tokyo, Japan"
    assert retrieved.checkin == ""
    assert retrieved.min_price is None
    assert retrieved.max_price is None
