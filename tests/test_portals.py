"""Booking.com context connector, matcher, and portal_context storage.

Portals are read-only corroboration: any failure must degrade to a clean no-op,
and a weak match must never attach another property's reviews to a host.
"""

import os
import tempfile

import pytest

from app import portals
from app.database import get_connection, init_db, save_listings
from app.leads import save_enrichment, upsert_lead
from app.models import Listing
from app.portals.booking import BookingConnector, PropertyQuery
from app.portals.matcher import MATCH_THRESHOLD, is_match, match_confidence


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


def _listing(**kwargs):
    return Listing(
        id=kwargs.pop("id", "L1"),
        title=kwargs.pop("title", "Cliff Villa Anjuna"),
        location=kwargs.pop("location", "Goa, India"),
        host_name=kwargs.pop("host_name", "Meera"),
        price_per_night=kwargs.pop("price_per_night", 4000.0),
        currency=kwargs.pop("currency", "INR"),
        guests=kwargs.pop("guests", 4),
        **kwargs,
    )


# --- Matcher ---------------------------------------------------------------


def test_identical_property_scores_near_one():
    listing = _listing()
    candidate = {
        "title": "Cliff Villa Anjuna",
        "location": "Goa, India",
        "guests": 4,
        "price_per_night": 4000,
    }
    assert match_confidence(listing, candidate) > 0.95
    assert is_match(listing, candidate)


def test_different_property_is_not_a_match():
    listing = _listing()
    candidate = {
        "title": "Downtown Loft Berlin",
        "location": "Berlin, Germany",
        "guests": 2,
        "price_per_night": 12000,
    }
    assert match_confidence(listing, candidate) < MATCH_THRESHOLD
    assert not is_match(listing, candidate)


def test_missing_fields_never_raise_and_score_low():
    assert match_confidence(_listing(), {}) < MATCH_THRESHOLD


# --- Connector -------------------------------------------------------------


def test_connector_with_no_fetcher_is_a_clean_noop():
    assert BookingConnector().fetch_context(PropertyQuery(listing_id="L1")) is None


def test_connector_swallows_fetcher_errors():
    def boom(_query):
        raise RuntimeError("network down")

    assert BookingConnector(fetcher=boom).fetch_context(PropertyQuery(listing_id="L1")) is None


def test_connector_returns_none_on_empty_candidate():
    assert BookingConnector(fetcher=lambda _q: None).fetch_context(
        PropertyQuery(listing_id="L1")
    ) is None


def test_connector_normalizes_a_raw_candidate():
    raw = {
        "title": "Cliff Villa Anjuna",
        "location": "Goa",
        "price": 4100,
        "rating": 9.2,
        "amenities": ["Wifi", "Pool"],
        "reviews": ["Amazing stay"],
        "url": "https://booking.com/x",
    }
    context = BookingConnector(fetcher=lambda _q: raw).fetch_context(
        PropertyQuery(listing_id="L1", title="Cliff Villa Anjuna")
    )
    assert context["price_per_night"] == 4100
    assert context["rating"] == 9.2
    assert context["amenities"] == ["Wifi", "Pool"]
    assert context["review_excerpts"] == ["Amazing stay"]
    assert context["url"] == "https://booking.com/x"


def test_property_query_is_built_from_a_listing():
    query = PropertyQuery.from_listing(_listing())
    assert query.listing_id == "L1"
    assert query.title == "Cliff Villa Anjuna"
    assert query.guests == 4


# --- Storage ---------------------------------------------------------------


def test_save_context_upserts_one_row_per_portal(db):
    portals.save_context("L1", "booking", {"rating": 8.0}, match_confidence=0.7, db_path=db)
    portals.save_context(
        "L1", "booking", {"rating": 9.0}, external_url="u", match_confidence=0.9, db_path=db
    )
    contexts = portals.get_context_for_listing("L1", db_path=db)
    assert len(contexts) == 1
    assert contexts[0]["payload"]["rating"] == 9.0
    assert contexts[0]["match_confidence"] == 0.9
    assert contexts[0]["external_url"] == "u"


def test_get_context_orders_by_confidence(db):
    portals.save_context("L1", "booking", {}, match_confidence=0.7, db_path=db)
    portals.save_context("L1", "goibibo", {}, match_confidence=0.95, db_path=db)
    contexts = portals.get_context_for_listing("L1", db_path=db)
    assert [c["portal"] for c in contexts] == ["goibibo", "booking"]


def test_listings_missing_context_only_lists_enriched_leads_without_context(db):
    save_listings([_listing(id="L1"), _listing(id="L2")], search_id=None, db_path=db)
    enriched = upsert_lead("L1", campaign_id=7, db_path=db)
    save_enrichment(enriched, {"description": "x"}, db_path=db)
    upsert_lead("L2", campaign_id=7, db_path=db)  # not enriched -> excluded

    missing = portals.listings_missing_context(campaign_id=7, portal="booking", db_path=db)
    assert missing == ["L1"]

    portals.save_context("L1", "booking", {}, db_path=db)
    assert portals.listings_missing_context(campaign_id=7, portal="booking", db_path=db) == []
