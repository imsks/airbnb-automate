# 🏠 Airbnb Automate

An agent office that finds places to stay across India, writes a personal message to each host, negotiates a content-for-stay collaboration, and hands you the deals that are ready to book.

You supply the card. Everything up to that point runs on its own.

---

## How it works

A **Planner** turns a campaign goal ("six months across North India, Nov–Apr") into jobs on a durable queue. A fixed roster of specialists drains it. Roles never change — the *work* is what multiplies.

| Agent | Does | Lives in |
|---|---|---|
| **Planner** | Breaks the goal into jobs, keeps the pipeline fed | [app/agent/planner.py](app/agent/planner.py) |
| **Scout** | Researches a place month by month — seasonality, connectivity, cost, events | [app/agent/scout.py](app/agent/scout.py) |
| **Router** | Orders places into a route that makes geographic sense | [app/agent/router.py](app/agent/router.py) |
| **Prospector** | Scrapes listings, then the detail page for real context | [app/listing_detail.py](app/listing_detail.py) |
| **Analyst** | Scores which hosts are actually likely to say yes | [app/agent/analyst.py](app/agent/analyst.py) |
| **Scribe** | Writes the opening message from the host's own words | [app/agent/scribe.py](app/agent/scribe.py) |
| **Closer** | Negotiates across rounds, sends replies, extracts agreed terms | [app/agent/closer.py](app/agent/closer.py) |
| **Warden** | Blocks any message that breaks your rules — deterministic, not a prompt | [app/warden.py](app/warden.py) |
| **Chronicler** | Builds the brief and the dashboard | [app/agent/chronicler.py](app/agent/chronicler.py) |

The agents run **fully autonomously up to Airbnb's payment wall**. They negotiate, agree terms, and stop — because only you can pay.

### The pipeline

```
campaign goal
  → research a place        (Scout)
  → order the route         (Router)
  → discover listings       (Prospector)
  → enrich from detail page (Prospector)
  → score the lead          (Analyst)
  → write + send outreach   (Scribe → Warden → send budget)
  → host replies            (inbox sync)
  → negotiate               (Closer → Warden → send budget)
  → extract agreed terms    (Closer)
  → READY TO BOOK           ← you take over here
```

### Every host relationship is a Deal

```mermaid
stateDiagram-v2
    [*] --> discovered
    discovered --> qualified
    qualified --> contacted
    contacted --> host_replied
    host_replied --> negotiating
    negotiating --> negotiating
    negotiating --> terms_agreed
    terms_agreed --> ready_to_book
    ready_to_book --> booked: you pay
    booked --> stayed
    stayed --> content_delivered
    contacted --> stale: no reply
    stale --> host_replied
    negotiating --> rejected
    negotiating --> needs_human: Warden blocked
    needs_human --> negotiating
```

Every transition is an append-only event, so the dashboard funnel is derived from history rather than guessed at.

---

## 🚀 Quick Start

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# Recommended — real Chrome makes Airbnb's OAuth login work reliably
playwright install chrome
```

Set `PLAYWRIGHT_CHANNEL=chrome` in `.env` when using the Chrome channel.

### 2. Configure

```bash
cp .env.example .env
```

At minimum set an LLM key (`GOOGLE_API_KEY` for the default Gemini provider). Then review the **guardrail** block — with full autonomy those values are the only thing between the model and a commitment you have to honour.

### 3. Log in to Airbnb, once

Airbnb blocks automated sign-in, so you do it by hand and the session persists in
a Chrome profile at `data/airbnb_browser_profile/` (with a cookie backup at
`data/browser_state.json`). The worker reuses it for every scrape and send.

```bash
make login      # opens a browser — sign in, then close it
make session    # check the session is still live
```

If the login never "sticks" or the browser opens logged out:

1. Set `PLAYWRIGHT_CHANNEL=chrome` and run `playwright install chrome`, **or**
2. Start Chrome yourself with `--remote-debugging-port` and a dedicated
   `--user-data-dir`, log in to Airbnb there, leave it open, and set
   `CHROME_CDP_URL` in `.env` so the app attaches to *your* browser instead of
   launching one.

### 4. Run it

```bash
make start
```

That's the whole thing. It runs the API and the worker together and opens the
dashboard at <http://127.0.0.1:8000>, where you create the campaign and watch
progress. Ctrl+C stops both.

The worker is still the only thing that touches the browser — the API just
writes rows to the `jobs` table. They share an event loop locally; run them as
separate processes with `make api` and `make worker` when the worker needs to
live somewhere else.

### 5. Create a campaign in the UI

Fill in the name, your starting city, the months you're free, and the
destinations to consider (pre-filled from [locations.md](locations.md)). Hit
**Create campaign & start work** and the Planner takes over.

Everything after that is on the page: live agent activity, the pipeline funnel,
what's ready to book, what needs you, the job queue, and LLM spend. It refreshes
every 10 seconds.

---

## Daily use

Open the dashboard. The buttons cover the common actions — freeze/resume
sending, sync the inbox, plan work now.

If you prefer the terminal:

```bash
make brief      # what happened, what needs you
make status     # queue depth + sends left in the window
make freeze     # panic button — takes effect mid-run
make resume

python manage.py itinerary   # the route the Router planned
```

`make brief` output:

```
  Closes / 100 messages : 12.5
  Reply rate            : 31.2%
  Messages sent         : 32
  Sends left in window  : 3/5

  READY TO BOOK (2) — needs your card
    · Asha — Sea Breeze Villa (2026-11 → 2026-12)
    · Ravi — Hill Hut (window TBC)

  NEEDS A HUMAN (1)
    · Meera — [price_ceiling] agrees to 8000 per night but only free stays…

  ALERTS
    ! The Warden blocked 1 draft(s) in the last 24h.
```

---

## 🛡 Guardrails

The agents send without asking you. So the rules live in **code**, not in the prompt — a system prompt saying "never promise specific dates" is a suggestion a model can talk itself out of. The Warden reviews the **final text** after generation and either allows it or blocks it.

A blocked draft parks its deal in `needs_human` and shows up on the dashboard. It fires if the message would:

| Rule | Example that gets blocked |
|---|---|
| Exceed your per-night ceiling | "I'd be happy to pay ₹8000 per night" |
| Commit to specific dates | "Let's lock in Dec 12 to Dec 19" |
| Over-promise deliverables | "I'll make 10 reels for you" |
| Leak contact details | "Call me on 9876543210" |
| Move off-platform | "Let's do a direct booking over WhatsApp" |
| Inflate your reach | "I have 500k followers" |
| Exceed the per-thread reply cap | a 5th agent reply on one conversation |

Quoting a host's price back to them is **not** a violation — the price rule only fires in a committing context ("pay", "agree", "happy to"). The Warden never rewrites a draft; it allows or refuses.

Set the values in `.env` (seeds the policy on first run) or change them live:

```bash
curl -X POST localhost:8000/api/policy \
  -H 'content-type: application/json' \
  -d '{"max_price_per_night": 2500, "max_agent_replies_per_thread": 3}'
```

### Kill switch

One flag freezes every outbound message. The worker re-reads it immediately before each send, and again *after* the rate-limit wait — which can last hours — so a freeze takes effect mid-run.

```bash
make freeze     # or: python manage.py freeze --reason "account looks flagged"
make resume
```

---

## 🐢 Send budget

Airbnb blocks bulk messaging. v1 only counted first-touch outreach, so auto-sending negotiation replies would have silently doubled the real rate against an unchanged cap.

In v2 **outreach and negotiation share one sliding window**. Negotiation isn't exempt — it just gets higher queue priority, because a warm thread is worth far more than a cold message. Defaults: **5 sends per 3 hours**, with ~120s spacing and jitter between attempts.

Two consequences worth knowing:

- The **Planner applies backpressure** — it won't queue sends it has no budget to deliver. Queueing a hundred against a five-send window just hides the constraint.
- Because volume is capped, the dashboard leads with **deals closed per 100 messages**. The only way to improve is to convert better: research harder, score more selectively, write a better message.

If Airbnb shows its in-app limit banner, outreach stops, marks the rest skipped, and moves on.

Tune with `OUTREACH_MAX_SENDS_PER_WINDOW`, `OUTREACH_RATE_WINDOW_SECONDS`, and `OUTREACH_INTER_MESSAGE_DELAY_SECONDS`.

---

## 🏗 Project Structure

```
airbnb-automate/
├── manage.py               # Entry point — `start` runs everything; login / brief / freeze
├── Makefile                # make start / login / brief / status / freeze / test
├── locations.md            # One destination per line
│
├── app/
│   ├── worker.py           # Leases jobs, dispatches them, owns the browser
│   ├── jobs.py             # Durable queue: enqueue / lease / retry / idempotency
│   ├── warden.py           # Deterministic guardrail validator
│   ├── policy.py           # Guardrail config + kill switch (DB-backed, live)
│   ├── send_budget.py      # One shared send window for every channel
│   ├── logging_config.py   # Readable console output + the UI activity feed
│   │
│   ├── deals.py            # Deal repo + state machine + message log
│   ├── leads.py            # Lead repo: enrichment + collab-fit score
│   ├── territories.py      # Places, research profiles, saturation counters
│   ├── campaigns.py        # Campaign goals and planned itineraries
│   │
│   ├── inbox.py            # Inbox sync + in-thread replies
│   ├── thread_linking.py   # Links a sent message to the thread it became
│   ├── listing_detail.py   # Detail-page scrape (the "public context")
│   ├── scraper.py          # Search-results scrape
│   ├── outreach.py         # Airbnb login + listing-page messaging (Playwright)
│   ├── browser_session.py  # Persistent Chrome profile / CDP attach
│   ├── database.py         # SQLite layer
│   ├── models.py           # Deal, Lead, Job, Territory, Campaign, …
│   ├── config.py           # Env configuration
│   │
│   ├── migrations/         # Numbered SQL, applied in order and recorded
│   │   └── sql/            # 0001 baseline · 0002 agent office · 0003 backfill · 0004 drop legacy
│   │
│   ├── agent/
│   │   ├── planner.py      # Decomposes goals into jobs
│   │   ├── scout.py        # Destination research
│   │   ├── router.py       # Route + month assignment
│   │   ├── analyst.py      # Lead scoring
│   │   ├── scribe.py       # Opening messages
│   │   ├── closer.py       # Negotiation + terms extraction
│   │   ├── chronicler.py   # Brief, funnel, north-star metric
│   │   ├── prompts_v2.py   # Prompts built from the policy fact sheet
│   │   ├── runs.py         # Token / cost / latency ledger
│   │   ├── llm.py          # Provider abstraction (Gemini / OpenAI / Perplexity)
│   │   └── chat_reader.py  # Inbox scraping
│   │
│   └── api/
│       ├── main.py         # FastAPI routes
│       └── dashboard.py    # Server-rendered dashboard
│
├── data/                   # Runtime data (gitignored) — DB, Chrome profile, logs
└── tests/                  # Includes a Warden red-team corpus and an e2e walk
```

---

## 📍 `locations.md`

One destination per line in the project root; lines starting with `#` are comments.
The campaign form in the dashboard pre-fills from this file, and
`manage.py campaign` reads it when you don't pass `--places` / `--places-file`.

These are only *candidates* — the Scout researches each one and the Router decides which actually make the itinerary, and in which month.

---

## 🔗 Flexible search URLs

Flexible searches use Airbnb's **structured** explore params: `refinement_paths[]`, `flexible_trip_dates[]` (lowercase English months), `monthly_start_date` / `monthly_length` / `monthly_end_date`, `flexible_trip_lengths[]` (`one_week`, `one_month`, `weekend_trip`), and `price_filter_num_nights`. Path slugs follow **“City, Region” → `City--Region`**. Tune with **`AIRBNB_BASE_URL`** (e.g. `https://www.airbnb.co.in`) and **`FLEX_TRIP_MONTHS_COUNT`**.

---

## ⚙️ Configuration

### Guardrails

These seed the `policy` table on first run. After that the dashboard is the source of truth, and changes apply **without restarting the worker**.

| Variable | Description | Default |
|---|---|---|
| `MAX_PRICE_PER_NIGHT` | Most an agent may agree to pay. `0` = free stays only; any paid counter-offer is escalated to you | `0` |
| `PRICE_CEILING_CURRENCY` | Currency the ceiling is in | `INR` |
| `ALLOWED_DELIVERABLES` | Comma-separated. The number in each entry is the cap, so "2 Instagram reels" blocks a draft offering three | 2 reels, 10 photos, 1 review, stories |
| `MAX_AGENT_REPLIES_PER_THREAD` | Replies one thread may get before it is parked for you | `4` |
| `CREATOR_NAME` / `CREATOR_ROLE` | Who the agent says you are | Sachin / founder, The Boring Education |
| `CREATOR_FOLLOWERS` | The largest reach claim permitted | `150k+ combined` |
| `CREATOR_HANDLES` | The only handles an agent may name | `@theboringfounder, @theboringeducation` |

### Rate limiting

| Variable | Description | Default |
|---|---|---|
| `OUTREACH_MAX_SENDS_PER_WINDOW` | Shared cap across outreach **and** negotiation | `5` |
| `OUTREACH_RATE_WINDOW_SECONDS` | Sliding window length | `10800` (3h) |
| `OUTREACH_INTER_MESSAGE_DELAY_SECONDS` | Minimum pause between attempts | `120` |

### Browser & search

| Variable | Description | Default |
|---|---|---|
| `DATABASE_PATH` | SQLite path | `data/airbnb_automate.db` |
| `AIRBNB_BASE_URL` | Origin for search URLs | `https://www.airbnb.com` |
| `FLEX_TRIP_MONTHS_COUNT` | Consecutive months in `flexible_trip_dates[]` | `3` |
| `HEADLESS` | Run the browser headless | `true` |
| `PLAYWRIGHT_CHANNEL` | Use installed `chrome` / `msedge` — fixes OAuth login | bundled Chromium |
| `BROWSER_USER_DATA_DIR` | Persistent profile path; `none` to disable | `data/airbnb_browser_profile` |
| `CHROME_CDP_URL` | Attach to your own running Chrome instead of launching one | — |
| `BROWSER_USER_AGENT` | Force a custom User-Agent (rarely needed) | browser default |

### LLM

| Variable | Description | Default |
|---|---|---|
| `LLM_PROVIDER` | `gemini`, `openai`, or `perplexity` | `gemini` |
| `LLM_TEMPERATURE` | Sampling temperature | `0.7` |
| `GOOGLE_API_KEY` / `GEMINI_MODEL` | Gemini credentials | — / `gemini-2.5-flash` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI credentials | — / `gpt-4o-mini` |
| `PERPLEXITY_API_KEY` / `PERPLEXITY_MODEL` | Perplexity credentials | — / `sonar-pro` |

Every LLM call is logged to `agent_runs` with tokens, latency and cost, attributed to an agent, a prompt version and a deal. The dashboard shows spend per agent and **reply rate per prompt version** — the highest-leverage thing to tune once you have volume.

---

## 🧪 Testing

```bash
pip install -r requirements-test.txt
make test          # or: python -m pytest tests/ -q
```

Worth knowing what's covered, because this system messages real people as you:

- **Warden red team** — adversarial drafts that leak a phone number, promise "Dec 12", offer ₹9000/night or claim 500k followers must all be blocked.
- **Idempotency** — a retried send job produces exactly one message and consumes exactly one budget slot.
- **Shared budget** — outreach and negotiation interleaved never exceed the window.
- **Kill switch** — flipping it mid-run stops the next send at two independent layers.
- **State machine** — illegal transitions are refused and leave no event behind.
- **End to end** — one deal walks `discovered → ready_to_book` with every LLM and browser call mocked.

---

## ⚠️ Notes

- **Airbnb ToS** — automated scraping and messaging may violate Airbnb's Terms of Service. You are messaging real hosts as yourself; keep the volume honest and the claims true. The credential fact sheet exists so an agent cannot overstate your reach on your behalf.
- **Login required** — the worker cannot log in for you. Run `make login` once; the session lives in `data/airbnb_browser_profile/`. If Google/Apple OAuth fails in bundled Chromium, set `PLAYWRIGHT_CHANNEL=chrome`.
- **No push notifications** — the dashboard is the only place a problem surfaces, so the brief leads with the two queues that need you and raises an anomaly banner when the system has gone unexpectedly quiet.
- **Cloud** — running this on a server needs a persistent VM, a headful browser under Xvfb, a residential India exit IP, an encrypted profile volume and a one-time remote login handoff. That's a separate project; the code is written to be liftable (all paths from config, browser confined to the worker) but the deployment is not built.

