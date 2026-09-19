"""Tests for the shared send budget and thread linking."""

import asyncio
import os
import tempfile

import pytest

from app import policy as policy_mod
from app import send_budget
from app.database import get_connection, init_db
from app.models import Deal
from app.send_budget import Channel, SendingFrozen
from app.thread_linking import (
    MATCH_THRESHOLD,
    extract_thread_id,
    match_thread_to_deals,
    score_match,
)


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr("app.policy.get_connection", lambda p=None: get_connection(path))
    yield path
    os.unlink(path)


@pytest.fixture
def budget_env(monkeypatch):
    monkeypatch.setenv("OUTREACH_MAX_SENDS_PER_WINDOW", "3")
    monkeypatch.setenv("OUTREACH_RATE_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("OUTREACH_INTER_MESSAGE_DELAY_SECONDS", "0")


def _reserve(channel, db, body=None):
    """Run one reservation to completion. ``body`` may raise to simulate failure."""

    async def _run():
        async with send_budget.reserved_send(channel, db):
            if body:
                body()

    return asyncio.run(_run())


# --- Budget accounting -----------------------------------------------------


def test_fresh_budget_reports_full_capacity(db, budget_env):
    status = send_budget.budget_status(db)
    assert status["used"] == 0
    assert status["remaining"] == 3
    assert status["sending_enabled"] is True


def test_a_send_consumes_one_slot(db, budget_env):
    _reserve(Channel.OUTREACH, db)
    assert send_budget.remaining_sends(db) == 2


def test_outreach_and_negotiation_share_one_budget(db, budget_env):
    """v1's bug: negotiation replies bypassed the cap entirely."""
    _reserve(Channel.OUTREACH, db)
    _reserve(Channel.NEGOTIATION, db)
    assert send_budget.remaining_sends(db) == 1
    assert send_budget.budget_status(db)["used"] == 2


def test_a_failed_send_does_not_consume_budget(db, budget_env):
    def explode():
        raise RuntimeError("browser exploded")

    with pytest.raises(RuntimeError, match="browser exploded"):
        _reserve(Channel.OUTREACH, db, explode)
    assert send_budget.remaining_sends(db) == 3


def test_exhausted_budget_reports_a_next_slot(db, budget_env):
    for _ in range(3):
        _reserve(Channel.OUTREACH, db)
    status = send_budget.budget_status(db)
    assert status["remaining"] == 0
    assert status["next_slot_at"] is not None


# --- Kill switch -----------------------------------------------------------


def test_kill_switch_prevents_reserving_a_slot(db, budget_env):
    policy_mod.freeze_sending("testing")

    def must_not_run():
        pytest.fail("body must not run while frozen")

    with pytest.raises(SendingFrozen):
        _reserve(Channel.OUTREACH, db, must_not_run)


def test_kill_switch_engaged_during_the_quota_wait_still_stops_the_send(
    db, budget_env, monkeypatch
):
    """The quota wait can last hours; a freeze during it must take effect."""

    async def freeze_midway(_db_path=None):
        policy_mod.freeze_sending("engaged mid-wait")

    monkeypatch.setattr("app.send_budget.wait_until_send_allowed", freeze_midway)

    def must_not_run():
        pytest.fail("body must not run after a mid-wait freeze")

    with pytest.raises(SendingFrozen, match="while waiting"):
        _reserve(Channel.OUTREACH, db, must_not_run)


def test_sending_resumes_after_the_switch_is_released(db, budget_env):
    policy_mod.freeze_sending()
    policy_mod.resume_sending()
    _reserve(Channel.NEGOTIATION, db)
    assert send_budget.budget_status(db)["used"] == 1


# --- Thread id extraction --------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.airbnb.com/messages/thread/1234567", "1234567"),
        ("https://www.airbnb.co.in/hosting/messages/thread/99", "99"),
        ("https://www.airbnb.com/inbox?thread_id=4242", "4242"),
        ("https://www.airbnb.com/rooms/12345", None),
        ("", None),
    ],
)
def test_extract_thread_id(url, expected):
    assert extract_thread_id(url) == expected


# --- Fuzzy matching --------------------------------------------------------


def _deal(deal_id, host, place, thread=None):
    return Deal(id=deal_id, listing_id=f"L{deal_id}", host_name=host, place_name=place,
                thread_id=thread)


def test_exact_host_and_title_scores_highly():
    deal = _deal(1, "Asha", "Sea Breeze Villa")
    assert score_match(deal, "Asha", "Sea Breeze Villa") > 0.95


def test_truncated_inbox_title_still_matches_on_host():
    deal = _deal(1, "Asha Menon", "Sea Breeze Villa with Infinity Pool")
    assert score_match(deal, "Asha Menon", "Sea Breeze Villa…") > MATCH_THRESHOLD


def test_match_picks_the_right_deal():
    candidates = [
        _deal(1, "Asha Menon", "Sea Breeze Villa"),
        _deal(2, "Ravi Kumar", "Mountain Hut"),
    ]
    matched = match_thread_to_deals("Ravi Kumar", "Mountain Hut", candidates)
    assert matched.id == 2


def test_unknown_host_matches_nothing():
    candidates = [_deal(1, "Asha Menon", "Sea Breeze Villa")]
    assert match_thread_to_deals("Zomato Support", "Order #42", candidates) is None


def test_ambiguous_match_is_refused():
    """Two near-identical hosts must not be guessed between."""
    candidates = [
        _deal(1, "Asha Menon", "Sea Breeze Villa"),
        _deal(2, "Asha Menon", "Sea Breeze Villa"),
    ]
    assert match_thread_to_deals("Asha Menon", "Sea Breeze Villa", candidates) is None


def test_already_linked_deals_are_not_candidates():
    candidates = [_deal(1, "Asha Menon", "Sea Breeze Villa", thread="T1")]
    assert match_thread_to_deals("Asha Menon", "Sea Breeze Villa", candidates) is None


def test_no_candidates_matches_nothing():
    assert match_thread_to_deals("Asha", "Villa", []) is None
