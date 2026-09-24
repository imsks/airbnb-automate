"""Phone pings and the durable per-loop activity feeds."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app import activity as activity_feeds
from app import deals as deal_repo
from app import jobs, notify
from app.database import get_connection, init_db
from app.jobs import JobType
from app.models import DealState
from app.notify import Moment

_REPOS = ("app.jobs", "app.deals", "app.activity")


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for module in _REPOS:
        monkeypatch.setattr(
            f"{module}.get_connection", lambda p=None: get_connection(path), raising=False
        )
    conn = get_connection(path)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute(
            """INSERT INTO listings (id, search_id, title, host_name, location)
               VALUES ('L1', 1, 'Sea Villa', 'Asha', 'Goa, India')"""
        )
        conn.commit()
    finally:
        conn.close()
    yield path
    os.unlink(path)


# --- Notifier --------------------------------------------------------------


def test_notify_is_a_silent_noop_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    opened = MagicMock()
    with patch("urllib.request.urlopen", opened):
        assert notify.notify(Moment.READY_TO_BOOK, "Asha — Sea Villa") is False
    opened.assert_not_called()  # never touches the network


def test_notify_posts_to_telegram_when_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    captured = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = request.data
        return _Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        assert notify.notify(Moment.SESSION_DEAD, "session expired") is True

    assert "bottok/sendMessage" in captured["url"]
    assert b"chat_id=42" in captured["data"]


def test_a_deal_reaching_ready_to_book_pings_the_human(db, monkeypatch):
    """The state machine itself fires the ping — no caller has to remember to."""
    deal_id = deal_repo.upsert_deal("L1", host_name="Asha", place_name="Sea Villa", db_path=db)
    for state in (
        DealState.QUALIFIED,
        DealState.CONTACTED,
        DealState.HOST_REPLIED,
        DealState.TERMS_AGREED,
    ):
        deal_repo.transition(deal_id, state, db_path=db)

    pinged = []
    monkeypatch.setattr(
        "app.notify.notify_deal_state", lambda state_value, deal: pinged.append(state_value)
    )
    deal_repo.transition(deal_id, DealState.READY_TO_BOOK, db_path=db)
    assert pinged == ["ready_to_book"]


# --- Per-loop feeds --------------------------------------------------------


def test_feeds_split_office_work_from_courier_work(db):
    research = jobs.enqueue(JobType.RESEARCH_TERRITORY, {"name": "Goa"}, db_path=db)
    jobs.complete(research, {"territory": "Goa"}, db_path=db)
    send = jobs.enqueue(
        JobType.SEND_OUTREACH, {"lead_id": 1}, idempotency_key="s1", db_path=db
    )
    jobs.complete(send, {"status": "sent"}, db_path=db)

    office = activity_feeds.office_feed(db_path=db)
    courier = activity_feeds.courier_feed(db_path=db)

    office_types = {r["type"] for r in office}
    courier_types = {r["type"] for r in courier}
    assert JobType.RESEARCH_TERRITORY in office_types
    assert JobType.SEND_OUTREACH not in office_types
    assert JobType.SEND_OUTREACH in courier_types
    assert JobType.RESEARCH_TERRITORY not in courier_types

    # Feeds carry a human label and a short outcome.
    research_row = next(r for r in office if r["type"] == JobType.RESEARCH_TERRITORY)
    assert research_row["label"] == "Researching a place"
    assert "Goa" in research_row["detail"]
