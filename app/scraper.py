"""Airbnb listing scraper using Playwright browser automation."""

import logging
import re
import asyncio
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Page, Browser

from app.models import Listing

logger = logging.getLogger(__name__)

AIRBNB_BASE_URL = "https://www.airbnb.com"


def build_search_url(
    location: str,
    checkin: str,
    checkout: str,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
) -> str:
    """Build an Airbnb search URL from parameters.

    Args:
        location: Search location (e.g., "Goa, India")
        checkin: Check-in date in YYYY-MM-DD format
        checkout: Check-out date in YYYY-MM-DD format
        guests: Number of guests
        min_price: Minimum price per night (optional)
        max_price: Maximum price per night (optional)

    Returns:
        Fully constructed Airbnb search URL
    """
    params = [
        f"query={quote_plus(location)}",
        f"checkin={checkin}",
        f"checkout={checkout}",
        f"adults={guests}",
    ]

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
    checkin: str,
    checkout: str,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_listings: int = 20,
    headless: bool = True,
) -> list[Listing]:
    """Scrape Airbnb search results for the given parameters.

    Uses Playwright to automate a browser, navigate to Airbnb search,
    and extract listing information from the results.

    Args:
        location: Search location
        checkin: Check-in date (YYYY-MM-DD)
        checkout: Check-out date (YYYY-MM-DD)
        guests: Number of guests
        min_price: Minimum price filter
        max_price: Maximum price filter
        max_listings: Maximum number of listings to collect
        headless: Run browser in headless mode

    Returns:
        List of Listing objects found
    """
    url = build_search_url(location, checkin, checkout, guests, min_price, max_price)
    logger.info("Searching Airbnb: %s", url)

    all_listings: list[Listing] = []

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

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
            await browser.close()

    # Trim to max
    return all_listings[:max_listings]


def scrape_listings_sync(
    location: str,
    checkin: str,
    checkout: str,
    guests: int = 2,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_listings: int = 20,
    headless: bool = True,
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
        )
    )
