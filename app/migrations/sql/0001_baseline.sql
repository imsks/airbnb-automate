-- Baseline: the v1 schema. Idempotent, so existing databases adopt the
-- migration registry without any table being recreated.

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

CREATE INDEX IF NOT EXISTS idx_listings_search ON listings(search_id);
CREATE INDEX IF NOT EXISTS idx_outreach_search ON outreach_messages(search_id);
CREATE INDEX IF NOT EXISTS idx_outreach_listing ON outreach_messages(listing_id);
CREATE INDEX IF NOT EXISTS idx_outreach_send_log_sent_at ON outreach_send_log(sent_at);
