"""Tests for the database module."""

import os
import tempfile

import pytest

from app.database import (
    init_db,
    create_campaign,
    get_campaigns,
    get_campaign,
    save_listings,
    get_listings,
    save_outreach,
    get_outreach_for_campaign,
    get_outreach_stats,
    update_campaign_status,
    update_outreach_status,
)
from app.models import Campaign, CampaignStatus, Listing, Outreach, OutreachStatus


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


def test_create_and_get_campaign(db_path):
    """Test creating and retrieving a campaign."""
    campaign = Campaign(
        name="Test Campaign",
        location="Goa, India",
        checkin="2026-06-01",
        checkout="2026-06-07",
        guests=2,
        min_price=30,
        max_price=150,
    )
    campaign_id = create_campaign(campaign, db_path)
    assert campaign_id is not None
    assert campaign_id > 0

    retrieved = get_campaign(campaign_id, db_path)
    assert retrieved is not None
    assert retrieved.name == "Test Campaign"
    assert retrieved.location == "Goa, India"


def test_get_campaigns_by_status(db_path):
    """Test filtering campaigns by status."""
    c1 = Campaign(name="Active", location="A", checkin="2026-01-01", checkout="2026-01-07")
    c2 = Campaign(
        name="Paused", location="B", checkin="2026-02-01", checkout="2026-02-07",
        status=CampaignStatus.PAUSED,
    )
    create_campaign(c1, db_path)
    create_campaign(c2, db_path)

    active = get_campaigns(status=CampaignStatus.ACTIVE, db_path=db_path)
    assert len(active) == 1
    assert active[0].name == "Active"

    paused = get_campaigns(status=CampaignStatus.PAUSED, db_path=db_path)
    assert len(paused) == 1
    assert paused[0].name == "Paused"


def test_update_campaign_status(db_path):
    """Test updating campaign status."""
    campaign = Campaign(name="Test", location="X", checkin="2026-01-01", checkout="2026-01-07")
    cid = create_campaign(campaign, db_path)

    update_campaign_status(cid, CampaignStatus.COMPLETED, db_path)
    updated = get_campaign(cid, db_path)
    assert updated.status == CampaignStatus.COMPLETED


def test_save_and_get_listings(db_path):
    """Test saving and retrieving listings."""
    campaign = Campaign(name="Test", location="Goa", checkin="2026-01-01", checkout="2026-01-07")
    cid = create_campaign(campaign, db_path)

    listings = [
        Listing(id="123", title="Beach House", host_name="Alice", price_per_night=50, rating=4.8),
        Listing(id="456", title="Mountain Cabin", host_name="Bob", price_per_night=75, rating=4.5),
    ]

    saved = save_listings(listings, cid, db_path)
    assert saved == 2

    retrieved = get_listings(cid, db_path)
    assert len(retrieved) == 2
    # Sorted by rating DESC
    assert retrieved[0].title == "Beach House"
    assert retrieved[1].title == "Mountain Cabin"


def test_duplicate_listings_ignored(db_path):
    """Test that duplicate listings are not inserted."""
    campaign = Campaign(name="Test", location="X", checkin="2026-01-01", checkout="2026-01-07")
    cid = create_campaign(campaign, db_path)

    listing = Listing(id="123", title="Test Place", host_name="Host")
    save_listings([listing], cid, db_path)
    save_listings([listing], cid, db_path)  # Duplicate

    retrieved = get_listings(cid, db_path)
    assert len(retrieved) == 1


def test_outreach_crud(db_path):
    """Test outreach create, read, update operations."""
    campaign = Campaign(name="Test", location="X", checkin="2026-01-01", checkout="2026-01-07")
    cid = create_campaign(campaign, db_path)

    listing = Listing(id="123", title="Test Place", host_name="Host")
    save_listings([listing], cid, db_path)

    outreach = Outreach(
        campaign_id=cid,
        listing_id="123",
        host_name="Host",
        listing_title="Test Place",
        listing_url="https://airbnb.com/rooms/123",
        message="Hello! I'd love to collaborate...",
    )
    oid = save_outreach(outreach, db_path)
    assert oid > 0

    records = get_outreach_for_campaign(cid, db_path)
    assert len(records) == 1
    assert records[0].status == OutreachStatus.PENDING

    update_outreach_status(oid, OutreachStatus.SENT, db_path)
    records = get_outreach_for_campaign(cid, db_path)
    assert records[0].status == OutreachStatus.SENT


def test_outreach_stats(db_path):
    """Test outreach statistics calculation."""
    campaign = Campaign(name="Test", location="X", checkin="2026-01-01", checkout="2026-01-07")
    cid = create_campaign(campaign, db_path)

    # Create listings first (required by foreign key)
    listings = [
        Listing(id=str(i), title=f"Place {i}", host_name=f"Host {i}")
        for i in range(3)
    ]
    save_listings(listings, cid, db_path)

    # Create 3 outreach records with different statuses
    for i, status in enumerate([OutreachStatus.SENT, OutreachStatus.ACCEPTED, OutreachStatus.PENDING]):
        o = Outreach(
            campaign_id=cid,
            listing_id=str(i),
            host_name=f"Host {i}",
            listing_title=f"Place {i}",
            message="Hello",
            status=status,
        )
        save_outreach(o, db_path)

    stats = get_outreach_stats(db_path)
    assert stats["total"] == 3
    assert stats["sent"] == 2  # SENT + ACCEPTED (not PENDING)
    assert stats["accepted"] == 1
