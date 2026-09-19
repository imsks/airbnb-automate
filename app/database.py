"""SQLite database layer for Airbnb Automate."""

import json
import sqlite3
from typing import Optional

from app.config import get_db_path
from app.migrations import run_migrations
from app.models import Listing, Search, SearchStatus


def _listings_table_columns(conn: sqlite3.Connection) -> set[str]:
    """Return column names for the listings table, or empty set if the table is missing."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='listings'"
    ).fetchone()
    if not row:
        return set()
    return {r[1] for r in conn.execute("PRAGMA table_info(listings)")}


def _migrate_searches_flexible_columns(conn: sqlite3.Connection) -> None:
    """Add date_mode / flex duration columns for flexible-trip searches."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='searches'"
    ).fetchone()
    if not row:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(searches)")}
    if "date_mode" not in columns:
        conn.execute(
            "ALTER TABLE searches ADD COLUMN date_mode TEXT DEFAULT 'flexible'"
        )
    if "flex_duration" not in columns:
        conn.execute("ALTER TABLE searches ADD COLUMN flex_duration INTEGER DEFAULT 1")
    if "flex_duration_unit" not in columns:
        conn.execute(
            "ALTER TABLE searches ADD COLUMN flex_duration_unit TEXT DEFAULT 'week'"
        )


def _migrate_listings_search_id(conn: sqlite3.Connection) -> None:
    """Ensure listings has search_id (legacy DBs used campaign_id or predate the column)."""
    columns = _listings_table_columns(conn)
    if not columns or "search_id" in columns:
        return
    conn.execute("ALTER TABLE listings ADD COLUMN search_id INTEGER")
    if "campaign_id" in columns:
        conn.execute(
            "UPDATE listings SET search_id = campaign_id "
            "WHERE search_id IS NULL"
        )
    # Foreign keys: historical rows may reference IDs that are not in `searches`;
    # SQLite does not re-validate existing rows after ALTER.


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
    """Bring the database up to the latest schema version."""
    conn = get_connection(db_path)
    try:
        # Legacy column fixups run first: migration 0001 indexes listings(search_id),
        # which fails outright on a pre-search_id table.
        _migrate_searches_flexible_columns(conn)
        _migrate_listings_search_id(conn)
        conn.commit()
        run_migrations(conn)
    finally:
        conn.close()


# --- Search Operations ---


def create_search(search: Search, db_path: Optional[str] = None) -> int:
    """Create a new search record and return its ID."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO searches
               (location, checkin, checkout, guests, min_price, max_price,
                date_mode, flex_duration, flex_duration_unit, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                search.location,
                search.checkin,
                search.checkout,
                search.guests,
                search.min_price,
                search.max_price,
                search.date_mode,
                search.flex_duration,
                search.flex_duration_unit,
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
        return _search_from_row(row)
    finally:
        conn.close()


def _search_from_row(row: sqlite3.Row) -> Search:
    """Build Search from DB row, tolerating legacy DBs without flex columns."""
    keys = row.keys()
    return Search(
        id=row["id"],
        location=row["location"],
        checkin=row["checkin"],
        checkout=row["checkout"],
        guests=row["guests"],
        min_price=row["min_price"],
        max_price=row["max_price"],
        date_mode=row["date_mode"] if "date_mode" in keys else "flexible",
        flex_duration=row["flex_duration"] if "flex_duration" in keys else 1,
        flex_duration_unit=row["flex_duration_unit"]
        if "flex_duration_unit" in keys
        else "week",
        status=SearchStatus(row["status"]),
        listings_count=row["listings_count"],
    )


def get_searches(db_path: Optional[str] = None) -> list[Search]:
    """Get all searches, ordered by most recent first."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM searches ORDER BY created_at DESC"
        ).fetchall()

        return [_search_from_row(row) for row in rows]
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
                cur = conn.execute(
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
                if cur.rowcount > 0:
                    saved += 1
            except sqlite3.IntegrityError:
                continue
        conn.commit()
        return saved
    finally:
        conn.close()


def get_listings(search_id: int, db_path: Optional[str] = None) -> list[Listing]:
    """Get all listings for a search.

    Includes rows whose ``search_id`` matches, plus any listing a *lead* on this
    search points at. The second half matters because ``listings.id`` is a
    global primary key: ``INSERT OR IGNORE`` skips a listing already stored by
    an earlier search, so its ``search_id`` still names the older run even
    though this run just rediscovered it.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT l.* FROM listings l WHERE l.search_id = ?
                UNION
                SELECT l.* FROM listings l
                INNER JOIN leads ld ON ld.listing_id = l.id AND ld.search_id = ?
            ) AS combined
            ORDER BY rating DESC
            """,
            (search_id, search_id),
        ).fetchall()

        return [_listing_from_row(row) for row in rows]
    finally:
        conn.close()


def _listing_from_row(row: sqlite3.Row) -> Listing:
    """Build a Listing, tolerating NULLs in nullable columns.

    A partial scrape or a legacy row leaves text columns NULL, and a single one
    of those used to raise and take the whole search's listings with it.
    """

    def text(key: str) -> str:
        return row[key] or ""

    def number(key: str, default=0):
        value = row[key]
        return default if value is None else value

    return Listing(
        id=text("id"),
        url=text("url"),
        title=text("title"),
        host_name=text("host_name"),
        location=text("location"),
        price_per_night=number("price_per_night", 0.0),
        currency=row["currency"] or "USD",
        rating=number("rating", 0.0),
        review_count=number("review_count"),
        property_type=text("property_type"),
        guests=number("guests"),
        bedrooms=number("bedrooms"),
        bathrooms=number("bathrooms", 0.0),
        amenities=json.loads(row["amenities"] or "[]"),
        photo_url=text("photo_url"),
        superhost=bool(number("superhost")),
    )


# --- Global send rate (sliding window, shared by outreach and negotiation) ---


def outreach_send_log_prune(
    db_path: Optional[str] = None,
    *,
    max_age_seconds: float = 86400 * 14,
) -> None:
    """Drop old send log rows so the table stays small."""
    import time

    cutoff = time.time() - max_age_seconds
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM outreach_send_log WHERE sent_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def outreach_send_log_record(
    db_path: Optional[str] = None,
    sent_at: Optional[float] = None,
) -> None:
    """Record one successful host message (Unix timestamp)."""
    import time

    ts = time.time() if sent_at is None else float(sent_at)
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO outreach_send_log (sent_at) VALUES (?)", (ts,))
        conn.commit()
    finally:
        conn.close()


def outreach_send_log_count_in_window(
    db_path: Optional[str],
    window_sec: float,
    *,
    now: Optional[float] = None,
) -> int:
    """How many sends recorded in (now - window_sec, now]."""
    import time

    n = time.time() if now is None else float(now)
    cutoff = n - float(window_sec)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM outreach_send_log WHERE sent_at > ?",
            (cutoff,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def outreach_send_log_oldest_in_window(
    db_path: Optional[str],
    window_sec: float,
    *,
    now: Optional[float] = None,
) -> Optional[float]:
    """Earliest sent_at still inside the sliding window, or None if empty."""
    import time

    n = time.time() if now is None else float(now)
    cutoff = n - float(window_sec)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MIN(sent_at) FROM outreach_send_log WHERE sent_at > ?",
            (cutoff,),
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        return None
    finally:
        conn.close()
