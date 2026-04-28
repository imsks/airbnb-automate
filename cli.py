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
from pathlib import Path
from typing import Optional

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_flex_trip_months_count, get_outreach_message_template
from app.locations_md import project_locations_md, read_locations_md
from app.database import (
    create_outreach_messages,
    create_search,
    get_listings,
    has_sent_outreach_to_listing,
    init_db,
    save_listings,
    update_search_status,
)
from app.models import Listing, Search, SearchStatus
from app.outreach import run_outreach_sync
from app.scraper import normalize_flex_duration_unit, scrape_listings_sync

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


def resolve_locations(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[str]:
    """Merge ``--locations``, ``--locations-file``, and optional default ``locations.md``."""
    locs: list[str] = list(args.locations or [])
    if args.locations_file:
        p = Path(args.locations_file)
        if not p.is_file():
            parser.error(f"Locations file not found: {p}")
        locs.extend(read_locations_md(p))
    elif not locs:
        default_md = project_locations_md(ROOT_DIR)
        if default_md.is_file():
            locs.extend(read_locations_md(default_md))
    if not locs:
        parser.error(
            "No locations: pass --locations and/or --locations-file, "
            f"or create {project_locations_md(ROOT_DIR).name} in the project root."
        )
    return locs


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI mode."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def select_outreach_targets(
    listings: list[Listing], invites: int, db_path: Optional[str] = None
) -> tuple[list[Listing], int]:
    """Choose up to ``invites`` listings, in scrape order, excluding any listing id we already
    messaged successfully (SENT) in a prior run. Returns (targets, n_skipped_due_to_prior_send).
    """
    out: list[Listing] = []
    seen_ids: set[str] = set()
    skipped_prior = 0
    for lst in listings:
        if len(out) >= invites:
            break
        lid = (lst.id or "").strip()
        if not lid:
            continue
        if lid in seen_ids:
            continue
        seen_ids.add(lid)
        if has_sent_outreach_to_listing(lid, db_path):
            skipped_prior += 1
            continue
        out.append(lst)
    return out, skipped_prior


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
    date_mode: str = "flexible",
    flex_duration: int = 1,
    flex_duration_unit: str = "week",
    flex_trip_months_count: Optional[int] = None,
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
        date_mode=date_mode,
        flex_duration=flex_duration,
        flex_duration_unit=flex_duration_unit,
    )
    search_id = create_search(search)
    logger.info("Created search #%d for '%s'", search_id, location)

    # 1. Scrape first (outreach runs only after scrape completes for this location)
    print(f"📥 Phase 1 — Scrape: '{location}'")
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
            date_mode=date_mode,
            flex_duration=flex_duration,
            flex_duration_unit=flex_duration_unit,
            flex_trip_months_count=flex_trip_months_count,
        )
    except Exception as e:
        logger.error("Scraping failed for '%s': %s", location, e)
        update_search_status(search_id, SearchStatus.FAILED, 0)
        print(f"❌ Scraping failed for '{location}': {e}")
        return {
            "location": location,
            "scraped": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "airbnb_rate_limited": False,
        }

    if not listings:
        update_search_status(search_id, SearchStatus.COMPLETED, 0)
        print(f"⚠️  No listings found for '{location}'")
        return {
            "location": location,
            "scraped": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "airbnb_rate_limited": False,
        }

    saved = save_listings(listings, search_id)
    update_search_status(search_id, SearchStatus.COMPLETED, len(listings))
    print(f"   ✅ {len(listings)} listings scraped ({saved} new rows saved)")

    # 2. Outreach: walk URLs in order, skip already-SENT, continue on per-listing errors
    print(f"📤 Phase 2 — Outreach: up to {invites} host(s) (skipping any already messaged)")
    target_listings, skipped_prior = select_outreach_targets(listings, invites)
    if skipped_prior:
        print(
            f"   ⏭️  Skipped {skipped_prior} result(s) in order — already sent in a past run"
        )
    if not target_listings:
        print("   ⚠️  No hosts to message (all top results were already contacted).")
        return {
            "location": location,
            "scraped": len(listings),
            "total": 0,
            "sent": 0,
            "failed": 0,
            "skipped": skipped_prior,
            "airbnb_rate_limited": False,
        }

    for i, lst in enumerate(target_listings, 1):
        host = lst.host_name or "Host"
        url = (lst.url or "").strip() or f"(room {lst.id})"
        print(f"   {i}. {url}")
        print(f"      {lst.title} — {host} (⭐ {lst.rating})")

    template = message_template or get_outreach_message_template()
    create_outreach_messages(search_id, target_listings, template)
    summary = run_outreach_sync(search_id, template)

    if summary.get("error"):
        logger.error("Outreach failed for '%s': %s", location, summary["error"])
        print(f"❌ Outreach error for '{location}': {summary['error']}")
        return {
            "location": location,
            "scraped": len(listings),
            "sent": summary.get("sent", 0),
            "failed": summary.get("failed", 0),
            "skipped": summary.get("skipped", 0),
            "airbnb_rate_limited": False,
        }

    print(f"\n📊 Results for '{location}':")
    print(
        f"   Scraped: {len(listings)} | Sent: {summary.get('sent', 0)} | "
        f"Failed: {summary.get('failed', 0)} | Skipped: {summary.get('skipped', 0)}"
    )
    if skipped_prior:
        print(
            f"   (Plus {skipped_prior} skipped before queue — already contacted)"
        )
    if summary.get("airbnb_rate_limited"):
        print(
            "   🛑 Airbnb capped host messages — remaining invites were skipped. "
            "Wait a few hours before the next run."
        )

    return {
        "location": location,
        "scraped": len(listings),
        "skipped_prior": skipped_prior,
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
    date_mode: str = "flexible",
    flex_duration: int = 1,
    flex_duration_unit: str = "week",
    flex_trip_months_count: Optional[int] = None,
) -> list[dict]:
    """Run one full cycle: scrape + outreach for every location."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'#'*60}")
    print(f"🚀 Starting cycle at {now} (each location: scrape → then outreach)")
    print(f"   Locations: {', '.join(locations)}")
    print(f"   Invites per location: {invites}")
    if date_mode == "fixed" and checkin:
        print(f"   Dates: fixed {checkin} → {checkout or '?'}")
    else:
        print(f"   Dates: flexible · {flex_duration} {flex_duration_unit}(s)")
    print(f"   Browser: {'headless' if headless else 'visible'}")
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
            date_mode=date_mode,
            flex_duration=flex_duration,
            flex_duration_unit=flex_duration_unit,
            flex_trip_months_count=flex_trip_months_count,
        )
        results.append(result)
        if result.get("airbnb_rate_limited"):
            print(
                "\n⏸️  Stopping this cycle — Airbnb in-app messaging limit was hit. "
                "Later locations are skipped; quota settings: OUTREACH_MAX_SENDS_PER_WINDOW / "
                "OUTREACH_RATE_WINDOW_SECONDS in .env"
            )
            break

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

  # Flexible trip (default): 2 weeks — browser runs headless
  python cli.py --locations "Goa, India" --invites 5 --flex-duration 2 --flex-duration-unit week

  # Fixed calendar dates
  python cli.py --locations "Goa, India" --date-mode fixed \\
                --checkin 2026-07-01 --checkout 2026-07-07

  # Run every 4 hours (Ctrl+C to stop)
  python cli.py --locations "Goa, India" "Bali, Indonesia" --schedule

  # All non-empty lines from locations.md (if file exists, no --locations needed)
  python cli.py --locations-file locations.md

  # Dry run: scrape only, no outreach
  python cli.py --locations "Goa, India" --dry-run
""",
    )

    parser.add_argument(
        "--locations",
        nargs="*",
        default=None,
        metavar="LOCATION",
        help='Airbnb locations (optional if --locations-file or locations.md exists)',
    )
    parser.add_argument(
        "--locations-file",
        default=None,
        metavar="PATH",
        help="Text/markdown file: one location per non-comment line (overrides auto locations.md)",
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
        "--date-mode",
        choices=["flexible", "fixed"],
        default="flexible",
        help="flexible = trip length (default); fixed = --checkin and --checkout required",
    )
    parser.add_argument(
        "--flex-duration",
        type=int,
        default=1,
        help="Flexible mode: trip length (default: 1)",
    )
    parser.add_argument(
        "--flex-trip-months",
        type=int,
        default=None,
        help="How many consecutive calendar months in flexible_trip_dates[] (default: FLEX_TRIP_MONTHS_COUNT / 3)",
    )
    parser.add_argument(
        "--flex-duration-unit",
        choices=["day", "week", "month", "weekend"],
        default="week",
        help="Flexible mode: weekend, day (nights), week, month (default: week)",
    )
    parser.add_argument(
        "--checkin",
        type=validate_date,
        default=None,
        help="Fixed mode: check-in (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--checkout",
        type=validate_date,
        default=None,
        help="Fixed mode: check-out (YYYY-MM-DD)",
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
        help="Show the browser (default: headless — recommended for CLI)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    # ── Agent sub-commands ──
    parser.add_argument(
        "--agent",
        choices=["negotiate", "outreach", "both"],
        default=None,
        help=(
            "Run the AI agent instead of the hardcoded flow. "
            "'negotiate' = monitor inbox & generate replies, "
            "'outreach' = generate AI-crafted initial messages, "
            "'both' = run negotiate then outreach"
        ),
    )
    parser.add_argument(
        "--agent-schedule",
        action="store_true",
        help="Run the agent on a recurring loop (default every 5 h, see AGENT_SCHEDULE_HOURS)",
    )
    parser.add_argument(
        "--auto-send",
        action="store_true",
        help="Agent mode: automatically send generated replies (default: review only)",
    )
    parser.add_argument(
        "--max-threads",
        type=int,
        default=20,
        help="Agent mode: max inbox threads to process (default: 20)",
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

    # ── Agent mode ────────────────────────────────────────────────────────
    if args.agent:
        headless = not args.no_headless

        if args.agent in ("negotiate", "both"):
            if args.agent_schedule:
                from app.agent.scheduler import run_agent_loop

                print("🤖 Starting negotiation agent loop…")
                run_agent_loop(
                    headless=headless,
                    auto_send=args.auto_send,
                    max_threads=args.max_threads,
                )
                return
            else:
                from app.agent.negotiator import run_negotiation

                print("🤖 Running negotiation agent (single cycle)…")
                replies = run_negotiation(
                    headless=headless,
                    auto_send=args.auto_send,
                    max_threads=args.max_threads,
                )
                if replies:
                    print(f"\n📊 Generated {len(replies)} reply/replies:")
                    for r in replies:
                        print(f"\n── {r.get('host_name', '?')} ──")
                        print(r.get("reply", "(empty)"))
                else:
                    print("✅ No replies needed right now.")

        if args.agent in ("outreach", "both"):
            from app.agent.outreach_agent import generate_outreach_message

            resolved_locations = resolve_locations(parser, args)
            print("🤖 Running AI outreach agent…")
            for location in resolved_locations:
                print(f"\n📍 Generating AI outreach for '{location}'…")
                try:
                    from app.scraper import scrape_listings_sync

                    listings = scrape_listings_sync(
                        location=location,
                        guests=args.guests,
                        max_listings=args.invites,
                        headless=headless,
                    )
                    for lst in listings[:args.invites]:
                        msg = generate_outreach_message(lst)
                        host = lst.host_name or "Host"
                        print(f"\n── {host} ({lst.title}) ──")
                        print(msg)
                except Exception as e:
                    print(f"   ❌ Failed: {e}")

        return

    # ── Classic mode (unchanged) ──────────────────────────────────────────
    resolved_locations = resolve_locations(parser, args)

    # CLI defaults to headless; use --no-headless only when debugging.
    headless = not args.no_headless

    if args.date_mode == "fixed":
        if not args.checkin or not args.checkout:
            parser.error("--date-mode fixed requires both --checkin and --checkout")
        checkin, checkout = args.checkin, args.checkout
        date_mode = "fixed"
        flex_duration = max(1, args.flex_duration)
        flex_unit = normalize_flex_duration_unit(args.flex_duration_unit)
        if flex_unit == "weekend":
            flex_duration = 1
    else:
        if args.checkin or args.checkout:
            parser.error(
                "Flexible mode does not use --checkin/--checkout; "
                "pass --date-mode fixed for calendar dates"
            )
        checkin, checkout = None, None
        date_mode = "flexible"
        flex_duration = max(1, args.flex_duration)
        flex_unit = normalize_flex_duration_unit(args.flex_duration_unit)
        if flex_unit == "weekend":
            flex_duration = 1

    flex_trip_months_count = (
        args.flex_trip_months
        if args.flex_trip_months is not None
        else get_flex_trip_months_count()
    )
    flex_trip_months_count = max(1, min(12, flex_trip_months_count))

    common_kwargs = {
        "locations": resolved_locations,
        "invites": args.invites,
        "checkin": checkin,
        "checkout": checkout,
        "guests": args.guests,
        "min_price": args.min_price,
        "max_price": args.max_price,
        "message_template": args.message,
        "headless": headless,
        "date_mode": date_mode,
        "flex_duration": flex_duration,
        "flex_duration_unit": flex_unit,
        "flex_trip_months_count": flex_trip_months_count,
    }

    if args.dry_run:
        # Dry-run: only scrape, no outreach
        print("🏃 Dry-run mode — scraping only, no outreach messages will be sent\n")
        for location in resolved_locations:
            print(f"\n📍 Scraping '{location}'...")
            try:
                listings = scrape_listings_sync(
                    location=location,
                    checkin=checkin,
                    checkout=checkout,
                    guests=args.guests,
                    min_price=args.min_price,
                    max_price=args.max_price,
                    max_listings=args.invites * 3,
                    headless=headless,
                    date_mode=date_mode,
                    flex_duration=flex_duration,
                    flex_duration_unit=flex_unit,
                    flex_trip_months_count=flex_trip_months_count,
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
