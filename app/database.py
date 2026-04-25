"""SQLite database layer for Airbnb Automate."""

import json
import sqlite3
from typing import Optional

from app.config import get_db_path
from app.models import Listing, Search, SearchStatus


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
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL,
                checkin TEXT DEFAULT '',
                checkout TEXT DEFAULT '',
                guests INTEGER DEFAULT 2,
                min_price REAL,
                max_price REAL,
                status TEXT DEFAULT 'searching',
                listings_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY,
                search_id INTEGER,
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
                FOREIGN KEY (search_id) REFERENCES searches(id)
            );

            CREATE INDEX IF NOT EXISTS idx_listings_search
                ON listings(search_id);
        """)
        conn.commit()
    finally:
        conn.close()


# --- Search Operations ---


def create_search(search: Search, db_path: Optional[str] = None) -> int:
    """Create a new search record and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO searches
               (location, checkin, checkout, guests, min_price, max_price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                search.location,
                search.checkin,
                search.checkout,
                search.guests,
                search.min_price,
                search.max_price,
                search.status.value,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_search(search_id: int, db_path: Optional[str] = None) -> Optional[Search]:
    """Get a single search by ID."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM searches WHERE id = ?", (search_id,)
        ).fetchone()
        if not row:
            return None
        return Search(
            id=row["id"],
            location=row["location"],
            checkin=row["checkin"],
            checkout=row["checkout"],
            guests=row["guests"],
            min_price=row["min_price"],
            max_price=row["max_price"],
            status=SearchStatus(row["status"]),
            listings_count=row["listings_count"],
        )
    finally:
        conn.close()


def get_searches(db_path: Optional[str] = None) -> list[Search]:
    """Get all searches, ordered by most recent first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM searches ORDER BY created_at DESC"
        ).fetchall()

        return [
            Search(
                id=row["id"],
                location=row["location"],
                checkin=row["checkin"],
                checkout=row["checkout"],
                guests=row["guests"],
                min_price=row["min_price"],
                max_price=row["max_price"],
                status=SearchStatus(row["status"]),
                listings_count=row["listings_count"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def update_search_status(
    search_id: int,
    status: SearchStatus,
    listings_count: int = 0,
    db_path: Optional[str] = None,
) -> None:
    """Update search status and listings count."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE searches SET status = ?, listings_count = ? WHERE id = ?",
            (status.value, listings_count, search_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- Listing Operations ---


def save_listings(
    listings: list[Listing], search_id: int, db_path: Optional[str] = None
) -> int:
    """Save listings to database. Returns count of new listings saved."""
    conn = get_connection(db_path)
    saved = 0
    try:
        for listing in listings:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO listings
                       (id, search_id, url, title, host_name, location,
                        price_per_night, currency, rating, review_count,
                        property_type, guests, bedrooms, bathrooms,
                        amenities, photo_url, superhost)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        listing.id,
                        search_id,
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


def get_listings(search_id: int, db_path: Optional[str] = None) -> list[Listing]:
    """Get all listings for a search."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM listings WHERE search_id = ? ORDER BY rating DESC",
            (search_id,),
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
