"""Tests for CLI helpers."""

import os
import tempfile

import pytest

from app.database import (
    create_outreach_messages,
    create_search,
    get_outreach_messages,
    init_db,
    save_listings,
    update_outreach_status,
)
from app.models import Listing, OutreachStatus, Search
from cli import select_outreach_targets


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def test_select_outreach_targets_skips_globally_sent(db_path):
    """After a listing is SENT, select_outreach_targets walks past it and picks the next."""
    sid = create_search(
        Search(location="Goa", checkin="2026-01-01", checkout="2026-01-08"), db_path
    )
    a = Listing(id="r1", title="A", host_name="h1", rating=5.0)
    b = Listing(id="r2", title="B", host_name="h2", rating=4.0)
    c = Listing(id="r3", title="C", host_name="h3", rating=3.0)
    save_listings([a, b, c], sid, db_path)
    template = "Hi {host_name} — {place_name} {location}"
    create_outreach_messages(sid, [a], template, db_path)
    msg = get_outreach_messages(sid, db_path)[0]
    update_outreach_status(msg.id, OutreachStatus.SENT, "", db_path)

    targets, skipped = select_outreach_targets([a, b, c], invites=2, db_path=db_path)
    assert skipped == 1
    assert [t.id for t in targets] == ["r2", "r3"]


def test_select_outreach_targets_takes_top_unmessaged_in_order(db_path):
    sid = create_search(
        Search(location="X", checkin="2026-01-01", checkout="2026-01-08"), db_path
    )
    a = Listing(id="x1", title="1", host_name="a")
    b = Listing(id="x2", title="2", host_name="b")
    save_listings([a, b], sid, db_path)
    t, sk = select_outreach_targets([a, b], invites=1, db_path=db_path)
    assert sk == 0
    assert len(t) == 1 and t[0].id == "x1"


def test_select_outreach_targets_dedupes_same_listing_id(db_path):
    """Scrape can return duplicate cards for the same room — only one outreach slot each."""
    sid = create_search(
        Search(location="X", checkin="2026-01-01", checkout="2026-01-08"), db_path
    )
    a1 = Listing(id="dup", title="A", host_name="a")
    a2 = Listing(id="dup", title="A copy", host_name="a")
    b = Listing(id="b", title="B", host_name="b")
    save_listings([a1, a2, b], sid, db_path)
    t, _ = select_outreach_targets([a1, a2, b], invites=3, db_path=db_path)
    assert [x.id for x in t] == ["dup", "b"]
