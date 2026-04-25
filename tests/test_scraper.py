"""Tests for the scraper module."""

from app.scraper import build_search_url


def test_build_search_url_basic():
    """Test basic search URL construction."""
    url = build_search_url(
        location="Goa, India",
        checkin="2026-06-01",
        checkout="2026-06-07",
        guests=2,
    )
    assert "airbnb.com" in url
    assert "Goa" in url
    assert "checkin=2026-06-01" in url
    assert "checkout=2026-06-07" in url
    assert "adults=2" in url


def test_build_search_url_with_price():
    """Test search URL with price filters."""
    url = build_search_url(
        location="Bali, Indonesia",
        checkin="2026-07-01",
        checkout="2026-07-14",
        guests=2,
        min_price=20,
        max_price=100,
    )
    assert "price_min=20" in url
    assert "price_max=100" in url


def test_build_search_url_no_price():
    """Test that price params are excluded when not provided."""
    url = build_search_url(
        location="Paris, France",
        checkin="2026-08-01",
        checkout="2026-08-05",
    )
    assert "price_min" not in url
    assert "price_max" not in url


def test_build_search_url_location_only():
    """Test search URL with only location (no dates)."""
    url = build_search_url(location="Tokyo, Japan")
    assert "airbnb.com" in url
    assert "Tokyo" in url
    assert "checkin" not in url
    assert "checkout" not in url
    assert "adults=2" in url
