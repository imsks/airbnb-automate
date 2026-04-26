"""Tests for the scraper module."""

from datetime import date

from app.scraper import (
    build_search_url,
    flexible_trip_nights,
    location_path_slug,
    normalize_flex_duration_unit,
)


def test_location_path_slug_two_part():
    assert location_path_slug("Dehradun, Uttarakhand") == "Dehradun--Uttarakhand"


def test_build_search_url_basic():
    """Fixed dates use calendar query params."""
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
    url = build_search_url(
        location="Paris, France",
        checkin="2026-08-01",
        checkout="2026-08-05",
        date_mode="fixed",
    )
    assert "price_min" not in url
    assert "price_max" not in url


def test_build_search_url_flexible_week_structured():
    ref = date(2026, 6, 1)
    url = build_search_url(
        "Dehradun, Uttarakhand",
        flex_duration=1,
        flex_duration_unit="week",
        flex_trip_months_count=3,
        reference_date=ref,
        airbnb_base_url="https://www.airbnb.co.in",
    )
    assert "airbnb.co.in" in url
    assert "Dehradun--Uttarakhand" in url
    assert "date_picker_type=flexible_dates" in url
    assert "one_week" in url
    assert "june" in url
    assert "july" in url
    assert "august" in url
    assert "monthly_start_date=2026-06-01" in url
    assert "price_filter_num_nights=7" in url
    assert "refinement_paths" in url


def test_build_search_url_flexible_month_structured():
    ref = date(2026, 6, 1)
    url = build_search_url(
        "Dehradun, Uttarakhand",
        flex_duration=1,
        flex_duration_unit="month",
        flex_trip_months_count=3,
        reference_date=ref,
    )
    assert "one_month" in url
    assert "price_filter_num_nights=28" in url


def test_build_search_url_flexible_weekend():
    ref = date(2026, 6, 1)
    url = build_search_url(
        "Dehradun, Uttarakhand",
        flex_duration_unit="weekend",
        flex_trip_months_count=3,
        reference_date=ref,
    )
    assert "weekend_trip" in url
    assert "price_filter_num_nights=5" in url


def test_build_search_url_flexible_two_weeks():
    ref = date(2026, 6, 1)
    url = build_search_url(
        "Lisbon, Portugal",
        flex_duration=2,
        flex_duration_unit="week",
        flex_trip_months_count=3,
        reference_date=ref,
    )
    assert "price_filter_num_nights=14" in url
    assert "one_week" not in url


def test_build_search_url_flexible_day_minimal():
    ref = date(2026, 6, 1)
    url = build_search_url(
        "Tokyo, Japan",
        flex_duration=3,
        flex_duration_unit="day",
        reference_date=ref,
    )
    assert "date_picker_type=flexible_dates" in url
    assert "price_filter_num_nights=3" in url
    assert "flexible_trip_dates" not in url


def test_flexible_trip_nights_and_normalize():
    assert flexible_trip_nights(3, "day") == 3
    assert flexible_trip_nights(2, "week") == 14
    assert flexible_trip_nights(1, "month") == 28
    assert flexible_trip_nights(1, "weekend") == 5
    assert normalize_flex_duration_unit("weeks") == "week"
    assert normalize_flex_duration_unit("weekend") == "weekend"
