-- The agent office: territories, campaigns, leads, deals, messages, jobs,
-- agent runs and policy. `searches` / `listings` remain the raw scrape layer;
-- everything downstream of a scrape now hangs off a Deal.

-- --- Territories -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS territories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    country TEXT DEFAULT 'India',
    region TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    leads_discovered INTEGER DEFAULT 0,
    messages_sent INTEGER DEFAULT 0,
    deals_won INTEGER DEFAULT 0,
    last_discovered_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Scout output. One current row per territory; older rows kept for audit.
CREATE TABLE IF NOT EXISTS territory_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    territory_id INTEGER NOT NULL,
    summary TEXT DEFAULT '',
    seasonality_json TEXT DEFAULT '{}',
    connectivity_score REAL DEFAULT 0,
    connectivity_note TEXT DEFAULT '',
    content_score REAL DEFAULT 0,
    content_angles_json TEXT DEFAULT '[]',
    cost_band TEXT DEFAULT '',
    cost_score REAL DEFAULT 0,
    events_json TEXT DEFAULT '[]',
    caveats_json TEXT DEFAULT '[]',
    sources_json TEXT DEFAULT '[]',
    model TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    is_current INTEGER NOT NULL DEFAULT 1,
    researched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    FOREIGN KEY (territory_id) REFERENCES territories(id)
);

CREATE INDEX IF NOT EXISTS idx_territory_profiles_current
    ON territory_profiles(territory_id, is_current);

-- --- Campaigns -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    goal TEXT DEFAULT '',
    window_start TEXT DEFAULT '',
    window_end TEXT DEFAULT '',
    origin TEXT DEFAULT '',
    guests INTEGER DEFAULT 2,
    stay_nights INTEGER DEFAULT 7,
    max_price_per_night REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Router output: the ordered itinerary.
CREATE TABLE IF NOT EXISTS campaign_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    territory_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    target_month TEXT DEFAULT '',
    score REAL DEFAULT 0,
    rationale TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planned',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (campaign_id, seq),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (territory_id) REFERENCES territories(id)
);

-- --- Leads -----------------------------------------------------------------

-- campaign_id defaults to 0 rather than NULL: SQLite treats NULLs as distinct
-- in UNIQUE indexes, which would let the same listing be re-inserted forever.
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    campaign_id INTEGER NOT NULL DEFAULT 0,
    territory_id INTEGER,
    search_id INTEGER,
    description TEXT DEFAULT '',
    house_rules TEXT DEFAULT '',
    amenities_json TEXT DEFAULT '[]',
    review_excerpts_json TEXT DEFAULT '[]',
    host_bio TEXT DEFAULT '',
    host_response_rate TEXT DEFAULT '',
    host_is_superhost INTEGER DEFAULT 0,
    listing_age_months INTEGER,
    has_long_stay_discount INTEGER DEFAULT 0,
    instant_book INTEGER DEFAULT 0,
    detail_scraped_at TEXT,
    collab_fit_score REAL,
    score_breakdown_json TEXT DEFAULT '{}',
    score_rationale TEXT DEFAULT '',
    scored_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (listing_id, campaign_id),
    FOREIGN KEY (listing_id) REFERENCES listings(id)
);

CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(campaign_id, collab_fit_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_territory ON leads(territory_id);

-- --- Deals -----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    listing_id TEXT NOT NULL,
    campaign_id INTEGER NOT NULL DEFAULT 0,
    territory_id INTEGER,
    host_name TEXT DEFAULT '',
    place_name TEXT DEFAULT '',
    location TEXT DEFAULT '',
    listing_url TEXT DEFAULT '',
    thread_id TEXT,
    thread_url TEXT DEFAULT '',
    thread_linked_via TEXT DEFAULT '',
    state TEXT NOT NULL DEFAULT 'discovered',
    state_reason TEXT DEFAULT '',
    agent_reply_count INTEGER NOT NULL DEFAULT 0,
    follow_up_count INTEGER NOT NULL DEFAULT 0,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    agreed_price_per_night REAL,
    agreed_currency TEXT DEFAULT '',
    agreed_discount_pct REAL,
    agreed_window_start TEXT DEFAULT '',
    agreed_window_end TEXT DEFAULT '',
    agreed_nights INTEGER,
    agreed_deliverables_json TEXT DEFAULT '[]',
    terms_confidence REAL,
    booking_url TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (listing_id, campaign_id),
    FOREIGN KEY (lead_id) REFERENCES leads(id),
    FOREIGN KEY (listing_id) REFERENCES listings(id)
);

-- Partial index: many deals have no thread yet, but a linked thread is exclusive.
CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_thread
    ON deals(thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_deals_state ON deals(state);
CREATE INDEX IF NOT EXISTS idx_deals_campaign ON deals(campaign_id, state);

CREATE TABLE IF NOT EXISTS deal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    from_state TEXT DEFAULT '',
    to_state TEXT NOT NULL,
    reason TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (deal_id) REFERENCES deals(id)
);

CREATE INDEX IF NOT EXISTS idx_deal_events_deal ON deal_events(deal_id, id);

-- --- Messages --------------------------------------------------------------

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'outreach',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT DEFAULT '',
    blocked_reason TEXT DEFAULT '',
    agent TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    agent_run_id INTEGER,
    idempotency_key TEXT UNIQUE,
    external_ts TEXT DEFAULT '',
    legacy_outreach_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    FOREIGN KEY (deal_id) REFERENCES deals(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_deal ON messages(deal_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);

-- --- Jobs ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    run_after REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    idempotency_key TEXT UNIQUE,
    lease_until REAL,
    lease_owner TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    result_json TEXT DEFAULT '{}',
    campaign_id INTEGER,
    deal_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_claimable
    ON jobs(status, priority, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_deal ON jobs(deal_id);

-- --- Observability ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    job_id INTEGER,
    deal_id INTEGER,
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    prompt_version TEXT DEFAULT '',
    input_preview TEXT DEFAULT '',
    output_preview TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_deal ON agent_runs(deal_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent, created_at);

-- --- Policy ----------------------------------------------------------------

-- Runtime-tunable guardrails and the kill switch. Read immediately before
-- every send so a change takes effect without restarting the worker.
CREATE TABLE IF NOT EXISTS policy (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
