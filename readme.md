# 🏠 Airbnb Automate

A simple app to search Airbnb listings by location, store results in a database, and **automatically outreach to hosts** with personalized messages — all from a clean web UI.

## 🚀 Quick Start

### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# Recommended for Airbnb login (uses your installed Google Chrome; OAuth works better)
playwright install chrome
```

Set `PLAYWRIGHT_CHANNEL=chrome` in `.env` when using the Chrome channel.

### 2. Configure (Optional)

```bash
cp .env.example .env
# Edit .env if you want to change port, database path, or message template
```

### 3. Run — Web UI

```bash
python run.py
# Open http://localhost:5000
```

### 4. Run — CLI (Autopilot Mode) 🤖

For a fully hands-off experience, use the CLI script. It scrapes listings and sends outreach invites automatically.

```bash
# One-time run: 3 invites each to multiple locations
python cli.py --locations "Himachal Pradesh, India" "Bali, Indonesia" "Manali, India" "Ladakh, India"

# Default: flexible trip (1 week), headless browser — 5 invites, price filters
python cli.py --locations "Goa, India" "Pondicherry, India" \
              --invites 5 --flex-duration 1 --flex-duration-unit week \
              --min-price 20 --max-price 120

# Fixed calendar dates
python cli.py --locations "Goa, India" --date-mode fixed \
              --checkin 2026-07-01 --checkout 2026-07-07

# Run on autopilot every 4 hours (Ctrl+C to stop)
python cli.py --locations "Himachal Pradesh, India" "Bali, Indonesia" "Manali, India" "Ladakh, India" --schedule

# Dry run: scrape only, no messages sent
python cli.py --locations "Goa, India" --dry-run

# Debug only: show the browser (CLI runs headless by default)
python cli.py --locations "Goa, India" --no-headless
```

**Important:** You must log in to Airbnb **once** before using the CLI for outreach. Either:
- Use the web UI (`python run.py` → click "🔐 Login to Airbnb"), or
- Start Chrome with `--remote-debugging-port` and set `CHROME_CDP_URL` in `.env` (see `.env.example`)

The CLI reuses the same persistent browser profile as the web UI.

#### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--locations` | One or more Airbnb locations (required) | — |
| `--invites` | Outreach invites per location | 3 |
| `--schedule` | Repeat every 4 hours | off |
| `--interval` | Custom schedule interval in seconds | 14400 (4h) |
| `--date-mode` | `flexible` (trip length) or `fixed` (calendar dates) | `flexible` |
| `--flex-duration` | Trip length in flexible mode | `1` |
| `--flex-duration-unit` | `day` (nights), `week`, or `month` | `week` |
| `--checkin` | Fixed mode: check-in (YYYY-MM-DD) | — |
| `--checkout` | Fixed mode: check-out (YYYY-MM-DD) | — |
| `--guests` | Number of guests | 2 |
| `--min-price` | Minimum price per night | — |
| `--max-price` | Maximum price per night | — |
| `--message` | Custom message template | Built-in |
| `--dry-run` | Scrape only, skip outreach | off |
| `--no-headless` | Show the browser (default is headless) | off |
| `-v, --verbose` | Debug logging | off |

That's it! The landing page lets you enter a location and optional preferences (dates, guests, price range). Hit search, and the app scrapes Airbnb and shows you the results.

## 📋 How It Works

1. **Enter a location** on the landing page (e.g., "Goa, India")
2. **Add optional preferences** — flexible trip length (nights / weeks / months) or fixed check-in/out, plus guests and price range
3. **Hit Search** — the app scrapes Airbnb listings matching your criteria
4. **View results** — listings are saved to the database and displayed in the UI
5. **Login to Airbnb** — click "🔐 Login to Airbnb" (one-time step, session is saved)
6. **Start Outreach** — click the outreach button to send personalized messages to all hosts
7. **Track progress** — watch messages get sent in real-time on the outreach status page

### 📨 Outreach Flow

The outreach system automates sending personalized messages to Airbnb hosts:

1. **Login first** — click **"🔐 Login to Airbnb"** in the navbar or on the results page. A browser opens; log in normally (email, Google, Apple — all work). Your session is saved in a persistent browser profile (`data/airbnb_browser_profile/`).
2. **Start outreach** — click **"🚀 Start Outreach"** on the results page. The app reuses your saved session — no login prompt during messaging.
3. The app visits each listing, clicks "Contact Host", types your personalized message, and sends it.
4. Track sent/pending/failed status in real-time.

> **Why a separate login step?** Airbnb blocks automated logins. By logging in once in a dedicated browser, your session persists on disk. The **search** and **outreach** steps share the same Playwright session (see `app/browser_session.py`); a backup copy of cookies is also written to `data/browser_state.json` after a successful login.

**If login never “sticks” or search opens a blank logged-out browser:** (1) Use **`PLAYWRIGHT_CHANNEL=chrome`** and `playwright install chrome`. (2) **Or** use **CDP**: start Chrome with `--remote-debugging-port` and a dedicated `--user-data-dir`, log in to Airbnb in that window, leave Chrome open, and set `CHROME_CDP_URL` in `.env` so the app attaches to *your* browser instead of launching a new one. Full steps are in `.env.example`.

The default message introduces you as a content creator offering to create content in exchange for stays. You can customize the message template from the UI before starting outreach.

## 🏗 Project Structure

```
airbnb-automate/
├── run.py                  # Entry point — web UI
├── cli.py                  # Entry point — CLI with scheduler (autopilot)
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variables template
│
├── app/                    # Core application
│   ├── config.py           # Configuration (DB path, message template)
│   ├── models.py           # Data models (Search, Listing, OutreachMessage)
│   ├── database.py         # SQLite database layer
│   ├── browser_session.py  # Shared Playwright session (search + login + outreach)
│   ├── scraper.py          # Airbnb scraper (Playwright)
│   └── outreach.py         # Host outreach automation (Playwright)
│
├── web/                    # Flask web app
│   ├── app.py              # Routes (home, search, results, outreach)
│   ├── static/style.css    # Styles
│   └── templates/          # HTML templates
│       ├── base.html
│       ├── home.html
│       ├── results.html
│       └── outreach.html
│
└── tests/                  # Test suite
    ├── test_database.py
    ├── test_cli.py
    └── test_scraper.py
```

## ⚙️ Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `FLASK_PORT` | App port | 5000 |
| `FLASK_DEBUG` | Debug mode | false |
| `FLASK_SECRET_KEY` | Session secret | dev-secret-key |
| `DATABASE_PATH` | SQLite DB path | data/airbnb_automate.db |
| `HEADLESS` | Run browser headless (scraping only) | true |
| `PLAYWRIGHT_CHANNEL` | Use installed `chrome` or `msedge` instead of bundled Chromium (helps if OAuth login fails) | (bundled Chromium) |
| `BROWSER_USER_DATA_DIR` | Persistent profile path for login sessions; set to `none` to disable | `data/airbnb_browser_profile` |
| `BROWSER_USER_AGENT` | Force a custom User-Agent (rarely needed) | (browser default) |
| `OUTREACH_MESSAGE` | Custom outreach message template | Built-in template |

## 🧪 Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

## ⚠️ Notes

- **Airbnb ToS**: Automated scraping and messaging may violate Airbnb's Terms of Service. Use responsibly.
- **Browser Required**: The scraper uses Playwright with Chromium. Run `playwright install chromium` after installing dependencies.
- **Login Required for Outreach**: Click **"Login to Airbnb"** in the web UI before starting outreach. The app stores your session in a persistent browser profile at `data/airbnb_browser_profile/`. If Google/Apple OAuth does not work in the bundled Chromium, set `PLAYWRIGHT_CHANNEL=chrome` in `.env` to use your installed Google Chrome instead.
