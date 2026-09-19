-- Backfill: v1 outreach history becomes deals + messages, so the pipeline
-- starts with real data instead of an empty funnel.
--
-- Rows are restricted to listings that still exist: `deals` has a foreign key
-- to `listings`, and some historical outreach rows reference IDs that were
-- never persisted.

-- Seed territories from places already contacted.
INSERT OR IGNORE INTO territories (name, status)
SELECT DISTINCT TRIM(location), 'candidate'
  FROM outreach_messages
 WHERE TRIM(COALESCE(location, '')) != '';

-- One deal per listing, in the default (campaign_id = 0) bucket.
INSERT OR IGNORE INTO deals
    (listing_id, campaign_id, host_name, place_name, location, state, created_at)
SELECT om.listing_id,
       0,
       COALESCE(MAX(om.host_name), ''),
       COALESCE(MAX(om.place_name), ''),
       COALESCE(MAX(om.location), ''),
       CASE
           WHEN SUM(CASE WHEN om.status = 'sent' THEN 1 ELSE 0 END) > 0
               THEN 'contacted'
           WHEN SUM(CASE WHEN om.status = 'skipped' THEN 1 ELSE 0 END) = COUNT(*)
               THEN 'disqualified'
           ELSE 'qualified'
       END,
       MIN(om.created_at)
  FROM outreach_messages om
 WHERE EXISTS (SELECT 1 FROM listings l WHERE l.id = om.listing_id)
 GROUP BY om.listing_id;

-- Every deal needs an opening event or its history reads as a gap.
INSERT INTO deal_events (deal_id, from_state, to_state, reason, actor, created_at)
SELECT d.id, '', d.state, 'backfilled from outreach_messages', 'migration', d.created_at
  FROM deals d
 WHERE NOT EXISTS (SELECT 1 FROM deal_events e WHERE e.deal_id = d.id);

-- Carry across the message bodies, keyed back to the legacy row.
INSERT INTO messages
    (deal_id, direction, kind, body, status, error, created_at, sent_at, legacy_outreach_id)
SELECT d.id,
       'outbound',
       'outreach',
       om.message,
       CASE om.status
           WHEN 'sent' THEN 'sent'
           WHEN 'failed' THEN 'failed'
           WHEN 'skipped' THEN 'failed'
           ELSE 'pending'
       END,
       COALESCE(om.error, ''),
       om.created_at,
       om.sent_at,
       om.id
  FROM outreach_messages om
  JOIN deals d
    ON d.listing_id = om.listing_id AND d.campaign_id = 0
 WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.legacy_outreach_id = om.id);

UPDATE deals
   SET last_outbound_at = (
           SELECT MAX(m.sent_at) FROM messages m
            WHERE m.deal_id = deals.id AND m.status = 'sent'
       )
 WHERE last_outbound_at IS NULL;

-- Saturation counters the Research agent uses to avoid re-working a place.
UPDATE territories
   SET messages_sent = COALESCE((
           SELECT COUNT(*) FROM messages m
             JOIN deals d ON d.id = m.deal_id
            WHERE d.location = territories.name AND m.status = 'sent'
       ), 0);
