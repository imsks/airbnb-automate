"""Configuration management for Airbnb Automate."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def load_config(config_path: str | None = None) -> dict:
    """Load configuration from YAML file."""
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", str(BASE_DIR / "config.yaml"))

    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_file, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_db_path() -> str:
    """Get the database file path, creating parent directories if needed."""
    db_path = os.getenv("DATABASE_PATH", "data/airbnb_automate.db")
    full_path = BASE_DIR / db_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    return str(full_path)


def get_creator_profile(config: dict | None = None) -> dict:
    """Get creator profile from config or environment variables."""
    if config and "creator" in config:
        return config["creator"]

    return {
        "name": os.getenv("CREATOR_NAME", "Content Creator"),
        "platform": os.getenv("CREATOR_PLATFORM", "Instagram"),
        "handle": os.getenv("CREATOR_HANDLE", "@creator"),
        "followers": int(os.getenv("CREATOR_FOLLOWERS", "10000")),
        "email": os.getenv("CREATOR_EMAIL", "creator@email.com"),
        "portfolio_url": os.getenv("CREATOR_PORTFOLIO_URL", ""),
        "content_types": [
            "Professional photography",
            "Short-form video (Reels/TikTok)",
            "Blog post with SEO optimization",
            "Google/Airbnb review",
        ],
    }
