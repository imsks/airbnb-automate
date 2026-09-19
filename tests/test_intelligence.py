"""Tests for the intelligence layer: leads, Analyst, Scout parsing, Router."""

import os
import tempfile

import pytest

from app import leads as lead_repo
from app import territories as territory_repo
from app.agent.analyst import WEIGHTS, score_lead
from app.agent.router import (
    campaign_months,
    month_name,
    plan_route,
    region_of,
    route_affinity,
    score_stop,
)
from app.agent.scout import parse_scout_response
from app.database import get_connection, init_db
from app.listing_detail import (
    detect_long_stay_discount,
    listing_url_for,
    parse_listing_age_months,
    parse_response_rate,
)
from app.models import Campaign, Lead, Listing, Territory, TerritoryProfile


@pytest.fixture
def db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for module in ("app.leads", "app.territories"):
        monkeypatch.setattr(f"{module}.get_connection", lambda p=None: get_connection(path))
    conn = get_connection(path)
    try:
        conn.execute("INSERT INTO searches (location) VALUES ('Goa, India')")
        conn.execute("INSERT INTO listings (id, search_id) VALUES ('L1', 1), ('L2', 1)")
        conn.commit()
    finally:
        conn.close()
    yield path
    os.unlink(path)


# --- Lead repository -------------------------------------------------------


def test_upsert_lead_is_idempotent(db):
    assert lead_repo.upsert_lead("L1") == lead_repo.upsert_lead("L1")


def test_enrichment_round_trips(db):
    lead_id = lead_repo.upsert_lead("L1")
    lead_repo.save_enrichment(
        lead_id,
        {
            "description": "A sunlit villa above the beach.",
            "amenities": ["Wifi", "Dedicated workspace"],
            "review_excerpts": ["Loved the sunrise deck"],
            "host_is_superhost": False,
            "has_long_stay_discount": True,
            "instant_book": False,
            "listing_age_months": 8,
            "host_response_rate": "95%",
        },
    )
    lead = lead_repo.get_lead(lead_id)
    assert lead.is_enriched
    assert lead.amenities == ["Wifi", "Dedicated workspace"]
    assert lead.has_long_stay_discount is True
    assert lead.listing_age_months == 8


def test_leads_needing_enrichment_excludes_enriched(db):
    first = lead_repo.upsert_lead("L1")
    lead_repo.upsert_lead("L2")
    assert len(lead_repo.leads_needing_enrichment()) == 2
    lead_repo.save_enrichment(first, {"description": "x"})
    assert [l.listing_id for l in lead_repo.leads_needing_enrichment()] == ["L2"]


def test_leads_needing_scoring_requires_enrichment_first(db):
    lead_id = lead_repo.upsert_lead("L1")
    assert lead_repo.leads_needing_scoring() == []
    lead_repo.save_enrichment(lead_id, {"description": "x"})
    assert len(lead_repo.leads_needing_scoring()) == 1
    lead_repo.save_score(lead_id, 0.7, {"host_hunger": 0.8})
    assert lead_repo.leads_needing_scoring() == []


def test_top_unsent_leads_ranks_by_score(db):
    first = lead_repo.upsert_lead("L1")
    second = lead_repo.upsert_lead("L2")
    for lead_id, score in ((first, 0.4), (second, 0.9)):
        lead_repo.save_enrichment(lead_id, {"description": "x"})
        lead_repo.save_score(lead_id, score, {})
    assert [l.listing_id for l in lead_repo.top_unsent_leads()] == ["L2", "L1"]


def test_top_unsent_leads_skips_contacted_deals(db, monkeypatch):
    from app import deals as deal_repo

    monkeypatch.setattr("app.deals.get_connection", lambda p=None: get_connection(db))
    lead_id = lead_repo.upsert_lead("L1")
    lead_repo.save_enrichment(lead_id, {"description": "x"})
    lead_repo.save_score(lead_id, 0.9, {})

    from app.models import DealState

    deal_id = deal_repo.upsert_deal("L1")
    assert len(lead_repo.top_unsent_leads()) == 1
    deal_repo.advance_to(deal_id, DealState.CONTACTED)
    assert lead_repo.top_unsent_leads() == []


# --- Detail page parsing ---------------------------------------------------


def test_listing_url_prefers_the_known_url():
    assert listing_url_for("123", "https://x/rooms/123") == "https://x/rooms/123"
    assert listing_url_for("123").endswith("/rooms/123")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3 years hosting", 36),
        ("1 year hosting", 12),
        ("7 months hosting", 7),
        ("Superhost", None),
    ],
)
def test_parse_listing_age_months(text, expected):
    assert parse_listing_age_months(text) == expected


def test_parse_response_rate():
    assert parse_response_rate("Response rate: 98%") == "98%"
    assert parse_response_rate("no data here") == ""


@pytest.mark.parametrize(
    "text",
    ["Weekly discount applied", "Monthly stay discount", "Stay 7+ nights for less"],
)
def test_detect_long_stay_discount(text):
    assert detect_long_stay_discount(text) is True


def test_no_long_stay_discount_detected():
    assert detect_long_stay_discount("Entire villa, 3 bedrooms") is False


# --- Analyst ---------------------------------------------------------------


def _lead(**kwargs):
    return Lead(listing_id="L1", **kwargs)


def test_analyst_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_hungry_host_outscores_established_superhost():
    hungry = _lead(
        host_is_superhost=False,
        has_long_stay_discount=True,
        instant_book=False,
        listing_age_months=4,
        description="Sunlit villa with pool and a view, fast wifi and a desk.",
    )
    hungry_listing = Listing(id="L1", review_count=2, price_per_night=1800)

    established = _lead(
        host_is_superhost=True,
        has_long_stay_discount=False,
        instant_book=True,
        listing_age_months=96,
        description="Luxury apartment.",
    )
    established_listing = Listing(
        id="L2", review_count=340, price_per_night=14000, superhost=True
    )

    hungry_score, _, _ = score_lead(hungry, hungry_listing)
    established_score, _, _ = score_lead(established, established_listing)
    assert hungry_score > established_score
    assert hungry_score > 0.7


def test_score_is_bounded_and_breakdown_is_complete():
    score, breakdown, rationale = score_lead(_lead(), Listing(id="L1"))
    assert 0.0 <= score <= 1.0
    assert set(breakdown) == set(WEIGHTS)
    assert rationale


def test_instant_book_reduces_negotiability():
    manual, _, _ = score_lead(_lead(instant_book=False), Listing(id="L1"))
    instant, _, _ = score_lead(_lead(instant_book=True), Listing(id="L1"))
    assert manual > instant


def test_long_stay_discount_raises_the_score():
    with_discount, _, _ = score_lead(_lead(has_long_stay_discount=True), Listing(id="L1"))
    without, _, _ = score_lead(_lead(has_long_stay_discount=False), Listing(id="L1"))
    assert with_discount > without


def test_cheaper_listing_scores_higher():
    cheap, _, _ = score_lead(_lead(), Listing(id="L1", price_per_night=1000))
    pricey, _, _ = score_lead(_lead(), Listing(id="L1", price_per_night=20000))
    assert cheap > pricey


# --- Scout parsing ---------------------------------------------------------


_GOOD_SCOUT_JSON = """```json
{
  "summary": "Great in winter, unbearable in monsoon.",
  "seasonality": {"november": 0.95, "december": 0.9, "july": 0.05},
  "connectivity_score": 0.8,
  "connectivity_note": "Reliable 4G across the coast.",
  "content_score": 0.9,
  "content_angles": ["beach shacks", "Portuguese heritage"],
  "cost_band": "low",
  "cost_score": 0.8,
  "events": ["december: Sunburn"],
  "caveats": ["Crowded over New Year"]
}
```"""


def test_scout_response_parses_through_markdown_fences():
    parsed = parse_scout_response(_GOOD_SCOUT_JSON)
    assert parsed["summary"].startswith("Great in winter")
    assert parsed["seasonality"]["november"] == 0.95
    assert parsed["cost_band"] == "low"
    assert parsed["content_angles"] == ["beach shacks", "Portuguese heritage"]


def test_unresearched_months_default_to_zero_not_neutral():
    """An unknown month must never look like a viable one."""
    parsed = parse_scout_response(_GOOD_SCOUT_JSON)
    assert len(parsed["seasonality"]) == 12
    assert parsed["seasonality"]["march"] == 0.0


def test_scout_scores_are_clamped():
    parsed = parse_scout_response(
        '{"connectivity_score": 7, "content_score": -3, "seasonality": {"may": 42}}'
    )
    assert parsed["connectivity_score"] == 1.0
    assert parsed["content_score"] == 0.0
    assert parsed["seasonality"]["may"] == 1.0


def test_invalid_cost_band_is_dropped():
    assert parse_scout_response('{"cost_band": "cheapish"}')["cost_band"] == ""


def test_scout_response_without_json_raises():
    with pytest.raises(ValueError, match="no JSON"):
        parse_scout_response("I could not research that place.")


# --- Territory repository --------------------------------------------------


def test_profile_save_marks_previous_as_stale(db):
    territory_id = territory_repo.upsert_territory("Goa, India")
    for score in (0.5, 0.9):
        territory_repo.save_profile(
            TerritoryProfile(territory_id=territory_id, content_score=score)
        )
    current = territory_repo.get_current_profile(territory_id)
    assert current.content_score == 0.9

    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM territory_profiles").fetchone()[0] == 2
    finally:
        conn.close()


def test_researching_a_territory_marks_it_researched(db):
    territory_id = territory_repo.upsert_territory("Goa, India")
    assert territory_repo.territories_needing_research()
    territory_repo.save_profile(TerritoryProfile(territory_id=territory_id))
    assert territory_repo.territories_needing_research() == []
    assert territory_repo.get_territory(territory_id).status.value == "researched"


def test_saturation_grows_with_sends(db):
    territory_id = territory_repo.upsert_territory("Goa, India")
    for _ in range(5):
        territory_repo.record_send(territory_id)
    territory = territory_repo.get_territory(territory_id)
    assert territory.messages_sent == 5
    assert territory_repo.saturation(territory, cap=10) == 0.5


# --- Router ----------------------------------------------------------------


def test_region_detection():
    assert region_of("Manali, Himachal Pradesh") == "north"
    assert region_of("Wayanad, Kerala") == "south"
    assert region_of("Shillong, Meghalaya") == "northeast"
    assert region_of("Atlantis") == ""


def test_route_affinity_prefers_nearby_hops():
    assert route_affinity("Manali, Himachal", "Shimla, Himachal") == 1.0
    assert route_affinity("Manali, Himachal", "Wayanad, Kerala") < 0.3
    assert route_affinity("Atlantis", "Narnia") == 0.5


def test_campaign_months_walks_the_window():
    campaign = Campaign(window_start="2026-11", window_end="2027-02")
    assert campaign_months(campaign) == ["2026-11", "2026-12", "2027-01", "2027-02"]


def test_campaign_months_rolls_over_the_year():
    campaign = Campaign(window_start="2026-12", window_end="2027-01")
    assert campaign_months(campaign) == ["2026-12", "2027-01"]


def test_month_name_matches_scout_keys():
    assert month_name("2026-11") == "november"
    assert month_name("garbage") == ""


def _territory(tid, name, sent=0):
    return Territory(id=tid, name=name, messages_sent=sent)


def _profile(tid, seasonality, **kwargs):
    return TerritoryProfile(territory_id=tid, seasonality=seasonality, **kwargs)


def test_seasonality_dominates_stop_scoring():
    territory = _territory(1, "Manali, Himachal Pradesh")
    good = _profile(1, {"november": 0.9})
    bad = _profile(1, {"november": 0.0})
    assert score_stop(territory, good, "2026-11", "")[0] > score_stop(
        territory, bad, "2026-11", ""
    )[0]


def test_route_plans_one_stop_per_month():
    campaign = Campaign(window_start="2026-11", window_end="2026-12", origin="Delhi")
    territories = [
        _territory(1, "Manali, Himachal Pradesh"),
        _territory(2, "Goa, India"),
    ]
    profiles = {
        1: _profile(1, {"november": 0.9, "december": 0.2}),
        2: _profile(2, {"november": 0.6, "december": 0.9}),
    }
    stops = plan_route(campaign, territories, profiles)
    assert [s.target_month for s in stops] == ["2026-11", "2026-12"]
    assert [s.seq for s in stops] == [1, 2]
    assert stops[0].territory_name == "Manali, Himachal Pradesh"
    assert stops[1].territory_name == "Goa, India"


def test_a_territory_is_never_scheduled_twice():
    campaign = Campaign(window_start="2026-11", window_end="2027-01")
    territories = [_territory(1, "Goa, India")]
    profiles = {1: _profile(1, {m: 0.9 for m in ("november", "december", "january")})}
    stops = plan_route(campaign, territories, profiles)
    assert len(stops) == 1


def test_out_of_season_months_are_left_empty_rather_than_badly_filled():
    """A snowed-in pass is never worth visiting, however cheap it is."""
    campaign = Campaign(window_start="2027-01", window_end="2027-01")
    territories = [_territory(1, "Manali, Himachal Pradesh")]
    profiles = {1: _profile(1, {"january": 0.05}, cost_score=1.0, content_score=1.0)}
    assert plan_route(campaign, territories, profiles) == []


def test_saturated_territory_loses_to_a_fresh_one():
    campaign = Campaign(window_start="2026-11", window_end="2026-11")
    territories = [
        _territory(1, "Goa, India", sent=100),
        _territory(2, "Gokarna, Karnataka"),
    ]
    profiles = {
        1: _profile(1, {"november": 0.9}),
        2: _profile(2, {"november": 0.9}),
    }
    stops = plan_route(campaign, territories, profiles)
    assert stops[0].territory_name == "Gokarna, Karnataka"


def test_unresearched_territory_is_not_scheduled():
    campaign = Campaign(window_start="2026-11", window_end="2026-11")
    stops = plan_route(campaign, [_territory(1, "Goa, India")], {1: None})
    assert stops[0].score < 0.2
