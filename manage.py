#!/usr/bin/env python3
"""Airbnb Automate — control surface.

Normal use is two commands:

    python manage.py login      # sign in to Airbnb once, by hand
    python manage.py start      # everything else, with a dashboard

`start` runs the API and the worker together and opens the UI, where you create
campaigns and watch progress. The rest of the commands below are for when you
want a single piece on its own — separate processes, a one-off tick, scripting.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from app import campaigns as campaign_repo
from app import jobs, policy as policy_mod
from app.agent import planner
from app.agent.chronicler import daily_brief
from app.database import init_db
from app.jobs import JobType, Priority
from app.locations_md import project_locations_md, read_locations_md
from app.logging_config import setup_logging
from app.models import Campaign, CampaignStatus
from app.send_budget import budget_status

logger = logging.getLogger(__name__)


def _resolve_places(args) -> list[str]:
    places = list(args.places or [])
    path = Path(args.places_file) if args.places_file else project_locations_md(ROOT_DIR)
    if path.exists():
        places.extend(read_locations_md(path))
    seen, unique = set(), []
    for place in places:
        if place and place not in seen:
            seen.add(place)
            unique.append(place)
    return unique


def cmd_start(args) -> int:
    """Run the API and the worker in one process, then open the dashboard.

    They share an event loop, which is safe because the worker is still the
    only thing that touches Playwright — the API just writes rows to `jobs`.
    Split them with `api` and `worker` when you want separate processes.
    """
    import asyncio
    import threading
    import webbrowser

    import uvicorn

    from app.api.main import create_app
    from app.outreach import check_airbnb_login_status_sync
    from app.worker import Worker

    url = f"http://{args.host}:{args.port}"

    if not args.skip_login_check and not check_airbnb_login_status_sync():
        print("\n  Not signed in to Airbnb. Run `python manage.py login` first,")
        print("  or pass --skip-login-check to start anyway.\n")
        return 1

    worker = Worker(campaign_id=args.campaign, headless=not args.no_headless)
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host=args.host, port=args.port, log_level="warning")
    )

    async def run_both() -> None:
        worker_task = asyncio.create_task(worker.run())
        try:
            await server.serve()
        finally:
            worker.stop()
            await asyncio.wait_for(worker_task, timeout=30)

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"\n  Dashboard  {url}")
    print("  Worker     running — create a campaign in the UI to give it work")
    print("  Stop       Ctrl+C\n")
    try:
        asyncio.run(run_both())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_login(args) -> int:
    """Airbnb blocks automated sign-in, so a human does it once and the
    persistent Chrome profile keeps the session for the worker."""
    from app.outreach import login_to_airbnb_sync

    print("Opening a browser — sign in to Airbnb, then leave the rest to the worker.")
    if login_to_airbnb_sync():
        print("✓ Logged in. The session is saved; you can close this.")
        return 0
    print("✗ Login not detected. Try PLAYWRIGHT_CHANNEL=chrome, or CHROME_CDP_URL.")
    return 1


def cmd_session(args) -> int:
    from app.outreach import check_airbnb_login_status_sync

    if check_airbnb_login_status_sync():
        print("✓ Airbnb session is live.")
        return 0
    print("✗ Not logged in — run: python manage.py login")
    return 1


def cmd_campaign(args) -> int:
    places = _resolve_places(args)
    if not places:
        print("No places given. Use --places or add them to locations.md.")
        return 1

    campaign_id = planner.bootstrap_campaign(
        Campaign(
            name=args.name,
            goal=args.goal,
            window_start=args.window[0],
            window_end=args.window[1],
            origin=args.origin,
            guests=args.guests,
            stay_nights=args.nights,
            status=CampaignStatus.ACTIVE,
        ),
        places,
        None,
    )
    result = planner.plan_tick(campaign_id)
    print(f"Campaign {campaign_id} created with {len(places)} territories.")
    print(f"Queued {result['total']} job(s): {result['queued']}")
    print("Start the worker to begin:  python manage.py worker")
    return 0


def cmd_worker(args) -> int:
    from app.worker import main as worker_main

    worker_main(campaign_id=args.campaign, headless=not args.no_headless, once=args.once)
    return 0


def cmd_api(args) -> int:
    import uvicorn

    uvicorn.run("app.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_tick(args) -> int:
    result = planner.plan_tick(args.campaign)
    print(json.dumps(result, indent=2))
    return 0


def cmd_brief(args) -> int:
    brief = daily_brief()
    star = brief["north_star"]
    print(f"\n  Closes / 100 messages : {star['closes_per_100_messages']}")
    print(f"  Reply rate            : {star['reply_rate_pct']}%")
    print(f"  Messages sent         : {star['messages_sent']}")
    print(f"  Sends left in window  : {brief['budget']['remaining']}/{brief['budget']['max']}")

    ready = brief["queues"]["ready_to_book"]
    print(f"\n  READY TO BOOK ({len(ready)}) — needs your card")
    for deal in ready:
        print(f"    · {deal['host']} — {deal['place']} ({deal['window'] or 'window TBC'})")

    blocked = brief["queues"]["needs_human"]
    print(f"\n  NEEDS A HUMAN ({len(blocked)})")
    for deal in blocked:
        print(f"    · {deal['host']} — {deal['reason'][:80]}")

    if brief["anomalies"]:
        print("\n  ALERTS")
        for alert in brief["anomalies"]:
            print(f"    ! {alert}")
    print()
    return 0


def cmd_status(args) -> int:
    print(json.dumps(
        {"jobs": jobs.queue_depth(), "budget": budget_status()}, indent=2, default=str
    ))
    return 0


def cmd_sync(args) -> int:
    job_id = jobs.enqueue(JobType.SYNC_INBOX, priority=Priority.INBOX_SYNC)
    print(f"Queued inbox sync as job {job_id}.")
    return 0


def cmd_freeze(args) -> int:
    policy_mod.freeze_sending(args.reason)
    print("Sending frozen. Nothing will be sent until you resume.")
    return 0


def cmd_resume(args) -> int:
    policy_mod.resume_sending()
    print("Sending resumed.")
    return 0


def cmd_itinerary(args) -> int:
    from app import territories as territory_repo

    stops = campaign_repo.get_stops(args.campaign)
    if not stops:
        print("No itinerary yet — run the worker so the Router can plan one.")
        return 0
    for stop in stops:
        territory = territory_repo.get_territory(stop.territory_id)
        name = territory.name if territory else f"#{stop.territory_id}"
        print(f"  {stop.seq:>2}. {stop.target_month}  {name:<34} {stop.score:.2f}  {stop.rationale}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py", description="Airbnb Automate v2 — campaign control"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="run everything and open the dashboard")
    start.add_argument("--campaign", type=int, default=None)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8000)
    start.add_argument("--no-browser", action="store_true", help="don't open a tab")
    start.add_argument("--no-headless", action="store_true", help="show the browser")
    start.add_argument(
        "--skip-login-check", action="store_true", help="start without an Airbnb session"
    )
    start.set_defaults(func=cmd_start)

    sub.add_parser("login", help="sign in to Airbnb once (opens a browser)").set_defaults(
        func=cmd_login
    )
    sub.add_parser("session", help="check whether the Airbnb session is live").set_defaults(
        func=cmd_session
    )

    campaign = sub.add_parser("campaign", help="create a campaign and queue its first work")
    campaign.add_argument("name")
    campaign.add_argument("--goal", default="")
    campaign.add_argument("--window", nargs=2, metavar=("START", "END"),
                          default=["", ""], help="YYYY-MM YYYY-MM")
    campaign.add_argument("--origin", default="", help="where the tour starts")
    campaign.add_argument("--places", nargs="+", help="candidate destinations")
    campaign.add_argument("--places-file", help="one place per line (default: locations.md)")
    campaign.add_argument("--guests", type=int, default=2)
    campaign.add_argument("--nights", type=int, default=7)
    campaign.set_defaults(func=cmd_campaign)

    worker = sub.add_parser("worker", help="drain the job queue (owns the browser)")
    worker.add_argument("--campaign", type=int, default=None)
    worker.add_argument("--no-headless", action="store_true", help="show the browser")
    worker.add_argument("--once", action="store_true", help="run a single job and exit")
    worker.set_defaults(func=cmd_worker)

    api = sub.add_parser("api", help="serve the dashboard")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")
    api.set_defaults(func=cmd_api)

    tick = sub.add_parser("tick", help="plan one round of work now")
    tick.add_argument("--campaign", type=int, default=None)
    tick.set_defaults(func=cmd_tick)

    itinerary = sub.add_parser("itinerary", help="show the planned route")
    itinerary.add_argument("--campaign", type=int, default=0)
    itinerary.set_defaults(func=cmd_itinerary)

    sub.add_parser("brief", help="print today's brief").set_defaults(func=cmd_brief)
    sub.add_parser("status", help="queue depth and send budget").set_defaults(func=cmd_status)
    sub.add_parser("sync", help="queue an inbox sync").set_defaults(func=cmd_sync)

    freeze = sub.add_parser("freeze", help="stop all sending immediately")
    freeze.add_argument("--reason", default="")
    freeze.set_defaults(func=cmd_freeze)

    sub.add_parser("resume", help="re-enable sending").set_defaults(func=cmd_resume)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
