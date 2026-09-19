-- Drop the v1 tables. Their contents were copied into deals/messages by
-- migration 0003, which always runs first, so nothing is lost here.
--
-- `dismissed_threads` needs no backfill: "this thread is not worth replying to"
-- is now a deal state (rejected / disqualified) with an event explaining why,
-- rather than a side table the negotiator consulted on every scan.

DROP TABLE IF EXISTS dismissed_threads;
DROP INDEX IF EXISTS idx_outreach_search;
DROP INDEX IF EXISTS idx_outreach_listing;
DROP TABLE IF EXISTS outreach_messages;
