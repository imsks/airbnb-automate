"""SQLite database layer for Airbnb Automate."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from app.config import get_db_path
from app.models import Campaign, CampaignStatus, Listing, Outreach, OutreachStatus


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Get a database connection."""
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize database tables."""
    conn = get_connection(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL,
                checkin TEXT NOT NULL,
                checkout TEXT NOT NULL,
                guests INTEGER DEFAULT 2,
                min_price REAL DEFAULT 0,
                max_price REAL DEFAULT 500,
                property_types TEXT DEFAULT '[]',
                amenities TEXT DEFAULT '[]',
                max_listings INTEGER DEFAULT 20,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                campaign_id INTEGER,
                url TEXT,
                title TEXT,
                host_name TEXT,
                location TEXT,
                price_per_night REAL,
                currency TEXT DEFAULT 'USD',
                rating REAL DEFAULT 0,
                review_count INTEGER DEFAULT 0,
                property_type TEXT,
                guests INTEGER DEFAULT 0,
                bedrooms INTEGER DEFAULT 0,
                bathrooms REAL DEFAULT 0,
                amenities TEXT DEFAULT '[]',
                photo_url TEXT,
                superhost INTEGER DEFAULT 0,
                scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
            );

            CREATE TABLE IF NOT EXISTS outreach (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                listing_id TEXT,
                host_name TEXT,
                listing_title TEXT,
                listing_url TEXT,
                message TEXT,
                status TEXT DEFAULT 'pending',
                sent_at TEXT,
                follow_up_count INTEGER DEFAULT 0,
                last_follow_up_at TEXT,
                response TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            );

            CREATE INDEX IF NOT EXISTS idx_listings_campaign
                ON listings(campaign_id);
            CREATE INDEX IF NOT EXISTS idx_outreach_campaign
                ON outreach(campaign_id);
            CREATE INDEX IF NOT EXISTS idx_outreach_status
                ON outreach(status);
        """)
        conn.commit()
    finally:
        conn.close()


# --- Campaign Operations ---


def create_campaign(campaign: Campaign, db_path: Optional[str] = None) -> int:
    """Create a new campaign and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO campaigns
               (name, location, checkin, checkout, guests,
                min_price, max_price, property_types, amenities,
                max_listings, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                campaign.name,
                campaign.location,
                campaign.checkin,
                campaign.checkout,
                campaign.guests,
                campaign.min_price,
                campaign.max_price,
                json.dumps(campaign.property_types),
                json.dumps(campaign.amenities),
                campaign.max_listings,
                campaign.status.value,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_campaigns(
    status: Optional[CampaignStatus] = None, db_path: Optional[str] = None
) -> list[Campaign]:
    """Get all campaigns, optionally filtered by status."""
    conn = get_connection(db_path)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM campaigns WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM campaigns ORDER BY created_at DESC"
            ).fetchall()

        campaigns = []
        for row in rows:
            campaigns.append(
                Campaign(
                    id=row["id"],
                    name=row["name"],
                    location=row["location"],
                    checkin=row["checkin"],
                    checkout=row["checkout"],
                    guests=row["guests"],
                    min_price=row["min_price"],
                    max_price=row["max_price"],
                    property_types=json.loads(row["property_types"]),
                    amenities=json.loads(row["amenities"]),
                    max_listings=row["max_listings"],
                    status=CampaignStatus(row["status"]),
                )
            )
        return campaigns
    finally:
        conn.close()


def get_campaign(campaign_id: int, db_path: Optional[str] = None) -> Optional[Campaign]:
    """Get a single campaign by ID."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        if not row:
            return None
        return Campaign(
            id=row["id"],
            name=row["name"],
            location=row["location"],
            checkin=row["checkin"],
            checkout=row["checkout"],
            guests=row["guests"],
            min_price=row["min_price"],
            max_price=row["max_price"],
            property_types=json.loads(row["property_types"]),
            amenities=json.loads(row["amenities"]),
            max_listings=row["max_listings"],
            status=CampaignStatus(row["status"]),
        )
    finally:
        conn.close()


def update_campaign_status(
    campaign_id: int, status: CampaignStatus, db_path: Optional[str] = None
) -> None:
    """Update campaign status."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE campaigns SET status = ? WHERE id = ?",
            (status.value, campaign_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- Listing Operations ---


def save_listings(
    listings: list[Listing], campaign_id: int, db_path: Optional[str] = None
) -> int:
    """Save listings to database. Returns count of new listings saved."""
    conn = get_connection(db_path)
    saved = 0
    try:
        for listing in listings:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO listings
                       (id, campaign_id, url, title, host_name, location,
                        price_per_night, currency, rating, review_count,
                        property_type, guests, bedrooms, bathrooms,
                        amenities, photo_url, superhost)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        listing.id,
                        campaign_id,
                        listing.url,
                        listing.title,
                        listing.host_name,
                        listing.location,
                        listing.price_per_night,
                        listing.currency,
                        listing.rating,
                        listing.review_count,
                        listing.property_type,
                        listing.guests,
                        listing.bedrooms,
                        listing.bathrooms,
                        json.dumps(listing.amenities),
                        listing.photo_url,
                        1 if listing.superhost else 0,
                    ),
                )
                saved += 1
            except sqlite3.IntegrityError:
                continue
        conn.commit()
        return saved
    finally:
        conn.close()


def get_listings(campaign_id: int, db_path: Optional[str] = None) -> list[Listing]:
    """Get all listings for a campaign."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM listings WHERE campaign_id = ? ORDER BY rating DESC",
            (campaign_id,),
        ).fetchall()

        return [
            Listing(
                id=row["id"],
                url=row["url"],
                title=row["title"],
                host_name=row["host_name"],
                location=row["location"],
                price_per_night=row["price_per_night"],
                currency=row["currency"],
                rating=row["rating"],
                review_count=row["review_count"],
                property_type=row["property_type"],
                guests=row["guests"],
                bedrooms=row["bedrooms"],
                bathrooms=row["bathrooms"],
                amenities=json.loads(row["amenities"]),
                photo_url=row["photo_url"],
                superhost=bool(row["superhost"]),
            )
            for row in rows
        ]
    finally:
        conn.close()


# --- Outreach Operations ---


def save_outreach(outreach: Outreach, db_path: Optional[str] = None) -> int:
    """Save an outreach record. Returns the outreach ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO outreach
               (campaign_id, listing_id, host_name, listing_title,
                listing_url, message, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                outreach.campaign_id,
                outreach.listing_id,
                outreach.host_name,
                outreach.listing_title,
                outreach.listing_url,
                outreach.message,
                outreach.status.value,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_outreach_status(
    outreach_id: int, status: OutreachStatus, db_path: Optional[str] = None
) -> None:
    """Update outreach status."""
    conn = get_connection(db_path)
    try:
        updates = {"status": status.value}
        if status == OutreachStatus.SENT:
            updates["sent_at"] = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"UPDATE outreach SET {', '.join(f'{k} = ?' for k in updates)} "
            f"WHERE id = ?",
            (*updates.values(), outreach_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_follow_up(outreach_id: int, db_path: Optional[str] = None) -> None:
    """Record a follow-up was sent."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """UPDATE outreach
               SET follow_up_count = follow_up_count + 1,
                   last_follow_up_at = ?,
                   status = ?
               WHERE id = ?""",
            (
                datetime.now(timezone.utc).isoformat(),
                OutreachStatus.FOLLOW_UP_SENT.value,
                outreach_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_outreach_for_campaign(
    campaign_id: int, db_path: Optional[str] = None
) -> list[Outreach]:
    """Get all outreach records for a campaign."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM outreach WHERE campaign_id = ? ORDER BY created_at DESC",
            (campaign_id,),
        ).fetchall()

        return [
            Outreach(
                id=row["id"],
                campaign_id=row["campaign_id"],
                listing_id=row["listing_id"],
                host_name=row["host_name"],
                listing_title=row["listing_title"],
                listing_url=row["listing_url"],
                message=row["message"],
                status=OutreachStatus(row["status"]),
                sent_at=row["sent_at"],
                follow_up_count=row["follow_up_count"],
                last_follow_up_at=row["last_follow_up_at"],
                response=row["response"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def get_outreach_stats(db_path: Optional[str] = None) -> dict:
    """Get overall outreach statistics."""
    conn = get_connection(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM outreach").fetchone()[0]
        sent = conn.execute(
            "SELECT COUNT(*) FROM outreach WHERE status != 'pending'"
        ).fetchone()[0]
        responded = conn.execute(
            "SELECT COUNT(*) FROM outreach WHERE status IN ('responded', 'accepted', 'declined')"
        ).fetchone()[0]
        accepted = conn.execute(
            "SELECT COUNT(*) FROM outreach WHERE status = 'accepted'"
        ).fetchone()[0]

        return {
            "total": total,
            "sent": sent,
            "responded": responded,
            "accepted": accepted,
            "response_rate": (responded / sent * 100) if sent > 0 else 0,
            "acceptance_rate": (accepted / responded * 100) if responded > 0 else 0,
        }
    finally:
        conn.close()
