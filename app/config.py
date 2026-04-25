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


# Outreach message template — placeholders: {host_name}, {place_name}, {location}
DEFAULT_OUTREACH_MESSAGE = """Hey {host_name}! 👋

I came across your beautiful place "{place_name}" in {location} and I'd love to stay there!

I'm Shubham — a Software Engineer working remotely and Founder at The Boring Education. I'm also a content creator with a combined following of 150k+ across my pages:

📱 @theboringfounder (120k+ followers)
📱 @theboringplanks & @theboringeducation (30k+)
💼 LinkedIn: linkedin.com/in/imsks

Here's my pitch — I'd love to stay at your place and in return, I'll create high-quality content featuring your property that drives real traffic and bookings your way.

Would you be open to a content collaboration? I'd love to chat more about how we can make this a win-win!

Best,
Shubham"""


def get_outreach_message_template() -> str:
    """Get the outreach message template."""
    return os.getenv("OUTREACH_MESSAGE", DEFAULT_OUTREACH_MESSAGE)
