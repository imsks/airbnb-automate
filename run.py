#!/usr/bin/env python3
"""Airbnb Automate — Main entry point.

Usage:
    python run.py serve          Start the web dashboard
    python run.py search         Run all active campaigns now
    python run.py schedule       Start the scheduler daemon
    python run.py init           Initialize database and load config campaigns
"""

import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import load_config, get_creator_profile
from app.database import init_db, create_campaign, get_campaigns
from app.models import Campaign, CampaignStatus
from app.scheduler import run_all_active_campaigns, start_scheduler


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the application."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_init() -> None:
    """Initialize the database and load campaigns from config."""
    print("🔧 Initializing database...")
    init_db()

    config = load_config()
    campaigns_config = config.get("campaigns", [])

    existing = get_campaigns()
    existing_names = {c.name for c in existing}

    loaded = 0
    for camp_cfg in campaigns_config:
        if not camp_cfg.get("enabled", True):
            continue
        if camp_cfg["name"] in existing_names:
            print(f"  ⏭  Campaign '{camp_cfg['name']}' already exists, skipping")
            continue

        campaign = Campaign(
            name=camp_cfg["name"],
            location=camp_cfg["location"],
            checkin=camp_cfg["checkin"],
            checkout=camp_cfg["checkout"],
            guests=camp_cfg.get("guests", 2),
            min_price=camp_cfg.get("min_price", 0),
            max_price=camp_cfg.get("max_price", 500),
            property_types=camp_cfg.get("property_types", []),
            amenities=camp_cfg.get("amenities", []),
            max_listings=camp_cfg.get("max_listings", 20),
        )
        campaign_id = create_campaign(campaign)
        print(f"  ✅ Created campaign: {campaign.name} (ID: {campaign_id})")
        loaded += 1

    print(f"\n✨ Done! Loaded {loaded} new campaigns.")
    print("   Run 'python run.py serve' to start the dashboard.")


def cmd_serve(port: int = 5000) -> None:
    """Start the Flask web dashboard."""
    from web.app import create_app

    print(f"🚀 Starting Airbnb Automate dashboard on http://localhost:{port}")
    app = create_app()
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)


def cmd_search() -> None:
    """Run all active campaigns immediately."""
    init_db()
    print("🔍 Running all active campaigns...")

    results = run_all_active_campaigns()

    if not results:
        print("No active campaigns to run. Use 'python run.py init' first.")
        return

    print("\n📊 Results:")
    for result in results:
        print(f"\n  Campaign: {result['campaign']}")
        print(f"    Listings found: {result['listings_found']}")
        print(f"    Outreach created: {result['outreach_created']}")

    print("\n✨ Done! Check the dashboard for details: python run.py serve")


def cmd_schedule() -> None:
    """Start the background scheduler."""
    import time

    init_db()
    config = load_config()
    schedule_config = config.get("schedule", {})

    print(f"⏰ Starting scheduler ({schedule_config.get('frequency', 'weekly')} "
          f"at {schedule_config.get('time', '09:00')})")
    print("   Press Ctrl+C to stop.\n")

    sched = start_scheduler()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n👋 Stopping scheduler...")
        sched.shutdown()
        print("Done!")


def main() -> None:
    """Main entry point with CLI argument parsing."""
    parser = argparse.ArgumentParser(
        description="Airbnb Automate — Content Creator Outreach Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  init        Initialize database and load campaigns from config.yaml
  serve       Start the web dashboard
  search      Run all active campaigns now
  schedule    Start the scheduler for automated runs

Examples:
  python run.py init              # First-time setup
  python run.py serve             # Start dashboard at http://localhost:5000
  python run.py serve --port 8080 # Start on custom port
  python run.py search            # Run campaigns immediately
  python run.py schedule          # Start automated scheduler
        """,
    )

    parser.add_argument(
        "command",
        choices=["init", "serve", "search", "schedule"],
        help="Command to run",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FLASK_PORT", "5000")),
        help="Port for web dashboard (default: 5000)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    commands = {
        "init": cmd_init,
        "serve": lambda: cmd_serve(args.port),
        "search": cmd_search,
        "schedule": cmd_schedule,
    }

    commands[args.command]()


if __name__ == "__main__":
    main()
