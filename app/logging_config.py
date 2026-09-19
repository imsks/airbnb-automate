"""Readable console logging, and an in-memory feed the dashboard can show.

The worker's useful output — "scouting Goa", "sent to Asha" — was buried in
library chatter. Here app logs get a short, aligned format and everything else
is turned down, while the same records are kept in a ring buffer so progress is
visible in the UI without tailing a terminal.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any

FEED_SIZE = 300

_NOISY = (
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "google",
    "google_genai",
    "langchain",
    "langchain_core",
    "langgraph",
    "asyncio",
    "uvicorn.access",
    "watchfiles",
)


class _Feed(logging.Handler):
    """Keeps recent app log records for the dashboard."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: deque[dict[str, Any]] = deque(maxlen=FEED_SIZE)

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("app"):
            return
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - never break logging
            return
        self.records.append(
            {
                "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "source": record.name.replace("app.agent.", "").replace("app.", ""),
                "message": message,
            }
        )


_feed = _Feed()


class _Formatter(logging.Formatter):
    """``14:32:01  scout  🔭 Scouting Goa, India…``"""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        source = record.name.replace("app.agent.", "").replace("app.", "")
        prefix = "" if record.levelno <= logging.INFO else f"{record.levelname} "
        return f"{stamp}  {source:<12} {prefix}{record.getMessage()}"


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging once, for any entry point."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(_Formatter())
    root.addHandler(console)
    root.addHandler(_feed)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if not verbose:
        for name in _NOISY:
            logging.getLogger(name).setLevel(logging.WARNING)

    # Playwright is extremely chatty even at INFO.
    logging.getLogger("playwright").setLevel(logging.WARNING)
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    os.environ.setdefault("GLOG_minloglevel", "2")


def recent_activity(limit: int = 60) -> list[dict[str, Any]]:
    """Most recent app log lines, newest first."""
    return list(_feed.records)[-limit:][::-1]
