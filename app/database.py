"""SQLite database layer for Airbnb Automate."""

import json
import sqlite3
from typing import Optional

from app.config import get_db_path
from app.models import Listing, OutreachMessage, OutreachStatus, Search, SearchStatus


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
    """Initialize database tables and migrate legacy schemas."""
    conn = get_connection(db_path)
    try:
        # Tables only: IF NOT EXISTS leaves an old listings table unchanged, so we
        # must migrate before creating indexes on new columns.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL,
                checkin TEXT DEFAULT '',
                checkout TEXT DEFAULT '',
                guests INTEGER DEFAULT 2,
                min_price REAL,
                max_price REAL,
                date_mode TEXT DEFAULT 'flexible',
                flex_duration INTEGER DEFAULT 1,
                flex_duration_unit TEXT DEFAULT 'week',
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

            CREATE TABLE IF NOT EXISTS outreach_messages (
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
                sent_at TEXT,
                FOREIGN KEY (search_id) REFERENCES searches(id),
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            );

            CREATE TABLE IF NOT EXISTS outreach_send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dismissed_threads (
                thread_id TEXT PRIMARY KEY,
                host_name TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                dismissed_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        _migrate_searches_flexible_columns(conn)
        _migrate_listings_search_id(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_search "
            "ON listings(search_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreach_search "
            "ON outreach_messages(search_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreach_listing "
            "ON outreach_messages(listing_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreach_send_log_sent_at "
            "ON outreach_send_log(sent_at)"
        )
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

    Includes rows with ``search_id`` set to this search, and rows that appear in
    ``outreach_messages`` for this search. The latter is required because
    ``listings.id`` is a global primary key: ``INSERT OR IGNORE`` skips rows
    already stored from an older search, so ``search_id`` on the row may not
    match a new run while ``outreach_messages`` still points at those listing
    ids.
    """
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT l.* FROM listings l WHERE l.search_id = ?
                UNION
                SELECT l.* FROM listings l
                INNER JOIN outreach_messages om
                    ON om.listing_id = l.id AND om.search_id = ?
            ) AS combined
            ORDER BY rating DESC
            """,
            (search_id, search_id),
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


def has_sent_outreach_to_listing(
    listing_id: str, db_path: Optional[str] = None
) -> bool:
    """True if any row has successfully sent a message to this listing (any search)."""
    if not (listing_id or "").strip():
        return False
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM outreach_messages WHERE listing_id = ? AND status = ? LIMIT 1",
            (listing_id, OutreachStatus.SENT.value),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def create_outreach_messages(
    search_id: int,
    listings: list[Listing],
    message_template: str,
    db_path: Optional[str] = None,
) -> list[OutreachMessage]:
    """Create outreach message records for each listing. Returns created messages."""
    conn = get_connection(db_path)
    messages = []
    try:
        for listing in listings:
            host = listing.host_name or "Host"
            place = listing.title or "your place"
            location = listing.location or "your area"

            try:
                personalized = message_template.format(
                    host_name=host,
                    place_name=place,
                    location=location,
                )
            except KeyError as e:
                # Gracefully handle invalid placeholders in the template
                personalized = message_template.replace("{host_name}", host)
                personalized = personalized.replace("{place_name}", place)
                personalized = personalized.replace("{location}", location)

            # Skip if we already have a message for this listing+search
            existing = conn.execute(
                "SELECT id FROM outreach_messages WHERE search_id = ? AND listing_id = ?",
                (search_id, listing.id),
            ).fetchone()
            if existing:
                continue

            # Never queue another message if we already sent to this host/listing (any search)
            if has_sent_outreach_to_listing(listing.id, db_path):
                continue

            cursor = conn.execute(
                """INSERT INTO outreach_messages
                   (search_id, listing_id, host_name, place_name, location, message, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    search_id,
                    listing.id,
                    host,
                    place,
                    location,
                    personalized,
                    OutreachStatus.PENDING.value,
                ),
            )
            messages.append(
                OutreachMessage(
                    id=cursor.lastrowid,
                    search_id=search_id,
                    listing_id=listing.id,
                    host_name=host,
                    place_name=place,
                    location=location,
                    message=personalized,
                    status=OutreachStatus.PENDING,
                )
            )
        conn.commit()
        return messages
    finally:
        conn.close()


def create_outreach_message_direct(
    search_id: int,
    listing: Listing,
    message: str,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Create a single outreach message with a pre-generated message (e.g. from AI agent).

    Returns the row id if created, or None if skipped (already exists / already sent).
    """
    conn = get_connection(db_path)
    try:
        host = listing.host_name or "Host"
        place = listing.title or "your place"
        location = listing.location or "your area"

        existing = conn.execute(
            "SELECT id FROM outreach_messages WHERE search_id = ? AND listing_id = ?",
            (search_id, listing.id),
        ).fetchone()
        if existing:
            return existing["id"]

        if has_sent_outreach_to_listing(listing.id, db_path):
            return None

        cursor = conn.execute(
            """INSERT INTO outreach_messages
               (search_id, listing_id, host_name, place_name, location, message, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                search_id,
                listing.id,
                host,
                place,
                location,
                message,
                OutreachStatus.PENDING.value,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# --- Dismissed Threads (Negotiation) ---


def dismiss_thread(
    thread_id: str,
    host_name: str = "",
    reason: str = "",
    db_path: Optional[str] = None,
) -> None:
    """Mark a chat thread as dismissed so the negotiation agent skips it in future runs."""
    if not (thread_id or "").strip():
        return
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_threads (thread_id, host_name, reason) VALUES (?, ?, ?)",
            (thread_id.strip(), host_name, reason),
        )
        conn.commit()
    finally:
        conn.close()


def is_thread_dismissed(thread_id: str, db_path: Optional[str] = None) -> bool:
    """Return True if a thread was previously dismissed."""
    if not (thread_id or "").strip():
        return False
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM dismissed_threads WHERE thread_id = ? LIMIT 1",
            (thread_id.strip(),),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_dismissed_thread_ids(db_path: Optional[str] = None) -> set[str]:
    """Return all dismissed thread IDs as a set for fast lookup."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT thread_id FROM dismissed_threads").fetchall()
        return {r["thread_id"] for r in rows}
    finally:
        conn.close()


def get_outreach_messages(
    search_id: int, db_path: Optional[str] = None
) -> list[OutreachMessage]:
    """Get all outreach messages for a search."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM outreach_messages WHERE search_id = ? ORDER BY id",
            (search_id,),
        ).fetchall()

        return [
            OutreachMessage(
                id=row["id"],
                search_id=row["search_id"],
                listing_id=row["listing_id"],
                host_name=row["host_name"],
                place_name=row["place_name"],
                location=row["location"],
                message=row["message"],
                status=OutreachStatus(row["status"]),
                error=row["error"] or "",
                sent_at=row["sent_at"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def update_outreach_status(
    message_id: int,
    status: OutreachStatus,
    error: str = "",
    db_path: Optional[str] = None,
) -> None:
    """Update the status of an outreach message."""
    conn = get_connection(db_path)
    try:
        if status == OutreachStatus.SENT:
            conn.execute(
                "UPDATE outreach_messages SET status = ?, error = ?, sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status.value, error, message_id),
            )
        else:
            conn.execute(
                "UPDATE outreach_messages SET status = ?, error = ? WHERE id = ?",
                (status.value, error, message_id),
            )
        conn.commit()
    finally:
        conn.close()


# --- Global outreach send rate (sliding window, all searches / CLI runs) ---


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
