-- Read-only context pulled from other portals (Booking.com, etc.) to corroborate
-- an Airbnb-discovered listing. Never a discovery or messaging channel.

CREATE TABLE IF NOT EXISTS portal_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    portal TEXT NOT NULL,
    external_url TEXT DEFAULT '',
    match_confidence REAL DEFAULT 0.0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (listing_id, portal)
);

CREATE INDEX IF NOT EXISTS idx_portal_context_listing ON portal_context(listing_id);
