#!/usr/bin/env python3
"""Airbnb Automate — Main entry point.

Usage:
    python run.py              Start the web app
    python run.py --port 8080  Start on a custom port
    python cli.py              Run the CLI with scheduler (see cli.py --help)
"""

import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import init_db


def setup_logging() -> None:
    """Configure logging for the application."""
    level = logging.DEBUG if os.getenv("FLASK_DEBUG", "false").lower() == "true" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """Start the web app."""
    import argparse

    parser = argparse.ArgumentParser(description="Airbnb Automate — Find Airbnb Listings")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FLASK_PORT", "5000")),
        help="Port for web app (default: 5000)",
    )
    args = parser.parse_args()

    setup_logging()

    print("🔧 Initializing database...")
    init_db()

    from web.app import create_app

    print(f"🚀 Starting Airbnb Automate on http://localhost:{args.port}")
    app = create_app()
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=args.port, debug=debug)


if __name__ == "__main__":
    main()
