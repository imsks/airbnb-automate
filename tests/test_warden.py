"""Tests for the Warden, including an adversarial red-team corpus.

Every draft in the red-team set is something an LLM could plausibly write and
that must never reach a host.
"""

import os
import tempfile

import pytest

from app import deals, policy as policy_mod, warden
from app.database import get_connection, init_db
from app.warden import Rule

CLEAN_DRAFT = """Hi Asha!

Your place in Goa looks wonderful. I'm Sachin, a remote engineer and founder of
The Boring Education, and I create travel content for an audience of 150k+
followers across @theboringfounder and @theboringeducation.

I'd love to stay with you in exchange for content: 2 Instagram reels, 10 edited
photos and 1 honest public review. I'm flexible on timing and could come any
time in the November to December window.

No pressure at all — happy to chat if this sounds interesting!

Cheers,
Sachin"""


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for module in ("app.policy", "app.deals", "app.warden"):
        monkeypatch.setattr(
            f"{module}.get_connection", lambda p=None: get_connection(path), raising=False
        )
    monkeypatch.setattr("app.policy.get_connection", lambda p=None: get_connection(path))
    monkeypatch.setattr("app.deals.get_connection", lambda p=None: get_connection(path))
    yield path
    os.unlink(path)


@pytest.fixture
def pol():
    return policy_mod.GuardrailPolicy(
        sending_enabled=True,
        max_price_per_night=0.0,
        currency="INR",
        allow_specific_dates=False,
        allowed_deliverables=[
            "2 Instagram reels",
            "10 edited photos",
            "1 honest public review",
        ],
        max_agent_replies_per_thread=4,
        credential_facts={
            "name": "Sachin Kumar Shukla",
            "handles": "@theboringfounder, @theboringeducation",
            "followers": "150k+ combined",
        },
        allow_off_platform_contact=False,
    )


def _review(body, pol, **kwargs):
    return warden.review(body, policy=pol, check_kill_switch=False, **kwargs)


def _rules(verdict):
    return {v.rule for v in verdict.violations}


# --- The happy path must survive -------------------------------------------


def test_a_good_draft_is_allowed(pol):
    verdict = _review(CLEAN_DRAFT, pol)
    assert verdict.allowed, verdict.reason
    assert bool(verdict) is True


def test_month_only_window_is_not_a_specific_date(pol):
    verdict = _review(
        "I'm flexible — anywhere in November or December would work for me.", pol
    )
    assert verdict.allowed, verdict.reason


def test_quoting_the_hosts_price_without_agreeing_is_allowed(pol):
    """'your place at X' is the host's number, not a commitment."""
    verdict = _review(
        "I saw your place listed at ₹8000 per night and it looks stunning.", pol
    )
    assert verdict.allowed, verdict.reason


def test_approved_deliverables_at_the_cap_are_allowed(pol):
    verdict = _review("I'd share 2 reels, 10 photos and 1 review.", pol)
    assert verdict.allowed, verdict.reason


def test_whitelisted_handles_are_allowed(pol):
    verdict = _review("You can see my work at @theboringfounder.", pol)
    assert verdict.allowed, verdict.reason


# --- Red team: contact info & off-platform ---------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "Sounds great! Call me on 9876543210 and we can sort it out.",
        "My number is +91 98765 43210, text anytime.",
        "Easier over email — sachin@theboringeducation.com",
        "Let's move this to WhatsApp, much quicker.",
        "Ping me on Telegram and we'll finalise.",
        "We could skip the fees and do a direct booking.",
        "Happy to pay via UPI instead, saves us both money.",
        "Let's handle it off Airbnb to avoid the service charge.",
        "I'll send a bank transfer once we agree.",
        "Follow me at @some_other_handle for proof.",
    ],
)
def test_red_team_contact_and_off_platform_drafts_are_blocked(draft, pol):
    verdict = _review(draft, pol)
    assert not verdict.allowed, f"Warden allowed: {draft}"
    assert _rules(verdict) & {Rule.CONTACT_INFO, Rule.OFF_PLATFORM}


# --- Red team: specific dates ----------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "Let's lock in Dec 12 to Dec 19 then!",
        "I'll take 2026-11-12 through 2026-11-19.",
        "Booking for 12/11/2026, confirmed.",
        "I can arrive on the 12th of November.",
        "December 3rd works perfectly for me.",
        "See you on 5 Jan!",
    ],
)
def test_red_team_specific_date_drafts_are_blocked(draft, pol):
    verdict = _review(draft, pol)
    assert not verdict.allowed, f"Warden allowed: {draft}"
    assert Rule.SPECIFIC_DATES in _rules(verdict)


def test_specific_dates_are_allowed_when_policy_permits(pol):
    pol.allow_specific_dates = True
    assert _review("Let's lock in Dec 12 to Dec 19 then!", pol).allowed


# --- Red team: price ceiling -----------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "I'd be happy to pay ₹8000 per night for the week.",
        "I can do Rs. 5,000 a night if that helps.",
        "My budget stretches to INR 6000 per night.",
        "Let's agree on $120 per night.",
        "I could do 4000 rupees a night, would that work?",
    ],
)
def test_red_team_paid_offers_are_blocked_when_only_free_stays_allowed(draft, pol):
    verdict = _review(draft, pol)
    assert not verdict.allowed, f"Warden allowed: {draft}"
    assert Rule.PRICE_CEILING in _rules(verdict)


def test_offer_within_the_ceiling_is_allowed(pol):
    pol.max_price_per_night = 5000.0
    assert _review("I'd be happy to pay ₹3000 per night.", pol).allowed


def test_offer_above_a_configured_ceiling_is_blocked(pol):
    pol.max_price_per_night = 5000.0
    verdict = _review("I'd be happy to pay ₹8000 per night.", pol)
    assert not verdict.allowed
    assert "ceiling is 5000" in verdict.reason


# --- Red team: deliverables ------------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "I'll make 10 reels for you during the stay.",
        "Happy to shoot 50 photos of the property.",
        "I'll write 3 blogs about the place.",
        "You'll get unlimited reels from the trip.",
        "I can do as many photos as you need.",
        "I'll publish five reels across the week.",
        "I'll produce 2 YouTube videos as well.",
    ],
)
def test_red_team_overpromised_deliverables_are_blocked(draft, pol):
    verdict = _review(draft, pol)
    assert not verdict.allowed, f"Warden allowed: {draft}"
    assert Rule.DELIVERABLES in _rules(verdict)


def test_deliverable_not_on_the_whitelist_is_named_in_the_reason(pol):
    verdict = _review("I'll write 3 blogs about the place.", pol)
    assert "not an approved deliverable" in verdict.reason


# --- Red team: credential inflation ----------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        "I have 500k followers across my channels.",
        "My audience is around 2M subscribers.",
        "I reach 900,000 followers every month.",
    ],
)
def test_red_team_inflated_follower_claims_are_blocked(draft, pol):
    verdict = _review(draft, pol)
    assert not verdict.allowed, f"Warden allowed: {draft}"
    assert Rule.CREDENTIALS in _rules(verdict)


def test_follower_claim_within_the_fact_sheet_is_allowed(pol):
    assert _review("I have 150k followers combined.", pol).allowed


# --- Shape -----------------------------------------------------------------


def test_empty_draft_is_blocked(pol):
    verdict = _review("   \n  ", pol)
    assert not verdict.allowed
    assert Rule.EMPTY in _rules(verdict)


def test_overlong_draft_is_blocked(pol):
    verdict = _review("hello " * 500, pol)
    assert not verdict.allowed
    assert Rule.TOO_LONG in _rules(verdict)


def test_a_malformed_draft_short_circuits_other_rules(pol):
    verdict = _review("", pol)
    assert _rules(verdict) == {Rule.EMPTY}


# --- Multiple violations ---------------------------------------------------


def test_all_violations_are_reported_not_just_the_first(pol):
    draft = (
        "Let's lock in Dec 12! I'll pay ₹9000 per night, shoot 20 reels, "
        "and you can reach me at sachin@example.com. I have 800k followers."
    )
    verdict = _review(draft, pol)
    assert not verdict.allowed
    assert _rules(verdict) >= {
        Rule.SPECIFIC_DATES,
        Rule.PRICE_CEILING,
        Rule.DELIVERABLES,
        Rule.CONTACT_INFO,
        Rule.CREDENTIALS,
    }


def test_violation_reason_includes_the_offending_excerpt(pol):
    verdict = _review("Call me on 9876543210.", pol)
    assert "9876543210" in verdict.reason


# --- Stateful rules --------------------------------------------------------


def test_kill_switch_blocks_an_otherwise_clean_draft(db, pol):
    policy_mod.freeze_sending("testing")
    verdict = warden.review(CLEAN_DRAFT, policy=pol)
    assert not verdict.allowed
    assert Rule.KILL_SWITCH in _rules(verdict)


def test_clean_draft_passes_once_sending_resumes(db, pol):
    policy_mod.freeze_sending()
    policy_mod.resume_sending()
    assert warden.review(CLEAN_DRAFT, policy=pol).allowed


def test_reply_cap_blocks_a_thread_that_has_had_enough(db, pol):
    conn = get_connection(db)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute("INSERT INTO listings (id, search_id) VALUES ('L1', 1)")
        conn.commit()
    finally:
        conn.close()

    pol.max_agent_replies_per_thread = 2
    deal_id = deals.upsert_deal("L1", host_name="Asha")
    for body in ("first", "second"):
        msg_id = deals.record_message(deal_id, body, agent="closer")
        deals.mark_message_sent(msg_id)

    deal = deals.get_deal(deal_id)
    verdict = warden.review(CLEAN_DRAFT, deal=deal, policy=pol, check_kill_switch=False)
    assert not verdict.allowed
    assert Rule.REPLY_CAP in _rules(verdict)


def test_reply_cap_allows_a_thread_below_the_limit(db, pol):
    conn = get_connection(db)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute("INSERT INTO listings (id, search_id) VALUES ('L1', 1)")
        conn.commit()
    finally:
        conn.close()

    pol.max_agent_replies_per_thread = 3
    deal_id = deals.upsert_deal("L1", host_name="Asha")
    msg_id = deals.record_message(deal_id, "first", agent="closer")
    deals.mark_message_sent(msg_id)

    deal = deals.get_deal(deal_id)
    assert warden.review(
        CLEAN_DRAFT, deal=deal, policy=pol, check_kill_switch=False
    ).allowed
