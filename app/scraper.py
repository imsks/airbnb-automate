"""Airbnb listing scraper using Playwright browser automation."""

import logging
import re
import asyncio
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Page

from app.browser_session import close_airbnb_session, open_airbnb_browser
from app.models import Listing

logger = logging.getLogger(__name__)

AIRBNB_BASE_URL = "https://www.airbnb.com"

# Airbnb flexible trip presets (URL enum values)
_FLEX_PRESET_WEEK = "one_week"
_FLEX_PRESET_MONTH = "one_month"


def normalize_flex_duration_unit(unit: str) -> str:
    """Normalize day(s) / week(s) / month(s) to day | week | month."""
    u = (unit or "week").strip().lower().rstrip("s")
    if u in ("day", "week", "month"):
        return u
    raise ValueError(f"Invalid flex duration unit: {unit!r}")


def flexible_trip_nights(duration: int, unit: str) -> int:
    """Convert flexible duration to a night count for Airbnb's price filter.

    Day unit is interpreted as *nights* (e.g. duration=3 → 3 nights).
    Month uses 28 nights per month to match Airbnb's flexible month bucket.
    """
    u = normalize_flex_duration_unit(unit)
    d = max(1, int(duration))
    if u == "day":
        return d
    if u == "week":
        return d * 7
    return d * 28


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
) -> str:
    """Build an Airbnb search URL from parameters.

    Args:
        location: Search location (e.g., "Goa, India")
        checkin: Check-in date in YYYY-MM-DD format (fixed mode)
        checkout: Check-out date in YYYY-MM-DD format (fixed mode)
        guests: Number of guests
        min_price: Minimum price per night (optional)
        max_price: Maximum price per night (optional)
        date_mode: ``flexible`` (trip length) or ``fixed`` (calendar dates)
        flex_duration: Length when ``date_mode`` is ``flexible`` (default 1)
        flex_duration_unit: ``day``, ``week``, or ``month`` (nights / 7-night blocks / 28-night blocks)

    Returns:
        Fully constructed Airbnb search URL
    """
    params = [
        f"query={quote_plus(location)}",
        f"adults={guests}",
    ]

    mode = (date_mode or "flexible").strip().lower()
    if mode == "fixed":
        if checkin:
            params.append(f"checkin={checkin}")
        if checkout:
            params.append(f"checkout={checkout}")
    else:
        # Flexible dates: duration-based search (no calendar check-in/out in URL)
        nights = flexible_trip_nights(flex_duration, flex_duration_unit)
        params.append("date_picker_type=flexible_dates")
        params.append(f"price_filter_num_nights={nights}")
        u = normalize_flex_duration_unit(flex_duration_unit)
        d = max(1, int(flex_duration))
        if d == 1 and u == "week":
            params.append(f"flexible_trip_lengths[]={_FLEX_PRESET_WEEK}")
        elif d == 1 and u == "month":
            params.append(f"flexible_trip_lengths[]={_FLEX_PRESET_MONTH}")

    if min_price is not None:
        params.append(f"price_min={int(min_price)}")
    if max_price is not None:
        params.append(f"price_max={int(max_price)}")

    return f"{AIRBNB_BASE_URL}/s/{quote_plus(location)}/homes?{'&'.join(params)}"


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
        flex_duration_unit: ``day``, ``week``, or ``month``

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
        )
    )
