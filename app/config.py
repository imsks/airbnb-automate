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


# --- Guardrail defaults ---
# These seed the policy table; runtime values are read via app.policy.


def get_default_price_ceiling_per_night() -> tuple[float, str]:
    """Most an agent may ever agree to pay per night, and the currency it is in.

    ``0`` means the agent may only pursue free stays and must escalate any
    paid counter-offer.
    """
    raw = (os.getenv("MAX_PRICE_PER_NIGHT") or "0").strip()
    currency = (os.getenv("PRICE_CEILING_CURRENCY") or "INR").strip().upper()
    return max(0.0, float(raw)), currency


def get_default_allowed_deliverables() -> list[str]:
    """The only things an agent may promise a host, as comma-separated env values."""
    raw = (os.getenv("ALLOWED_DELIVERABLES") or "").strip()
    if not raw:
        return [
            "2 Instagram reels",
            "10 edited photos",
            "1 honest public review",
            "story coverage during the stay",
        ]
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_default_max_agent_replies_per_thread() -> int:
    """How many agent replies one thread may receive before a human must look."""
    raw = (os.getenv("MAX_AGENT_REPLIES_PER_THREAD") or "4").strip()
    return max(1, int(raw))


def get_default_credential_facts() -> dict[str, str]:
    """The claims an agent is allowed to make about who you are and your reach.

    The Warden cross-checks every draft against these, so anything not listed
    here cannot be asserted to a host.
    """
    return {
        "name": (os.getenv("CREATOR_NAME") or "Sachin Kumar Shukla").strip(),
        "role": (
            os.getenv("CREATOR_ROLE") or "remote software engineer and founder of The Boring Education"
        ).strip(),
        "followers": (os.getenv("CREATOR_FOLLOWERS") or "150k+ combined").strip(),
        "handles": (
            os.getenv("CREATOR_HANDLES") or "@theboringfounder, @theboringeducation"
        ).strip(),
    }


def get_follower_claim_ceiling() -> int:
    """Largest follower number an agent may state, parsed from the fact sheet."""
    raw = (os.getenv("CREATOR_FOLLOWERS_MAX") or "").strip()
    if raw.isdigit():
        return int(raw)
    facts = get_default_credential_facts()["followers"].lower()
    digits = "".join(ch for ch in facts if ch.isdigit())
    if not digits:
        return 0
    value = int(digits)
    return value * 1000 if "k" in facts else value
