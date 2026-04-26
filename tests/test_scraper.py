"""Tests for the scraper module."""

from app.scraper import (
    build_search_url,
    flexible_trip_nights,
    normalize_flex_duration_unit,
)


def test_build_search_url_basic():
    """Test basic search URL construction (fixed dates)."""
    url = build_search_url(
        location="Goa, India",
        checkin="2026-06-01",
        checkout="2026-06-07",
        guests=2,
        date_mode="fixed",
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
        date_mode="fixed",
    )
    assert "price_min=20" in url
    assert "price_max=100" in url


def test_build_search_url_no_price():
    """Test that price params are excluded when not provided."""
    url = build_search_url(
        location="Paris, France",
        checkin="2026-08-01",
        checkout="2026-08-05",
        date_mode="fixed",
    )
    assert "price_min" not in url
    assert "price_max" not in url


def test_build_search_url_flexible_default():
    """Default mode is flexible trip length (no calendar check-in/out)."""
    url = build_search_url(location="Tokyo, Japan")
    assert "airbnb.com" in url
    assert "Tokyo" in url
    assert "checkin" not in url
    assert "checkout" not in url
    assert "date_picker_type=flexible_dates" in url
    assert "price_filter_num_nights=7" in url
    assert "flexible_trip_lengths[]=one_week" in url
    assert "adults=2" in url


def test_build_search_url_flexible_two_weeks():
    url = build_search_url(
        "Lisbon, Portugal",
        flex_duration=2,
        flex_duration_unit="week",
    )
    assert "price_filter_num_nights=14" in url
    assert "one_week" not in url


def test_flexible_trip_nights_and_normalize():
    assert flexible_trip_nights(3, "day") == 3
    assert flexible_trip_nights(2, "week") == 14
    assert flexible_trip_nights(1, "month") == 28
    assert normalize_flex_duration_unit("weeks") == "week"
