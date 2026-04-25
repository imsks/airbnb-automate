# 🏠 Airbnb Automate

A simple app to search Airbnb listings by location, store results in a database, and **automatically outreach to hosts** with personalized messages — all from a clean web UI.

## 🚀 Quick Start

### 1. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure (Optional)

```bash
cp .env.example .env
# Edit .env if you want to change port, database path, or message template
```

### 3. Run

```bash
python run.py
# Open http://localhost:5000
```

That's it! The landing page lets you enter a location and optional preferences (dates, guests, price range). Hit search, and the app scrapes Airbnb and shows you the results.

## 📋 How It Works

1. **Enter a location** on the landing page (e.g., "Goa, India")
2. **Add optional preferences** — check-in/out dates, guests, price range
3. **Hit Search** — the app scrapes Airbnb listings matching your criteria
4. **View results** — listings are saved to the database and displayed in the UI
5. **Start Outreach** — click the outreach button to send personalized messages to all hosts
6. **Track progress** — watch messages get sent in real-time on the outreach status page

### 📨 Outreach Flow

The outreach system automates sending personalized messages to Airbnb hosts:

1. After a search, click **"🚀 Start Outreach"** on the results page
2. A browser window opens (non-headless) — **log in to Airbnb** if prompted
3. The app visits each listing and sends your personalized message to the host
4. Session is saved so you don't need to log in every time
5. Track sent/pending/failed status in real-time

The default message introduces you as a content creator offering to create content in exchange for stays. You can customize the message template from the UI before starting outreach.

## 🏗 Project Structure

```
airbnb-automate/
├── run.py                  # Entry point — just run this
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variables template
│
├── app/                    # Core application
│   ├── config.py           # Configuration (DB path, message template)
│   ├── models.py           # Data models (Search, Listing, OutreachMessage)
│   ├── database.py         # SQLite database layer
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
| `PLAYWRIGHT_CHANNEL` | Use installed `chrome` or `msedge` instead of bundled Chromium (helps Airbnb login) | (bundled Chromium) |
| `BROWSER_USER_DATA_DIR` | Persistent profile path (e.g. `data/airbnb_chrome_profile`); do not use your main Chrome profile while Chrome is open | (session in `data/browser_state.json`) |
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
- **Login Required for Outreach**: The outreach feature requires you to be logged in to Airbnb. The browser opens non-headless so you can log in manually. **If login fails in the opened window** (Google/Apple/captcha, or “unsupported browser”), set `PLAYWRIGHT_CHANNEL=chrome` in `.env` so the app uses your installed Google Chrome, then restart. Session is stored in `data/browser_state.json`, or in the folder from `BROWSER_USER_DATA_DIR` if you set it.
