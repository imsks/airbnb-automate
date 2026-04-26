#!/usr/bin/env python3
"""Airbnb Automate — CLI with optional scheduler.

Run once:
    python cli.py --locations "Goa, India" "Bali, Indonesia" "Manali, India"

Run on a 4-hour loop:
    python cli.py --locations "Goa, India" "Bali, Indonesia" --schedule

Customize:
    python cli.py --locations "Goa, India" --invites 5 --guests 3 \\
                  --checkin 2026-07-01 --checkout 2026-07-07 \\
                  --min-price 20 --max-price 120
"""

import argparse
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import get_outreach_message_template
from app.database import (
    create_outreach_messages,
    create_search,
    get_listings,
    init_db,
    save_listings,
    update_search_status,
)
from app.models import Search, SearchStatus
from app.outreach import run_outreach_sync
from app.scraper import scrape_listings_sync

logger = logging.getLogger(__name__)

SCHEDULE_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received — finishing current cycle then exiting.")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_date(value: str) -> str:
    """Validate that a string is a valid YYYY-MM-DD date."""
    if not _DATE_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Use YYYY-MM-DD format (e.g. 2026-07-01)"
        )
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Use a valid YYYY-MM-DD date (e.g. 2026-07-01)"
        )
    return value


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI mode."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def process_location(
    location: str,
    *,
    invites: int = 3,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    message_template: Optional[str] = None,
    headless: bool = True,
) -> dict:
    """Search a single location, pick top listings, and send outreach messages.

    Returns a summary dict with counts.
    """
    print(f"\n{'='*60}")
    print(f"📍 Processing: {location}")
    print(f"{'='*60}")

    # 1. Create a search record
    search = Search(
        location=location,
        checkin=checkin or "",
        checkout=checkout or "",
        guests=guests,
        min_price=min_price,
        max_price=max_price,
    )
    search_id = create_search(search)
    logger.info("Created search #%d for '%s'", search_id, location)

    # 2. Scrape listings
    print(f"🔍 Scraping Airbnb listings for '{location}'...")
    try:
        listings = scrape_listings_sync(
            location=location,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            min_price=min_price,
            max_price=max_price,
            max_listings=invites * 3,  # fetch extra so we have choices
            headless=headless,
        )
    except Exception as e:
        logger.error("Scraping failed for '%s': %s", location, e)
        update_search_status(search_id, SearchStatus.FAILED, 0)
        print(f"❌ Scraping failed for '{location}': {e}")
        return {"location": location, "scraped": 0, "sent": 0, "failed": 0, "skipped": 0}

    if not listings:
        update_search_status(search_id, SearchStatus.COMPLETED, 0)
        print(f"⚠️  No listings found for '{location}'")
        return {"location": location, "scraped": 0, "sent": 0, "failed": 0, "skipped": 0}

    saved = save_listings(listings, search_id)
    update_search_status(search_id, SearchStatus.COMPLETED, len(listings))
    print(f"✅ Found {len(listings)} listings (saved {saved} new)")

    # 3. Pick top N listings for outreach (sorted by rating already from scraper)
    target_listings = listings[:invites]
    print(f"📨 Sending invites to top {len(target_listings)} hosts...")

    for i, lst in enumerate(target_listings, 1):
        host = lst.host_name or "Host"
        print(f"   {i}. {lst.title} — hosted by {host} (⭐ {lst.rating})")

    # 4. Create outreach messages and run outreach
    template = message_template or get_outreach_message_template()
    create_outreach_messages(search_id, target_listings, template)

    try:
        summary = run_outreach_sync(search_id, template)
    except Exception as e:
        logger.error("Outreach failed for '%s': %s", location, e)
        print(f"❌ Outreach error for '{location}': {e}")
        return {"location": location, "scraped": len(listings), "sent": 0, "failed": len(target_listings), "skipped": 0}

    print(f"\n📊 Results for '{location}':")
    print(f"   Scraped: {len(listings)} | Sent: {summary.get('sent', 0)} | "
          f"Failed: {summary.get('failed', 0)} | Skipped: {summary.get('skipped', 0)}")

    return {
        "location": location,
        "scraped": len(listings),
        **summary,
    }


def run_cycle(
    locations: list[str],
    *,
    invites: int = 3,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    message_template: Optional[str] = None,
    headless: bool = True,
) -> list[dict]:
    """Run one full cycle: scrape + outreach for every location."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'#'*60}")
    print(f"🚀 Starting outreach cycle at {now}")
    print(f"   Locations: {', '.join(locations)}")
    print(f"   Invites per location: {invites}")
    print(f"{'#'*60}")

    results = []
    for location in locations:
        if _shutdown:
            print("⏹️  Shutdown requested — skipping remaining locations.")
            break
        result = process_location(
            location,
            invites=invites,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            min_price=min_price,
            max_price=max_price,
            message_template=message_template,
            headless=headless,
        )
        results.append(result)

    # Print overall summary
    total_scraped = sum(r.get("scraped", 0) for r in results)
    total_sent = sum(r.get("sent", 0) for r in results)
    total_failed = sum(r.get("failed", 0) for r in results)

    print(f"\n{'='*60}")
    print(f"📋 Cycle Summary")
    print(f"   Locations processed: {len(results)}/{len(locations)}")
    print(f"   Total scraped: {total_scraped}")
    print(f"   Total sent: {total_sent}")
    print(f"   Total failed: {total_failed}")
    print(f"{'='*60}")

    return results


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Airbnb Automate CLI — Scrape listings and send outreach on autopilot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # One-time run for multiple locations (3 invites each, the default)
  python cli.py --locations "Goa, India" "Bali, Indonesia" "Manali, India"

  # Send 5 invites per location with date filters
  python cli.py --locations "Goa, India" --invites 5 \\
                --checkin 2026-07-01 --checkout 2026-07-07

  # Run every 4 hours (Ctrl+C to stop)
  python cli.py --locations "Goa, India" "Bali, Indonesia" --schedule

  # Dry run: scrape only, no outreach
  python cli.py --locations "Goa, India" --dry-run
""",
    )

    parser.add_argument(
        "--locations",
        nargs="+",
        required=True,
        metavar="LOCATION",
        help='One or more Airbnb locations (e.g. "Goa, India" "Bali, Indonesia")',
    )
    parser.add_argument(
        "--invites",
        type=int,
        default=3,
        help="Number of outreach invites per location (default: 3)",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run every 4 hours in a loop (Ctrl+C to stop)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=SCHEDULE_INTERVAL_SECONDS,
        help="Schedule interval in seconds (default: 14400 = 4 hours). Only used with --schedule",
    )
    parser.add_argument(
        "--checkin",
        type=validate_date,
        default=None,
        help="Check-in date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--checkout",
        type=validate_date,
        default=None,
        help="Check-out date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--guests",
        type=int,
        default=2,
        help="Number of guests (default: 2)",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=None,
        help="Minimum price per night",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="Maximum price per night",
    )
    parser.add_argument(
        "--message",
        default=None,
        help="Custom outreach message template (uses {host_name}, {place_name}, {location} placeholders)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape listings only — skip outreach",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show the browser window (default: headless)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print("🔧 Initializing database...")
    init_db()

    headless = not args.no_headless

    common_kwargs = {
        "locations": args.locations,
        "invites": args.invites,
        "checkin": args.checkin,
        "checkout": args.checkout,
        "guests": args.guests,
        "min_price": args.min_price,
        "max_price": args.max_price,
        "message_template": args.message,
        "headless": headless,
    }

    if args.dry_run:
        # Dry-run: only scrape, no outreach
        print("🏃 Dry-run mode — scraping only, no outreach messages will be sent\n")
        for location in args.locations:
            print(f"\n📍 Scraping '{location}'...")
            try:
                listings = scrape_listings_sync(
                    location=location,
                    checkin=args.checkin,
                    checkout=args.checkout,
                    guests=args.guests,
                    min_price=args.min_price,
                    max_price=args.max_price,
                    max_listings=args.invites * 3,
                    headless=headless,
                )
                print(f"   Found {len(listings)} listings")
                for i, lst in enumerate(listings[:args.invites], 1):
                    host = lst.host_name or "Unknown"
                    print(f"   {i}. {lst.title} — {host} (⭐ {lst.rating}, 💰 {lst.price_per_night})")
            except Exception as e:
                print(f"   ❌ Failed: {e}")
        return

    if not args.schedule:
        # Single run
        run_cycle(**common_kwargs)
        print("\n✨ Done! Run with --schedule to repeat every 4 hours.")
        return

    # Scheduled mode
    interval = args.interval
    hours = interval / 3600
    print(f"\n⏰ Scheduler active — running every {hours:.1f} hours")
    print("   Press Ctrl+C to stop\n")

    cycle_count = 0
    while not _shutdown:
        cycle_count += 1
        print(f"\n🔄 Cycle #{cycle_count}")
        run_cycle(**common_kwargs)

        if _shutdown:
            break

        next_run = datetime.now(timezone.utc).timestamp() + interval
        next_run_str = datetime.fromtimestamp(next_run, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        print(f"\n😴 Sleeping until next cycle at {next_run_str} ({hours:.1f}h)...")
        print("   Press Ctrl+C to stop\n")

        # Sleep in small chunks so Ctrl+C is responsive
        elapsed = 0
        while elapsed < interval and not _shutdown:
            chunk = min(30, interval - elapsed)
            time.sleep(chunk)
            elapsed += chunk

    print("\n👋 Scheduler stopped. Goodbye!")


if __name__ == "__main__":
    main()
