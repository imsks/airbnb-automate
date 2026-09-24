"""Single-message permission and retry safety at the real persistence boundary."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import deals, policy
from app.agent import scribe
from app.database import create_search, get_connection, init_db, save_listings
from app.leads import upsert_lead
from app.messaging_errors import DeliveryUnconfirmed, MessageRejected
from app.models import DealState, Listing, MessageStatus, Search
from app.send_budget import budget_status


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = str(tmp_path / "one-message.db")
    init_db(path)
    search_id = create_search(Search(location="Gangtok, India"), path)
    save_listings([Listing(id="42", title="Garden room", host_name="Peggy")], search_id, path)
    lead_id = upsert_lead("42", search_id=search_id, db_path=path)
    monkeypatch.setenv("OUTREACH_MAX_SENDS_PER_WINDOW", "5")
    monkeypatch.setenv("OUTREACH_RATE_WINDOW_SECONDS", "10800")
    monkeypatch.setattr(scribe, "compose", MagicMock(return_value=("Hi Peggy! Your garden looks lovely.", None)))
    draft = scribe.prepare_outreach(lead_id, preview=True, db_path=path)
    return path, lead_id, draft


def test_one_message_authorization_never_resumes_bulk(setup):
    path, _lead, draft = setup
    token = policy.authorize_single_message(draft["message_id"], path)
    assert not policy.sending_enabled(path)
    assert policy.single_message_authorized(draft["message_id"], token, path)
    assert not policy.single_message_authorized(draft["message_id"] + 1, token, path)
    assert not policy.single_message_authorized(draft["message_id"], "wrong-token", path)
    with pytest.raises(ValueError, match="single-message"):
        policy.resume_sending(path)


def test_permission_consumed_atomically_with_attempt(setup):
    path, _lead, draft = setup
    token = policy.authorize_single_message(draft["message_id"], path)
    deals.begin_message_delivery(draft["message_id"], draft["message"], path, authorization=token)
    assert not policy.single_message_authorized(draft["message_id"], token, path)
    assert not policy.sending_enabled(path)
    assert deals.get_message(draft["message_id"], path).status is MessageStatus.SENDING
    with pytest.raises(ValueError, match="already submitted"):
        deals.begin_message_delivery(draft["message_id"], draft["message"], path, authorization=token)


def test_freeze_revokes_unused_permission(setup):
    path, _lead, draft = setup
    token = policy.authorize_single_message(draft["message_id"], path)
    policy.freeze_sending("user cancelled", path)
    assert not policy.single_message_authorized(draft["message_id"], token, path)
    with pytest.raises(PermissionError):
        deals.begin_message_delivery(draft["message_id"], draft["message"], path, authorization=token)
    assert deals.get_message(draft["message_id"], path).status is MessageStatus.PENDING


def test_retry_uses_saved_text_not_a_different_generated_draft(setup):
    path, lead_id, first = setup
    deals.mark_message_failed(first["message_id"], "composer unavailable; nothing submitted", path)
    second = scribe.prepare_outreach(lead_id, preview=True, db_path=path)
    assert second["message_id"] == first["message_id"]
    assert second["message"] == deals.get_message(first["message_id"], path).body
    assert scribe.compose.call_count == 1


def test_sent_record_blocks_retry_even_if_deal_update_was_interrupted(setup):
    path, lead_id, draft = setup
    deals.mark_message_sent(draft["message_id"], path)
    assert scribe.prepare_outreach(lead_id, preview=True, db_path=path)["status"] == "sent"
    assert scribe.compose.call_count == 1


def test_uncertain_delivery_is_not_automatically_retried(setup, monkeypatch):
    path, lead_id, draft = setup

    @asynccontextmanager
    async def page(headless=True):
        yield MagicMock()

    async def ambiguous_send(page, listing, message, *, before_send=None):
        await before_send()
        raise DeliveryUnconfirmed("No receipt after submission")

    send = AsyncMock(side_effect=ambiguous_send)
    monkeypatch.setattr(scribe, "airbnb_page", page)
    monkeypatch.setattr("app.outreach._send_message_to_host", send)
    with pytest.raises(DeliveryUnconfirmed):
        asyncio.run(scribe.send_outreach_for_lead(lead_id, db_path=path))
    asyncio.run(scribe.send_outreach_for_lead(lead_id, db_path=path))
    assert send.await_count == 1
    assert deals.get_message(draft["message_id"], path).status is MessageStatus.SENDING
    # The click may have landed, so this draft is not retried. The rest of the
    # queue stays live: the deal is not escalated and sending is not frozen.
    assert deals.get_deal(draft["deal_id"], path).state is not DealState.NEEDS_HUMAN
    assert budget_status(path)["used"] == 1
    assert policy.sending_enabled(path)


def test_a_refused_message_is_rewritten_rather_than_resent(setup, monkeypatch):
    """Airbnb confirmed it sent nothing, so a fresh draft is the right move."""
    path, lead_id, first = setup

    @asynccontextmanager
    async def page(headless=True):
        yield MagicMock()

    async def refused(page, listing, message, *, before_send=None):
        await before_send()
        raise MessageRejected("Airbnb refused the message", ["Instagram"])

    monkeypatch.setattr(scribe, "airbnb_page", page)
    monkeypatch.setattr("app.outreach._send_message_to_host", AsyncMock(side_effect=refused))
    with pytest.raises(MessageRejected):
        asyncio.run(scribe.send_outreach_for_lead(lead_id, db_path=path))

    assert deals.get_message(first["message_id"], path).status is MessageStatus.FAILED
    # A refusal is a content problem, not a system problem: stay unfrozen and
    # leave the deal in the pipeline instead of paging a human.
    assert policy.sending_enabled(path)
    assert deals.get_deal(first["deal_id"], path).state is not DealState.NEEDS_HUMAN

    scribe.compose.return_value = ("Hi Peggy! I shoot IG reels.", None)
    second = scribe.prepare_outreach(lead_id, preview=True, db_path=path)
    assert second["message_id"] != first["message_id"]
    assert second["message"] == "Hi Peggy! I shoot IG reels."


def test_a_draft_airbnb_would_refuse_is_rewritten_before_any_send(setup, monkeypatch):
    path, lead_id, _first = setup
    drafts = [
        ("Come stay and I'll post Instagram reels.", None),
        ("Come stay and I'll post IG reels.", None),
    ]
    scribe.compose.side_effect = drafts
    deals.mark_message_rejected(_first["message_id"], "superseded", path)

    result = scribe.prepare_outreach(lead_id, preview=True, db_path=path)
    assert result["status"] == "ready"
    assert "Instagram" not in result["message"]


def test_rewriting_gives_up_rather_than_looping(setup, monkeypatch):
    path, lead_id, first = setup
    scribe.compose.side_effect = lambda *a, **k: ("Always Instagram reels.", None)
    deals.mark_message_rejected(first["message_id"], "superseded", path)

    result = scribe.prepare_outreach(lead_id, preview=True, db_path=path)
    # A mechanical block a rewrite cannot fix is dropped, not parked for a human.
    assert result["status"] == "dropped"
    assert scribe.compose.call_count <= 4
    assert deals.get_deal(first["deal_id"], path).state is not DealState.NEEDS_HUMAN


def test_rejected_text_change_does_not_consume_single_permission(setup):
    path, _lead, draft = setup
    token = policy.authorize_single_message(draft["message_id"], path)
    with pytest.raises(ValueError, match="stored outbound draft"):
        deals.begin_message_delivery(draft["message_id"], "A different message", path, authorization=token)
    assert policy.single_message_authorized(draft["message_id"], token, path)


def test_only_one_outreach_attempt_per_listing_across_campaigns(setup):
    path, _lead, draft = setup
    other_deal = deals.upsert_deal("42", campaign_id=2, db_path=path)
    other = deals.record_message(other_deal, "Another draft", db_path=path)
    deals.begin_message_delivery(draft["message_id"], draft["message"], path)
    with pytest.raises(ValueError, match="duplicate outreach"):
        deals.begin_message_delivery(other, "Another draft", path)


def test_no_permission_is_written_on_failure(setup):
    path, _lead, _draft = setup
    with pytest.raises(ValueError):
        policy.authorize_single_message(999999, path)
    conn = get_connection(path)
    try:
        assert conn.execute("SELECT count(*) FROM policy WHERE key = ?", (policy.KEY_SINGLE_SEND,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_dashboard_shows_exact_draft_and_failure_reason(setup):
    from app.agent.chronicler import daily_brief
    from app.api.dashboard import render_dashboard, render_messages

    path, _lead, draft = setup
    deals.mark_message_failed(draft["message_id"], "Login needed; nothing submitted", path)
    html = render_messages(daily_brief(path).get("messages", []))
    assert draft["message"] not in render_dashboard(daily_brief(path))
    assert "Messages — drafts and delivery" in html
    assert draft["message"] in html
    assert "Login needed; nothing submitted" in html
    assert f'Message #{draft["message_id"]}' in html


def test_dashboard_escapes_untrusted_draft_text(setup):
    from app.agent.chronicler import daily_brief
    from app.api.dashboard import render_messages

    path, _lead, draft = setup
    deals.record_message(draft["deal_id"], '<script>alert("not executable")</script>', agent="scribe", db_path=path)
    html = render_messages(daily_brief(path).get("messages", []))
    assert '<script>alert("not executable")</script>' not in html
    assert "&lt;script&gt;" in html