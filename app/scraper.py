"""Airbnb listing scraper using Playwright browser automation."""

from __future__ import annotations

import logging
import re
import asyncio
from datetime import date
from typing import Optional
from urllib.parse import quote, urlencode

from playwright.async_api import async_playwright, Page

from app.browser_session import close_airbnb_session, open_airbnb_browser
from app.config import get_airbnb_base_url, get_flex_trip_months_count
from app.models import Listing

logger = logging.getLogger(__name__)

_FLEX_PRESET_WEEKEND = "weekend_trip"
_FLEX_PRESET_WEEK = "one_week"
_FLEX_PRESET_MONTH = "one_month"

_ENGLISH_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def normalize_flex_duration_unit(unit: str) -> str:
    """Normalize to day | week | month | weekend."""
    u = (unit or "week").strip().lower().rstrip("s")
    if u == "weekend":
        return "weekend"
    if u in ("day", "week", "month"):
        return u
    raise ValueError(f"Invalid flex duration unit: {unit!r}")


def location_path_slug(location: str) -> str:
    """Path segment for ``/s/{slug}/homes`` (e.g. ``Dehradun--Uttarakhand``)."""
    raw = (location or "").strip()
    if not raw:
        return "homes"
    parts = [p.strip() for p in re.split(r"[,，]", raw) if p.strip()]
    if len(parts) >= 2:
        slug = "--".join(parts)
    else:
        slug = re.sub(r"\s+", "--", parts[0])
    return quote(slug, safe="-–—")


def upcoming_flex_calendar_months(
    count: int,
    *,
    ref: Optional[date] = None,
) -> list[tuple[int, int]]:
    """Consecutive ``(year, month)`` tuples starting from ``ref``'s month."""
    d = ref or date.today()
    y, m = d.year, d.month
    out: list[tuple[int, int]] = []
    for _ in range(max(1, count)):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def flexible_monthly_window_strings(
    months: list[tuple[int, int]],
) -> tuple[str, int, str]:
    """``monthly_start_date``, ``monthly_length``, ``monthly_end_date`` (end exclusive)."""
    if not months:
        today = date.today()
        months = [(today.year, today.month)]
    start = date(months[0][0], months[0][1], 1)
    last_y, last_m = months[-1][0], months[-1][1]
    end_m = last_m + 1
    end_y = last_y
    if end_m > 12:
        end_m = 1
        end_y += 1
    end = date(end_y, end_m, 1)
    return start.isoformat(), len(months), end.isoformat()


def flexible_trip_nights(duration: int, unit: str) -> int:
    """Night count for ``price_filter_num_nights``."""
    u = normalize_flex_duration_unit(unit)
    d = max(1, int(duration))
    if u == "weekend":
        return 5
    if u == "day":
        return d
    if u == "week":
        return d * 7
    return d * 28


def _flexible_trip_length_tokens(duration: int, unit: str) -> list[str]:
    """``flexible_trip_lengths[]`` values."""
    u = normalize_flex_duration_unit(unit)
    d = max(1, int(duration))
    if u == "weekend":
        return [_FLEX_PRESET_WEEKEND]
    if u == "week" and d == 1:
        return [_FLEX_PRESET_WEEK]
    if u == "month" and d == 1:
        return [_FLEX_PRESET_MONTH]
    return []


def build_search_url(
    location: str,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    *,
    date_mode: str = "flexible",
    flex_duration: int = 1,
    flex_duration_unit: str = "week",
    flex_trip_months_count: Optional[int] = None,
    reference_date: Optional[date] = None,
    airbnb_base_url: Optional[str] = None,
) -> str:
    """Build an Airbnb search URL (structured flexible URLs for week/month/weekend)."""
    base = (airbnb_base_url or get_airbnb_base_url()).rstrip("/")
    path_slug = location_path_slug(location)
    mode = (date_mode or "flexible").strip().lower()

    if mode == "fixed":
        params: list[tuple[str, str]] = [
            ("query", location.strip()),
            ("adults", str(int(guests))),
        ]
        if checkin:
            params.append(("checkin", checkin))
        if checkout:
            params.append(("checkout", checkout))
        if min_price is not None:
            params.append(("price_min", str(int(min_price))))
        if max_price is not None:
            params.append(("price_max", str(int(max_price))))
        q = urlencode(params, doseq=True)
        return f"{base}/s/{path_slug}/homes?{q}"

    u = normalize_flex_duration_unit(flex_duration_unit)
    d = max(1, int(flex_duration))
    if u == "weekend":
        d = 1

    nights = flexible_trip_nights(d, u)
    month_count = (
        flex_trip_months_count
        if flex_trip_months_count is not None
        else get_flex_trip_months_count()
    )
    ym = upcoming_flex_calendar_months(month_count, ref=reference_date)
    month_names = [_ENGLISH_MONTHS[mm - 1] for _, mm in ym]
    monthly_start, monthly_len, monthly_end = flexible_monthly_window_strings(ym)

    if u == "day":
        params_day: list[tuple[str, str]] = [
            ("query", location.strip()),
            ("adults", str(int(guests))),
            ("date_picker_type", "flexible_dates"),
            ("price_filter_num_nights", str(nights)),
        ]
        if min_price is not None:
            params_day.append(("price_min", str(int(min_price))))
        if max_price is not None:
            params_day.append(("price_max", str(int(max_price))))
        qd = urlencode(params_day, doseq=True)
        return f"{base}/s/{path_slug}/homes?{qd}"

    pairs: list[tuple[str, str]] = [
        ("refinement_paths[]", "/homes"),
        ("date_picker_type", "flexible_dates"),
        ("adults", str(int(guests))),
        ("search_type", "unknown"),
        ("query", location.strip()),
        ("monthly_start_date", monthly_start),
        ("monthly_length", str(monthly_len)),
        ("monthly_end_date", monthly_end),
        ("search_mode", "regular_search"),
        ("price_filter_input_type", "1"),
        ("price_filter_num_nights", str(nights)),
        ("channel", "EXPLORE"),
        ("source", "structured_search_input_header"),
    ]
    for mn in month_names:
        pairs.append(("flexible_trip_dates[]", mn))
    for tok in _flexible_trip_length_tokens(d, u):
        pairs.append(("flexible_trip_lengths[]", tok))

    if min_price is not None:
        pairs.append(("price_min", str(int(min_price))))
    if max_price is not None:
        pairs.append(("price_max", str(int(max_price))))

    q = urlencode(pairs, doseq=True)
    return f"{base}/s/{path_slug}/homes?{q}"


async def _extract_listings_from_page(page: Page, location: str) -> list[Listing]:
    """Extract listing data from the current search results page.

    Parses the search results page DOM to extract listing cards
    with title, price, rating, host info, and URLs.
    """
    listings = []

    # Wait for listing cards to load
    try:
        await page.wait_for_selector(
            '[itemprop="itemListElement"], [data-testid="card-container"]',
            timeout=15000,
        )
    except Exception:
        logger.warning("No listing cards found on page")
        return listings

    # Extract data from listing cards
    cards = await page.query_selector_all(
        '[itemprop="itemListElement"], [data-testid="card-container"]'
    )

    for card in cards:
        try:
            listing = await _parse_listing_card(card, location)
            if listing and listing.title:
                listings.append(listing)
        except Exception as e:
            logger.debug("Failed to parse listing card: %s", e)
            continue

    return listings


async def _parse_listing_card(card, location: str) -> Optional[Listing]:
    """Parse a single listing card element into a Listing model."""
    listing = Listing(location=location)

    # Extract title
    title_el = await card.query_selector(
        '[data-testid="listing-card-title"], '
        '[id^="title_"]'
    )
    if title_el:
        listing.title = (await title_el.inner_text()).strip()

    # Extract URL and ID
    link_el = await card.query_selector("a[href*='/rooms/']")
    if link_el:
        href = await link_el.get_attribute("href")
        if href:
            listing.url = (
                f"{AIRBNB_BASE_URL}{href}" if href.startswith("/") else href
            )
            # Extract listing ID from URL
            id_match = re.search(r"/rooms/(\d+)", href)
            if id_match:
                listing.id = id_match.group(1)

    # Extract price
    price_el = await card.query_selector(
        '[data-testid="price-availability-row"] span, '
        'span[class*="price"], '
        'span._1y74zjx'
    )
    if price_el:
        price_text = await price_el.inner_text()
        price_match = re.search(r"[\d,]+", price_text.replace(",", ""))
        if price_match:
            listing.price_per_night = float(price_match.group())

    # Extract rating
    rating_el = await card.query_selector(
        '[aria-label*="rating"], span[class*="rating"]'
    )
    if rating_el:
        rating_text = await rating_el.inner_text()
        rating_match = re.search(r"([\d.]+)", rating_text)
        if rating_match:
            listing.rating = float(rating_match.group(1))

    # Extract host name (from subtitle or badge)
    host_el = await card.query_selector(
        '[data-testid="listing-card-subtitle"] span, '
        'span[class*="host"]'
    )
    if host_el:
        text = await host_el.inner_text()
        # Host name sometimes appears as "Hosted by Name"
        host_match = re.search(r"(?:Hosted by|Host:)\s*(.+)", text)
        if host_match:
            listing.host_name = host_match.group(1).strip()

    # Check for Superhost badge
    superhost_el = await card.query_selector(
        '[aria-label*="Superhost"], [class*="superhost"]'
    )
    listing.superhost = superhost_el is not None

    # Extract photo URL
    img_el = await card.query_selector("img[src*='muscache']")
    if img_el:
        listing.photo_url = await img_el.get_attribute("src") or ""

    return listing


async def scrape_listings(
    location: str,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_listings: int = 20,
    headless: bool = True,
    *,
    date_mode: str = "flexible",
    flex_duration: int = 1,
    flex_duration_unit: str = "week",
) -> list[Listing]:
    """Scrape Airbnb search results for the given parameters.

    Uses Playwright to automate a browser, navigate to Airbnb search,
    and extract listing information from the results.

    Args:
        location: Search location
        checkin: Check-in date (YYYY-MM-DD) (fixed mode)
        checkout: Check-out date (YYYY-MM-DD) (fixed mode)
        guests: Number of guests
        min_price: Minimum price filter
        max_price: Maximum price filter
        max_listings: Maximum number of listings to collect
        headless: Run browser in headless mode
        date_mode: ``flexible`` or ``fixed``
        flex_duration: Trip length when using flexible mode
        flex_duration_unit: ``day``, ``week``, ``month``, or ``weekend``
        flex_trip_months_count: Months in ``flexible_trip_dates[]`` (structured flex)
        reference_date: Anchor month for flexible dates (tests)

    Returns:
        List of Listing objects found
    """
    url = build_search_url(
        location,
        checkin,
        checkout,
        guests,
        min_price,
        max_price,
        date_mode=date_mode,
        flex_duration=flex_duration,
        flex_duration_unit=flex_duration_unit,
        flex_trip_months_count=flex_trip_months_count,
        reference_date=reference_date,
    )
    logger.info("Searching Airbnb: %s", url)

    all_listings: list[Listing] = []

    async with async_playwright() as p:
        context, page, browser, uses_cdp = await open_airbnb_browser(
            p, headless=headless
        )

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Allow dynamic content to load
            await page.wait_for_timeout(3000)

            # Scrape current page
            page_listings = await _extract_listings_from_page(page, location)
            all_listings.extend(page_listings)
            logger.info(
                "Found %d listings on page 1 for %s",
                len(page_listings),
                location,
            )

            # Paginate if needed
            page_num = 2
            while len(all_listings) < max_listings:
                next_btn = await page.query_selector(
                    'a[aria-label="Next"], [data-testid="pagination-next"]'
                )
                if not next_btn:
                    break

                await next_btn.click()
                await page.wait_for_timeout(3000)

                page_listings = await _extract_listings_from_page(page, location)
                if not page_listings:
                    break

                all_listings.extend(page_listings)
                logger.info(
                    "Found %d listings on page %d (total: %d)",
                    len(page_listings),
                    page_num,
                    len(all_listings),
                )
                page_num += 1

        except Exception as e:
            logger.error("Error scraping Airbnb: %s", e)
        finally:
            await close_airbnb_session(context, browser, uses_cdp=uses_cdp)

    # Trim to max
    return all_listings[:max_listings]


def scrape_listings_sync(
    location: str,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_listings: int = 20,
    headless: bool = True,
    *,
    date_mode: str = "flexible",
    flex_duration: int = 1,
    flex_duration_unit: str = "week",
    flex_trip_months_count: Optional[int] = None,
    reference_date: Optional[date] = None,
) -> list[Listing]:
    """Synchronous wrapper for scrape_listings."""
    return asyncio.run(
        scrape_listings(
            location=location,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            min_price=min_price,
            max_price=max_price,
            max_listings=max_listings,
            headless=headless,
            date_mode=date_mode,
            flex_duration=flex_duration,
            flex_duration_unit=flex_duration_unit,
            flex_trip_months_count=flex_trip_months_count,
            reference_date=reference_date,
        )
    )
