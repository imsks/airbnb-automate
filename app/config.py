"""Configuration management for Airbnb Automate."""

import os
from pathlib import Path

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
