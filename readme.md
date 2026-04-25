# 🏠 Airbnb Automate

**Content Creator Outreach Automation** — A tool that helps content creators find Airbnb listings and automate outreach to hosts for content-for-stay collaborations.

## 💡 The Idea

You're a content creator who wants to stay at Airbnb properties. In exchange, you create professional content (photos, videos, reviews) that brings exposure and bookings to the host. This tool automates the entire workflow:

1. **Search** — Finds Airbnb listings matching your target location, dates, and budget
2. **Generate** — Creates personalized pitch messages for each host
3. **Track** — Manages your outreach pipeline (sent, responded, accepted, declined)
4. **Schedule** — Runs searches automatically on a daily/weekly/monthly basis
5. **Dashboard** — Web UI to manage campaigns and track results

## 🏗 Architecture

```
airbnb-automate/
├── run.py                  # CLI entry point (init, serve, search, schedule)
├── config.yaml             # Campaign & creator configuration
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variables template
│
├── app/                    # Core application modules
│   ├── config.py           # Configuration management
│   ├── models.py           # Data models (Campaign, Listing, Outreach)
│   ├── database.py         # SQLite database layer
│   ├── scraper.py          # Airbnb search scraper (Playwright)
│   ├── outreach.py         # Message generation & email sending
│   ├── scheduler.py        # APScheduler for automated runs
│   └── templates/          # Message templates
│       ├── pitch_message.txt
│       └── follow_up_message.txt
│
├── web/                    # Flask web dashboard
│   ├── app.py              # Flask routes & API
│   ├── static/style.css    # Dashboard styling
│   └── templates/          # HTML templates
│       ├── base.html
│       ├── dashboard.html
│       ├── campaigns.html
│       ├── new_campaign.html
│       └── campaign_detail.html
│
└── tests/                  # Test suite
    ├── test_scraper.py
    ├── test_database.py
    └── test_outreach.py
```

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

Copy and edit the environment file:
```bash
cp .env.example .env
# Edit .env with your details
```

Edit `config.yaml` with your:
- **Creator profile** — name, platform, handle, followers, content types
- **Target campaigns** — locations, dates, price ranges
- **Schedule** — how often to run (daily/weekly/monthly)

### 3. Initialize

```bash
python run.py init
```

This creates the database and loads campaigns from `config.yaml`.

### 4. Run

**Option A: Web Dashboard** (recommended)
```bash
python run.py serve
# Open http://localhost:5000
```

**Option B: CLI Search**
```bash
python run.py search
```

**Option C: Scheduled Automation**
```bash
python run.py schedule
```

## 📋 Commands

| Command | Description |
|---------|-------------|
| `python run.py init` | Initialize database and load campaigns from config |
| `python run.py serve` | Start the web dashboard (default port 5000) |
| `python run.py serve --port 8080` | Start dashboard on custom port |
| `python run.py search` | Run all active campaigns immediately |
| `python run.py schedule` | Start the background scheduler |

## 🎯 How It Works

### Campaign Flow

1. **Define campaigns** in `config.yaml` — each campaign targets a specific location + date range
2. **Run a campaign** — the scraper searches Airbnb and collects listing details
3. **Auto-generate outreach** — personalized pitch messages are created for each host
4. **Copy & send messages** — use the dashboard to view messages and copy them to send via Airbnb messaging
5. **Track responses** — update status (sent, responded, accepted, declined) in the dashboard
6. **Follow up** — auto-generated follow-up messages for non-responsive hosts

### Outreach Pipeline

```
🔍 Search → 📋 Listings Found → ✉️ Message Generated → 📨 Sent → 💬 Response → ✅ Accepted
                                                                              → ❌ Declined
                                                                              → 😶 No Response → 📨 Follow-up
```

### Web Dashboard Features

- **Dashboard** — Overview stats (total outreach, response rate, acceptance rate)
- **Campaigns** — Create, view, pause, and run campaigns
- **Listings** — Browse scraped listings with photos, ratings, prices
- **Outreach** — View generated messages, update status, track follow-ups
- **API endpoints** — `/api/stats` and `/api/campaigns` for programmatic access

## ⚙️ Configuration

### config.yaml

```yaml
# Your profile
creator:
  name: "Your Name"
  platform: "Instagram"
  handle: "@yourhandle"
  followers: 10000
  content_types:
    - "Professional photography"
    - "Short-form video (Reels/TikTok)"

# Target campaigns
campaigns:
  - name: "Goa Beach Stays"
    location: "Goa, India"
    checkin: "2026-06-01"
    checkout: "2026-06-07"
    guests: 2
    min_price: 30
    max_price: 150
    max_listings: 20
    enabled: true

# Schedule
schedule:
  frequency: "weekly"    # daily, weekly, monthly
  day: "monday"
  time: "09:00"
```

### Environment Variables (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `CREATOR_NAME` | Your name | Content Creator |
| `CREATOR_PLATFORM` | Social media platform | Instagram |
| `CREATOR_HANDLE` | Your handle | @creator |
| `CREATOR_FOLLOWERS` | Follower count | 10000 |
| `SMTP_HOST` | Email server (optional) | smtp.gmail.com |
| `SMTP_USER` | Email username (optional) | — |
| `SMTP_PASSWORD` | Email password (optional) | — |
| `FLASK_PORT` | Dashboard port | 5000 |
| `DATABASE_PATH` | SQLite DB path | data/airbnb_automate.db |
| `HEADLESS` | Run browser headless | true |

## 🧪 Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

## 📝 Message Templates

The tool uses Jinja2 templates for message generation. Customize them in `app/templates/`:

- **`pitch_message.txt`** — Initial outreach to hosts
- **`follow_up_message.txt`** — Follow-up for non-responsive hosts

Templates have access to all listing and creator profile variables for personalization.

## ⚠️ Important Notes

- **Airbnb Terms of Service**: Automated scraping may violate Airbnb's ToS. Use responsibly and consider rate limiting.
- **Rate Limiting**: The tool includes configurable delays between actions to avoid being flagged.
- **Message Sending**: Messages are generated but must be manually sent through Airbnb's messaging system (copy-paste from dashboard). The optional email feature is for direct outreach when host emails are available.
- **Browser Required**: The scraper uses Playwright with Chromium. Run `playwright install chromium` after installing dependencies.

## 🛣 Future Enhancements

- Direct Airbnb messaging integration
- AI-powered message personalization
- Analytics dashboard with conversion funnels
- Multi-platform support (Booking.com, VRBO)
- Media kit auto-attachment
- Calendar integration for availability management
