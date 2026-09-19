"""Tests for the deal repository and state machine."""

import os
import tempfile

import pytest

from app import deals
from app.database import get_connection, init_db
from app.deals import IllegalTransition
from app.models import (
    DealState,
    MessageDirection,
    MessageKind,
    MessageStatus,
    allowed_deal_transitions,
)


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr("app.deals.get_connection", lambda p=None: get_connection(path))
    _seed_listing(path, "L1")
    _seed_listing(path, "L2")
    yield path
    os.unlink(path)


def _seed_listing(path, listing_id):
    conn = get_connection(path)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute("INSERT INTO listings (id, search_id) VALUES (?, 1)", (listing_id,))
        conn.commit()
    finally:
        conn.close()


def _deal(**kwargs):
    return deals.upsert_deal(
        kwargs.pop("listing_id", "L1"),
        host_name=kwargs.pop("host_name", "Asha"),
        place_name=kwargs.pop("place_name", "Sea Villa"),
        location=kwargs.pop("location", "Goa, India"),
        **kwargs,
    )


# --- Creation --------------------------------------------------------------


def test_upsert_creates_deal_in_discovered_state(db):
    deal_id = _deal()
    deal = deals.get_deal(deal_id)
    assert deal.state is DealState.DISCOVERED
    assert deal.host_name == "Asha"
    assert deal.is_linked is False


def test_upsert_is_idempotent_per_listing_and_campaign(db):
    assert _deal() == _deal()


def test_creation_records_an_opening_event(db):
    events = deals.get_events(_deal())
    assert len(events) == 1
    assert events[0].to_state == DealState.DISCOVERED.value


# --- State machine ---------------------------------------------------------


def test_legal_transition_moves_state_and_logs_event(db):
    deal_id = _deal()
    deal = deals.transition(
        deal_id, DealState.QUALIFIED, reason="score 0.82", actor="analyst"
    )
    assert deal.state is DealState.QUALIFIED
    events = deals.get_events(deal_id)
    assert events[-1].from_state == DealState.DISCOVERED.value
    assert events[-1].to_state == DealState.QUALIFIED.value
    assert events[-1].actor == "analyst"


def test_illegal_transition_is_refused(db):
    deal_id = _deal()
    with pytest.raises(IllegalTransition):
        deals.transition(deal_id, DealState.BOOKED)
    assert deals.get_deal(deal_id).state is DealState.DISCOVERED


def test_illegal_transition_leaves_no_event(db):
    deal_id = _deal()
    with pytest.raises(IllegalTransition):
        deals.transition(deal_id, DealState.TERMS_AGREED)
    assert len(deals.get_events(deal_id)) == 1


def test_full_happy_path_is_walkable(db):
    deal_id = _deal()
    path = [
        DealState.QUALIFIED,
        DealState.CONTACTED,
        DealState.HOST_REPLIED,
        DealState.NEGOTIATING,
        DealState.TERMS_AGREED,
        DealState.READY_TO_BOOK,
        DealState.BOOKED,
        DealState.STAYED,
        DealState.CONTENT_DELIVERED,
    ]
    for state in path:
        deals.transition(deal_id, state)
    assert deals.get_deal(deal_id).state is DealState.CONTENT_DELIVERED
    assert len(deals.get_events(deal_id)) == len(path) + 1


def test_any_live_state_can_escalate_to_needs_human(db):
    """The Warden's escape hatch must never be blocked."""
    live = [
        DealState.DISCOVERED,
        DealState.QUALIFIED,
        DealState.CONTACTED,
        DealState.HOST_REPLIED,
        DealState.NEGOTIATING,
        DealState.TERMS_AGREED,
        DealState.READY_TO_BOOK,
        DealState.BOOKED,
        DealState.STAYED,
    ]
    for state in live:
        assert DealState.NEEDS_HUMAN in allowed_deal_transitions(state)


def test_terminal_states_cannot_escalate(db):
    for state in (DealState.REJECTED, DealState.DISQUALIFIED, DealState.CONTENT_DELIVERED):
        assert allowed_deal_transitions(state) == frozenset()


def test_human_can_resolve_a_needs_human_deal(db):
    deal_id = _deal()
    deals.transition(deal_id, DealState.QUALIFIED)
    deals.transition(deal_id, DealState.NEEDS_HUMAN, reason="warden blocked reply")
    deals.transition(deal_id, DealState.REJECTED, actor="human")
    assert deals.get_deal(deal_id).state is DealState.REJECTED


def test_negotiating_can_loop_on_itself(db):
    deal_id = _deal()
    for state in (DealState.QUALIFIED, DealState.CONTACTED, DealState.HOST_REPLIED):
        deals.transition(deal_id, state)
    deals.transition(deal_id, DealState.NEGOTIATING)
    deals.transition(deal_id, DealState.NEGOTIATING, reason="round 2")
    assert deals.get_deal(deal_id).state is DealState.NEGOTIATING


def test_stale_deal_revives_when_host_finally_replies(db):
    deal_id = _deal()
    deals.transition(deal_id, DealState.QUALIFIED)
    deals.transition(deal_id, DealState.CONTACTED)
    deals.transition(deal_id, DealState.STALE, reason="14 days silent")
    deals.transition(deal_id, DealState.HOST_REPLIED)
    assert deals.get_deal(deal_id).state is DealState.HOST_REPLIED


# --- Thread linking --------------------------------------------------------


def test_link_thread_connects_deal_to_inbox(db):
    deal_id = _deal()
    assert deals.link_thread(deal_id, "T100", thread_url="/messages/T100")
    deal = deals.get_deal(deal_id)
    assert deal.is_linked
    assert deal.thread_linked_via == "send_capture"
    assert deals.get_deal_by_thread("T100").id == deal_id


def test_thread_cannot_be_linked_to_two_deals(db):
    first = _deal(listing_id="L1")
    second = _deal(listing_id="L2")
    assert deals.link_thread(first, "T100")
    assert deals.link_thread(second, "T100") is False
    assert deals.get_deal(second).is_linked is False


def test_relinking_the_same_deal_is_allowed(db):
    deal_id = _deal()
    deals.link_thread(deal_id, "T100")
    assert deals.link_thread(deal_id, "T100", via="fuzzy_match")
    assert deals.get_deal(deal_id).thread_linked_via == "fuzzy_match"


# --- Messages --------------------------------------------------------------


def test_record_message_appends_to_thread(db):
    deal_id = _deal()
    deals.record_message(deal_id, "hello there", agent="scribe")
    (msg,) = deals.get_messages(deal_id)
    assert msg.body == "hello there"
    assert msg.direction is MessageDirection.OUTBOUND
    assert msg.status is MessageStatus.PENDING


def test_idempotency_key_prevents_a_second_copy(db):
    """A retried send job must not produce a second message."""
    deal_id = _deal()
    first = deals.record_message(deal_id, "hello", idempotency_key="send-1")
    second = deals.record_message(deal_id, "hello", idempotency_key="send-1")
    assert first == second
    assert len(deals.get_messages(deal_id)) == 1


def test_marking_sent_stamps_the_deal(db):
    deal_id = _deal()
    msg_id = deals.record_message(deal_id, "hello")
    deals.mark_message_sent(msg_id)
    assert deals.get_messages(deal_id)[0].status is MessageStatus.SENT
    assert deals.get_deal(deal_id).last_outbound_at is not None


def test_blocked_message_records_the_reason(db):
    deal_id = _deal()
    msg_id = deals.record_message(deal_id, "call me on 9876543210")
    deals.mark_message_blocked(msg_id, "contains a phone number")
    msg = deals.get_messages(deal_id)[0]
    assert msg.status is MessageStatus.BLOCKED
    assert msg.blocked_reason == "contains a phone number"


def test_conversation_text_omits_blocked_and_failed_drafts(db):
    deal_id = _deal()
    sent = deals.record_message(deal_id, "Hi Asha!")
    deals.mark_message_sent(sent)
    deals.record_inbound(deal_id, "Hi, tell me more.")
    blocked = deals.record_message(deal_id, "here is my phone number")
    deals.mark_message_blocked(blocked, "contact info")

    text = deals.conversation_text(deal_id)
    assert "Me: Hi Asha!" in text
    assert "Host: Hi, tell me more." in text
    assert "phone number" not in text


def test_agent_reply_count_only_counts_delivered_agent_messages(db):
    deal_id = _deal()
    sent = deals.record_message(deal_id, "one", agent="closer")
    deals.mark_message_sent(sent)
    deals.record_message(deal_id, "two", agent="closer")  # still pending
    blocked = deals.record_message(deal_id, "three", agent="closer")
    deals.mark_message_blocked(blocked, "nope")
    assert deals.count_agent_replies(deal_id) == 1


def test_inbound_messages_are_deduplicated(db):
    deal_id = _deal()
    assert deals.record_inbound(deal_id, "Sure!", external_ts="10:00")
    assert deals.record_inbound(deal_id, "Sure!", external_ts="10:00") is None
    assert deals.record_inbound(deal_id, "Sure!", external_ts="11:00")
    assert len(deals.get_messages(deal_id)) == 2


def test_inbound_message_stamps_last_inbound(db):
    deal_id = _deal()
    deals.record_inbound(deal_id, "Interested")
    deal = deals.get_deal(deal_id)
    assert deal.last_inbound_at is not None
    assert deals.get_messages(deal_id)[0].kind is MessageKind.HOST


# --- Terms & reporting -----------------------------------------------------


def test_set_terms_persists_the_agreed_deal(db):
    deal_id = _deal()
    deals.set_terms(
        deal_id,
        price_per_night=0.0,
        currency="INR",
        discount_pct=100.0,
        window_start="2026-11-01",
        window_end="2026-11-30",
        nights=7,
        deliverables=["2 reels", "10 photos"],
        confidence=0.9,
    )
    deal = deals.get_deal(deal_id)
    assert deal.agreed_discount_pct == 100.0
    assert deal.agreed_deliverables == ["2 reels", "10 photos"]
    assert deal.agreed_window_start == "2026-11-01"


def test_funnel_counts_cover_every_state(db):
    first = _deal(listing_id="L1")
    deals.transition(first, DealState.QUALIFIED)
    _deal(listing_id="L2")

    counts = deals.funnel_counts()
    assert counts[DealState.DISCOVERED.value] == 1
    assert counts[DealState.QUALIFIED.value] == 1
    assert counts[DealState.BOOKED.value] == 0
    assert set(counts) == {s.value for s in DealState}


def test_get_deals_by_state_filters(db):
    first = _deal(listing_id="L1")
    deals.transition(first, DealState.QUALIFIED)
    _deal(listing_id="L2")

    qualified = deals.get_deals_by_state(DealState.QUALIFIED)
    assert [d.id for d in qualified] == [first]
    assert len(deals.get_deals_by_state(DealState.QUALIFIED, DealState.DISCOVERED)) == 2
