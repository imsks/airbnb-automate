# 🏠 Airbnb Automate

A simple app to search Airbnb listings by location, store results in a database, and view them in a clean UI.

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
# Edit .env if you want to change port, database path, etc.
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
5. **Browse past searches** — all your previous searches are listed on the home page

## 🏗 Project Structure

```
airbnb-automate/
├── run.py                  # Entry point — just run this
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variables template
│
├── app/                    # Core application
│   ├── config.py           # Configuration (DB path, env)
│   ├── models.py           # Data models (Search, Listing)
│   ├── database.py         # SQLite database layer
│   └── scraper.py          # Airbnb scraper (Playwright)
│
├── web/                    # Flask web app
│   ├── app.py              # Routes (home, search, results)
│   ├── static/style.css    # Styles
│   └── templates/          # HTML templates
│       ├── base.html
│       ├── home.html
│       └── results.html
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
| `HEADLESS` | Run browser headless | true |

## 🧪 Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

## ⚠️ Notes

- **Airbnb ToS**: Automated scraping may violate Airbnb's Terms of Service. Use responsibly.
- **Browser Required**: The scraper uses Playwright with Chromium. Run `playwright install chromium` after installing dependencies.
