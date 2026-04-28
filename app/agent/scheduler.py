"""Scheduler that runs the negotiation agent on a recurring interval.

Default interval: 5 hours (configurable via ``AGENT_SCHEDULE_HOURS`` env var).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:
    global _shutdown
    _shutdown = True
    logger.info("Shutdown signal received — will stop after current cycle.")


def get_schedule_interval_seconds() -> int:
    """Interval between agent cycles (default 5 hours)."""
    raw = (os.getenv("AGENT_SCHEDULE_HOURS") or "5").strip()
    hours = max(1, int(raw))
    return hours * 3600


def run_agent_loop(
    *,
    headless: bool = True,
    auto_send: bool = False,
    max_threads: int = 20,
    once: bool = False,
) -> None:
    """Run the negotiation agent in a loop (or once if ``once=True``).

    Each cycle:
      1. Fetches inbox chats
      2. Classifies which need a reply
      3. Generates negotiation replies
      4. Optionally sends them (if auto_send)
      5. Sleeps for the configured interval
    """
    from app.agent.negotiator import run_negotiation

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    interval = get_schedule_interval_seconds()

    while not _shutdown:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{'#' * 60}")
        print(f"🤖 Agent cycle starting at {now}")
        print(f"{'#' * 60}")

        try:
            replies = run_negotiation(
                headless=headless,
                auto_send=auto_send,
                max_threads=max_threads,
            )

            if replies:
                print(f"\n📊 Generated {len(replies)} reply/replies:")
                for r in replies:
                    status = r.get("status", "unknown")
                    print(f"   • {r.get('host_name', '?')} — {status}")
                    print(f"     {r.get('reply', '')[:120]}…")
            else:
                print("   ✅ No replies needed right now.")

        except Exception as e:
            logger.exception("Agent cycle failed: %s", e)
            print(f"❌ Agent cycle error: {e}")

        if once:
            break

        if _shutdown:
            break

        next_run = datetime.fromtimestamp(
            time.time() + interval, tz=timezone.utc
        ).strftime("%H:%M UTC")
        print(f"\n⏰ Next cycle in {interval // 3600}h — sleeping until ~{next_run}  (Ctrl+C to stop)")

        # Sleep in small increments so we can respond to signals quickly
        elapsed = 0
        while elapsed < interval and not _shutdown:
            time.sleep(min(30, interval - elapsed))
            elapsed += 30

    print("\n👋 Agent loop stopped.")
