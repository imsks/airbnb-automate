# PRD — Multi-portal context & vector memory for the standing deal office

> Status: `ready-for-agent`
> Owner: standing deal office
> Depends on: the deferred SQLite → Postgres migration (pgvector needs Postgres). A
> numpy/sqlite fallback backend keeps local dev and the test suite green without Postgres.

## Problem Statement

The office writes and negotiates outreach off a single source of truth: the Airbnb
listing plus whatever the Scout researched about the territory. That is thin. Every
draft leans on the same handful of fields — title, price, rating, a few review
excerpts — so messages read like something any listing could have received, and the
Closer negotiates without knowing whether the host is actually responsive, whether the
"long-stay discount" is real, or how this stay compares to others in the same town.

At the same time the office has no memory. It has already sent hundreds of messages and
watched which openings earned replies and which closed deals, but none of that comes
back at drafting time. It re-learns the same lessons per listing and repeats angles that
never land.

I want the agents to pull richer context from other portals (Booking.com and friends)
to corroborate and deepen what they know about a stay, and I want the office to remember
what has worked so every new draft stands on the best of everything it has learned —
without me configuring anything and without ever letting another portal touch the Airbnb
session or message a host.

## Solution

Two capabilities, both feeding the existing draft-then-send pipeline:

1. **Portal context (read-only).** For a listing the office already discovered on
   Airbnb, a portal connector looks the same property up on Booking.com (and, later,
   other portals), matches it to the Airbnb listing, and pulls a normalized bundle of
   corroborating signals — real guest reviews, amenities, a price band, host
   responsiveness. Portals are strictly a **context** source. They never discover new
   leads and never message anyone. Airbnb stays the only place we identify hosts and
   send.

2. **Vector memory (context recall).** A semantic store remembers listing descriptions,
   reviews, host bios, territory research, and — crucially — the outreach we sent, the
   host replies we got, and the messages that closed deals. At drafting and negotiation
   time the Scribe and Closer recall the most relevant chunks (the strongest angle for
   this kind of stay, the openings that earned replies in similar towns) and write from
   them.

Both are assembled into one **ContextPack** that the Scribe and Closer consume in place
of today's thin `_local_colour` line. Everything runs on the Office side of the split
(network fetches and embeddings, no Airbnb browser), stays inside the Warden's
guardrails, and needs zero new configuration from me.

## User Stories

1. As the office, I want to enrich an Airbnb-discovered listing with Booking.com signals, so that my outreach cites facts a generic message could not.
2. As the office, I want portals used only for context, so that discovery and messaging stay on Airbnb where the host identity lives.
3. As the office, I want a portal connector with one uniform interface, so that adding Agoda or MakeMyTrip later is a new implementation, not a rewrite.
4. As the office, I want to match a Booking.com property to the right Airbnb listing with a confidence score, so that I never attach another property's reviews to the wrong host.
5. As the office, I want low-confidence matches dropped rather than guessed, so that a wrong match never poisons a draft.
6. As the office, I want portal context normalized into one shape regardless of source, so that the Scribe does not care which portal a fact came from.
7. As the Scout, I want portal reviews folded into a territory's content angles, so that the angles reflect what guests actually praise across portals.
8. As the Scribe, I want a ContextPack that already merges enrichment, portal facts, and recalled memory, so that I write from one grounded structure instead of re-fetching pieces.
9. As the Scribe, I want to recall the openings that earned replies for similar stays, so that my first message uses a proven angle rather than a fresh guess.
10. As the Closer, I want to recall how comparable negotiations went, so that I counter a host's ask with terms that have worked before.
11. As the office, I want the vector store to remember every outreach I send and every host reply I receive, so that my memory grows with each thread.
12. As the office, I want to remember which messages preceded a closed deal, so that winning angles are weighted higher in recall.
13. As the office, I want recall filtered by kind (territory, review, prior outreach, host reply), so that I can ask for the right sort of context at each step.
14. As the office, I want recall scoped so the pack stays within a token budget, so that prompts do not bloat and cost does not spike.
15. As the office, I want the embedder to be provider-agnostic, so that I can swap OpenAI, Gemini, or a local model without touching recall.
16. As the office, I want a deterministic fake embedder in tests, so that recall behavior is testable without a network call or an API key.
17. As the office, I want a Postgres/pgvector backend in production and a numpy/sqlite fallback locally, so that the same recall interface works in both places and the test suite needs no Postgres.
18. As the office, I want portal pulls and embedding to run as durable jobs on the Office role, so that they retry and never block the API or borrow the Airbnb session.
19. As the office, I want a listing enriched by a portal only after it is discovered on Airbnb, so that portal work is demand-driven and never speculative.
20. As the office, I want portal context refreshed on a sane cadence, not on every draft, so that I do not re-scrape the same property needlessly.
21. As the office, I want portal fetches that fail to degrade gracefully, so that a Booking.com outage still lets me draft from Airbnb context alone.
22. As the Warden, I want portal-sourced facts to pass the same guardrails as everything else, so that a scraped review cannot smuggle contact info or off-platform terms into a draft.
23. As the operator, I want to see on the dashboard which listings have corroborating portal context, so that I trust the message the office wrote.
24. As the operator, I want portal and memory work visible in the Office loop feed, so that I can watch it happen from my phone.
25. As the operator, I want to configure nothing about portals or embeddings, so that the office stays as hands-off as the rest of the system.
26. As the office, I want portal scraping to respect polite rate limits and identify itself honestly, so that context gathering does not get the setup blocked.
27. As the office, I want to store a raw payload plus a normalized view of each portal pull, so that I can re-normalize later without re-scraping.
28. As the office, I want entity matching to combine cheap heuristics (name, geo, capacity) with embedding similarity, so that matching is both fast and robust.
29. As the office, I want memory writes to be idempotent per source chunk, so that re-running enrichment does not duplicate embeddings.
30. As the office, I want recall to prefer recent and higher-outcome memories on ties, so that stale or losing angles fade over time.

## Implementation Decisions

**Scope guardrail (from prior decisions).** Portals are context-only. They must not
create `Lead`s, must not drive discovery, and must never send or receive host messages.
The Courier remains the sole owner of the Airbnb browser session; all portal and
embedding work lives on the Office role and uses plain network fetches, not the Airbnb
session.

**Deep modules to build.** Each has a small interface that hides a lot and rarely
changes, so each is testable in isolation:

- **`PortalConnector`** — the uniform portal interface: `fetch_context(query) ->
  PortalContext`. `PropertyQuery` carries the normalized identity of an Airbnb listing
  (title, location, lat/lng if known, host name, capacity, price band). `PortalContext`
  is the normalized result: source portal, external id/url, reviews, amenities, price
  band, host responsiveness signals, a raw payload, and a `fetched_at`. `BookingConnector`
  is the first implementation; the connector registry makes additional portals additive.
- **`EntityMatcher`** — given an Airbnb `Listing`/`Lead` and a portal candidate set,
  returns the best match with a confidence in `[0,1]`, combining heuristics (name
  similarity, geo distance, guest capacity, price proximity) with embedding similarity.
  A configurable threshold decides match vs. drop; below threshold returns "no match"
  rather than a guess.
- **`Embedder`** — provider-agnostic `embed(texts) -> vectors`, selected the same way
  `agent/llm.py` selects a chat model. Ships a deterministic `FakeEmbedder` for tests.
- **`VectorStore` (ContextMemory)** — `remember(chunks)` (idempotent upsert keyed by a
  stable chunk id) and `recall(query, k, filter) -> [Chunk]` (cosine similarity with
  optional `source_type`/territory/portal filters, recency and outcome as tie-breakers).
  Two backends behind the one interface: **pgvector** in production and a **numpy/sqlite
  fallback** for local dev and tests. A contract test asserts both backends rank a
  fixture set identically.
- **`ContextPack` builder** — merges `Lead` enrichment + `PortalContext` + recalled
  memory into a single structure the Scribe and Closer consume, trimmed to a token
  budget. Replaces today's `_local_colour` seam with a richer, still-deterministic
  assembler.

**Pipeline integration.**

- New Office job types, leased only by the Office role: `PULL_PORTAL_CONTEXT` (fetch +
  match + persist portal context for a discovered listing) and `INDEX_CONTEXT` (embed and
  `remember` new/updated chunks). Both run at research/housekeeping priority, below
  drafting and sending.
- The Planner queues `PULL_PORTAL_CONTEXT` for enriched leads that lack fresh portal
  context, and `INDEX_CONTEXT` whenever a new context source appears (enrichment
  completes, an outreach is sent, a host reply lands, a deal closes).
- The Scribe's draft step builds a `ContextPack` (enrichment + portal facts + recall of
  winning angles for similar stays) and writes from it. The Closer builds a pack scoped
  to negotiation memory. Warden runs unchanged on the output; portal facts get no special
  pass.
- If portal context is missing or a match was dropped, the pack degrades to
  enrichment + memory only — drafting never blocks on a portal.

**Schema changes (Postgres, with fallback mirror).**

- `portal_context`: `id`, `listing_id` (FK), `portal`, `external_id`, `url`,
  `payload_raw jsonb`, `payload_normalized jsonb`, `match_confidence`, `fetched_at`.
  One current row per (listing, portal); refresh on a cadence, not per draft.
- `context_chunks`: `id`, `chunk_key` (stable, unique — drives idempotent upsert),
  `source_type` (`listing_desc` | `review` | `host_bio` | `territory_research` |
  `outreach_sent` | `host_reply` | `closed_deal_msg`), `ref_id`, `territory_id`,
  `portal`, `text`, `embedding vector(N)`, `outcome_weight`, `metadata jsonb`,
  `created_at`. pgvector index on `embedding`; the fallback backend stores vectors in a
  column and does cosine in-process.

**Configuration.** Nothing new required of the operator. Embedding provider/key reuse
the existing provider config pattern; portal connectors and the vector backend are
enabled by presence of config and otherwise no-op cleanly (like Telegram notify does
today). Absent Postgres, the fallback backend is used automatically.

**Dashboard.** The message card gains a small "corroborated by Booking.com" indicator
when portal context backed the draft, and `PULL_PORTAL_CONTEXT` / `INDEX_CONTEXT` show up
in the Office loop feed like any other job. No new controls.

## Testing Decisions

A good test here asserts **external behavior**, not internals: given fixed inputs
(saved portal HTML/JSON, a seeded set of memory chunks, a fake embedder), assert the
observable result — the normalized `PortalContext`, the match/no-match decision and
confidence, the recall ordering, the assembled `ContextPack`, and the fact that a draft
uses a recalled angle. No test should reach the network or a real embedding API.

Modules to test (all deep modules ship with tests, per the repo rule that every testable
line has a test):

- **`PortalConnector` / `BookingConnector`** — normalization against saved fixtures.
  Prior art: `chat_reader.py` was verified against real HTML snapshots, and the existing
  `scraper`/`listing_detail` tests parse fixture pages. Assert reviews, amenities, price
  band, and host signals are extracted; assert a malformed page degrades to an empty,
  valid `PortalContext` rather than raising.
- **`EntityMatcher`** — fixture pairs of true matches and near-miss non-matches; assert
  confidence crosses/does not cross the threshold and that low confidence yields "no
  match". Assert wrong-property candidates are rejected.
- **`Embedder`** — `FakeEmbedder` determinism; provider selection mirrors the
  `agent/llm.py` selection tests.
- **`VectorStore`** — with the fake embedder and the fallback backend, seed chunks and
  assert `recall` ranks the relevant chunk first and honors `source_type`/territory
  filters; assert idempotent upsert (re-`remember` does not duplicate). A contract test
  asserts pgvector and fallback return the same ordering on one fixture (pg test skipped
  when Postgres is unavailable, like other environment-gated tests).
- **`ContextPack` builder** — given a `Lead` + `PortalContext` + recalled chunks, assert
  the pack contains the expected angles, stays within the token budget, and degrades
  correctly when portal context is absent. Prior art: the existing Scribe context tests
  around `_local_colour`.
- **Scribe/Closer integration** — assert a draft/negotiation prompt includes a recalled
  winning angle and a corroborated portal fact (behavioral), and that Warden still
  blocks/rewrites exactly as today. Prior art: `test_single_send.py`,
  `test_e2e_pipeline.py`.
- **Planner** — assert `PULL_PORTAL_CONTEXT` is queued for enriched leads missing fresh
  portal context and `INDEX_CONTEXT` is queued on new sources, both Office-only. Prior
  art: `test_office_courier.py`, `test_worker_api.py` backpressure tests.

## Out of Scope

- Portals as a discovery or messaging channel — they are context-only, permanently.
- Auto-booking, live price monitoring, or rebooking on price drops.
- Any change to who owns the Airbnb session (Courier keeps it) or to the human-in-the-loop
  moments (ready-to-book, session dead, impossible host demand).
- The SQLite → Postgres migration itself — it is a prerequisite tracked separately; this
  PRD ships behind a fallback backend so it is not blocked on that migration.
- Re-ranking models or a learned scorer for recall — start with cosine + recency/outcome
  tie-breakers; a learned ranker is a later iteration.
- A new operator-facing configuration surface for portals or embeddings.

## Further Notes

- The winning-angle memory is the highest-leverage piece: weighting chunks that preceded
  a `READY_TO_BOOK`/`BOOKED` transition turns the office's own history into its best
  prompt. `outcome_weight` is set when a deal advances and used as a recall tie-breaker.
- Booking.com is the first connector because it has the deepest public review and
  amenity data for the India + international stays the office targets; the registry keeps
  Agoda/MakeMyTrip/Google additive.
- Politeness matters: portal scraping must rate-limit and identify honestly so context
  gathering does not get the setup flagged. Treat portal fetches as best-effort and
  cache aggressively via the `fetched_at` cadence.
- This PRD intentionally leaves the module list explicit so it can be corrected before
  build. If any deep module should be merged or split — or if some should ship without
  tests — flag it on this doc before implementation starts.
