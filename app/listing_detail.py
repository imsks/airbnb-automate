"""Scrapes a listing's detail page for the public context the Scribe needs.

The search-results card has a title and a price. That is not enough to write a
message a host believes a human wrote. The detail page has the host's own words,
their bio, what guests actually said, and the signals that predict whether they
will entertain a collab at all.

Parsing is split from browsing so the fragile part — Airbnb's markup — can be
tested against fixtures without a browser.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.browser_session import close_airbnb_session, open_airbnb_browser
from app.config import get_airbnb_base_url

logger = logging.getLogger(__name__)

_MAX_DESCRIPTION_CHARS = 2500
_MAX_REVIEWS = 6
_MAX_REVIEW_CHARS = 400

_RESPONSE_RATE_RE = re.compile(r"response rate:?\s*(\d{1,3})\s*%", re.IGNORECASE)
_NEW_LISTING_RE = re.compile(r"\bnew\b", re.IGNORECASE)
_HOSTING_YEARS_RE = re.compile(
    r"(\d+)\s*(year|yr)s?\s*(?:of\s*)?hosting", re.IGNORECASE
)
_HOSTING_MONTHS_RE = re.compile(r"(\d+)\s*months?\s*(?:of\s*)?hosting", re.IGNORECASE)
_LONG_STAY_RE = re.compile(
    r"(weekly|monthly)\s+(discount|stay discount)|stay\s+\d+\+?\s*nights?\s+for",
    re.IGNORECASE,
)


def listing_url_for(listing_id: str, url: str = "") -> str:
    """Canonical detail-page URL for a listing."""
    if url:
        return url
    return f"{get_airbnb_base_url().rstrip('/')}/rooms/{listing_id}"


def parse_listing_age_months(text: str) -> Optional[int]:
    """Months a host has been listing, from their 'x years hosting' badge."""
    years = _HOSTING_YEARS_RE.search(text)
    if years:
        return int(years.group(1)) * 12
    months = _HOSTING_MONTHS_RE.search(text)
    if months:
        return int(months.group(1))
    return None


def parse_response_rate(text: str) -> str:
    """The host's stated response rate, as a bare percentage string."""
    match = _RESPONSE_RATE_RE.search(text)
    return f"{match.group(1)}%" if match else ""


def detect_long_stay_discount(text: str) -> bool:
    """Whether the listing advertises a weekly or monthly discount.

    A host who already discounts long stays has shown they would rather fill the
    calendar than hold the nightly rate — the best predictor of a yes.
    """
    return bool(_LONG_STAY_RE.search(text))


def _clean(text: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "")).strip()
    return collapsed[:limit]


async def _text_of(page, selectors: tuple[str, ...], limit: int) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            text = await locator.inner_text(timeout=5_000)
            if text and text.strip():
                return _clean(text, limit)
        except Exception:
            continue
    return ""


async def _all_texts(page, selectors: tuple[str, ...], limit: int, cap: int) -> list[str]:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count == 0:
                continue
            out: list[str] = []
            for index in range(min(count, cap)):
                text = await locator.nth(index).inner_text(timeout=3_000)
                cleaned = _clean(text, limit)
                if cleaned:
                    out.append(cleaned)
            if out:
                return out
        except Exception:
            continue
    return []


async def scrape_detail_on_page(page, url: str) -> dict:
    """Extract detail-page context using an already-open page."""
    logger.info("Enriching listing: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("load", timeout=30_000)
    except Exception:
        pass

    description = await _text_of(
        page,
        (
            '[data-section-id="DESCRIPTION_DEFAULT"]',
            '[data-testid="listing-description"]',
            'div[data-plugin-in-point-id="DESCRIPTION_DEFAULT"]',
        ),
        _MAX_DESCRIPTION_CHARS,
    )
    house_rules = await _text_of(
        page,
        (
            '[data-section-id="POLICIES_DEFAULT"]',
            '[data-testid="house-rules"]',
            'div[data-plugin-in-point-id="POLICIES_DEFAULT"]',
        ),
        1200,
    )
    host_bio = await _text_of(
        page,
        (
            '[data-section-id="HOST_PROFILE_DEFAULT"]',
            '[data-testid="host-profile"]',
            'div[data-plugin-in-point-id="HOST_PROFILE_DEFAULT"]',
        ),
        1200,
    )
    amenities = await _all_texts(
        page,
        (
            '[data-section-id="AMENITIES_DEFAULT"] li',
            '[data-testid="amenity-row"]',
            'div[data-plugin-in-point-id="AMENITIES_DEFAULT"] li',
        ),
        80,
        40,
    )
    reviews = await _all_texts(
        page,
        (
            '[data-section-id="REVIEWS_DEFAULT"] [data-review-id]',
            '[data-testid="review-card"]',
            'div[data-review-id]',
        ),
        _MAX_REVIEW_CHARS,
        _MAX_REVIEWS,
    )

    try:
        body_text = _clean(await page.inner_text("body", timeout=10_000), 40_000)
    except Exception:
        body_text = ""

    return {
        "description": description,
        "house_rules": house_rules,
        "amenities": amenities,
        "review_excerpts": reviews,
        "host_bio": host_bio,
        "host_response_rate": parse_response_rate(body_text or host_bio),
        "host_is_superhost": "superhost" in (host_bio + body_text[:5000]).lower(),
        "listing_age_months": parse_listing_age_months(host_bio or body_text),
        "has_long_stay_discount": detect_long_stay_discount(body_text),
        "instant_book": "instant book" in body_text.lower(),
    }


async def scrape_listing_detail(listing_id: str, url: str = "", headless: bool = True) -> dict:
    """Open a browser, enrich one listing, and close it again."""
    context, page, browser, uses_cdp = await open_airbnb_browser(headless=headless)
    try:
        return await scrape_detail_on_page(page, listing_url_for(listing_id, url))
    finally:
        await close_airbnb_session(context, browser, uses_cdp)
