"""Configuration management for Airbnb Automate."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def get_db_path() -> str:
    """Get the database file path, creating parent directories if needed."""
    db_path = os.getenv("DATABASE_PATH", "data/airbnb_automate.db")
    full_path = BASE_DIR / db_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    return str(full_path)


def get_browser_state_path() -> str:
    """Get the path for storing browser state (cookies, session) for Airbnb login."""
    state_path = BASE_DIR / "data" / "browser_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return str(state_path)


def get_playwright_channel() -> Optional[str]:
    """Playwright browser channel: 'chrome', 'chromium', 'msedge', or empty (bundled).

    Set PLAYWRIGHT_CHANNEL=chrome to use the installed Google Chrome instead of
    Playwright's Chromium — this often fixes Airbnb / OAuth login issues.
    """
    raw = (os.getenv("PLAYWRIGHT_CHANNEL") or "").strip().lower()
    if not raw or raw in ("chromium", "playwright", "default"):
        return None
    return raw


DEFAULT_BROWSER_USER_DATA_DIR = "data/airbnb_browser_profile"


def get_browser_user_data_dir() -> str:
    """Persistent profile directory for Playwright (Chrome user-data).

    Defaults to ``data/airbnb_browser_profile`` so that login sessions are
    automatically preserved between runs.  Set BROWSER_USER_DATA_DIR to
    override, or set it to ``none`` to disable (not recommended).

    Use a *dedicated* directory — do not point at your live Chrome profile
    while Google Chrome is running (profile lock).
    """
    raw = (os.getenv("BROWSER_USER_DATA_DIR") or "").strip()
    if raw.lower() == "none":
        return ""
    if not raw:
        raw = DEFAULT_BROWSER_USER_DATA_DIR
    path = (BASE_DIR / raw).resolve() if not os.path.isabs(raw) else Path(raw)
    return str(path)


def get_browser_user_agent() -> Optional[str]:
    """Optional custom User-Agent. If unset, the browser's default is used (recommended)."""
    raw = (os.getenv("BROWSER_USER_AGENT") or "").strip()
    return raw or None


def get_chrome_cdp_url() -> Optional[str]:
    """If set, Playwright connects to that Chrome via CDP instead of launching a browser.

    See readme and .env.example for starting Chrome with ``--remote-debugging-port``
    and setting ``CHROME_CDP_URL=http://127.0.0.1:PORT``.
    """
    raw = (os.getenv("CHROME_CDP_URL") or "").strip()
    return raw or None


# Outreach message template — placeholders: {host_name}, {place_name}, {location}
DEFAULT_OUTREACH_MESSAGE = """Hi {host_name}! 👋

I just came across "{place_name}" while planning a trip to {location} and honestly, it looks amazing — exactly the kind of place I've been looking for.

A little about me — I'm Sachin, a remote software engineer and the founder of The Boring Education. I also create content online, and my pages (@theboringfounder and @theboringeducation) have grown to about 150k+ followers combined.

I'm reaching out because I'd genuinely love to stay at your place. I travel a lot for work and always look for unique homes over hotels. In return for the stay, I'd be happy to create some organic content — photos, reels, an honest review — that showcases your property to my audience and helps drive future bookings.

No pressure at all! If this sounds interesting, I'd love to hop on a quick chat and figure out dates that work for both of us.

Either way, beautiful place — you've done a great job with it!

Cheers,
Sachin"""


def get_outreach_message_template() -> str:
    """Get the outreach message template."""
    return os.getenv("OUTREACH_MESSAGE", DEFAULT_OUTREACH_MESSAGE)


def get_outreach_max_sends_per_window() -> int:
    """Max successful host messages per sliding time window (global across all searches)."""
    raw = (os.getenv("OUTREACH_MAX_SENDS_PER_WINDOW") or "5").strip()
    return max(1, int(raw))


def get_outreach_rate_window_seconds() -> int:
    """Sliding window length in seconds (default 3 hours)."""
    raw = (os.getenv("OUTREACH_RATE_WINDOW_SECONDS") or str(3 * 3600)).strip()
    return max(60, int(raw))


def get_outreach_inter_message_delay_seconds() -> float:
    """Minimum pause between each outreach attempt (success or failure), in seconds."""
    raw = (os.getenv("OUTREACH_INTER_MESSAGE_DELAY_SECONDS") or "120").strip()
    return max(0.0, float(raw))


def get_airbnb_base_url() -> str:
    """Site origin for search URLs (e.g. https://www.airbnb.co.in or .com)."""
    raw = (os.getenv("AIRBNB_BASE_URL") or "https://www.airbnb.com").strip().rstrip("/")
    return raw or "https://www.airbnb.com"


def get_flex_trip_months_count() -> int:
    """How many consecutive calendar months to pass as ``flexible_trip_dates[]`` (flexible search)."""
    raw = (os.getenv("FLEX_TRIP_MONTHS_COUNT") or "3").strip()
    return max(1, min(12, int(raw)))
