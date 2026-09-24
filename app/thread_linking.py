"""Connecting a message we sent to the inbox thread it became.

v1 never recorded this edge, so a reply from a host could not be traced back to
the listing it was about. Two mechanisms, in order of trust:

1. ``capture_thread_reference`` reads the thread id straight off the page right
   after a send. Exact, and the only one used for new outreach.
2. ``match_thread_to_deals`` scores a scraped inbox thread against unlinked
   deals on host name and listing title. Needed only for history that predates
   mechanism 1, and deliberately conservative — an ambiguous match returns
   nothing rather than guessing.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Optional, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit

from app.models import Deal

logger = logging.getLogger(__name__)

_THREAD_URL_RE = re.compile(
    r"^/(?:hosting/|guest/)?(?:(?:messages|messaging)/(?:thread/)?|inbox/)(\d+)(?:/|$)"
)
#: Inbox URLs now look like ``/hosting/inbox/folder/all/thread/123``.
_THREAD_SEGMENT_RE = re.compile(r"/thread/(\d+)(?:/|$)")

#: Below this, a fuzzy match is treated as no match at all.
MATCH_THRESHOLD = 0.72
#: A match must beat the runner-up by this much, or the thread is ambiguous.
MATCH_MARGIN = 0.10


def extract_thread_id(url: str) -> Optional[str]:
    """Pull the thread id out of an Airbnb messaging URL."""
    if not url:
        return None
    parsed = urlsplit(url)
    match = _THREAD_URL_RE.match(parsed.path) or _THREAD_SEGMENT_RE.search(parsed.path)
    if match:
        return match.group(1)
    thread_id = parse_qs(parsed.query).get("thread_id", [""])[0]
    if thread_id.isdigit():
        return thread_id
    return None


async def capture_thread_reference(page) -> tuple[Optional[str], str]:
    """Read the thread the browser landed on after sending. ``(id, url)``.

    Returns ``(None, "")`` when the send did not navigate to a thread, which is
    normal for the listing-page composer; the fuzzy matcher covers that case on
    the next inbox sync.
    """
    try:
        current = page.url or ""
    except Exception:
        return None, ""

    thread_id = extract_thread_id(current)
    if thread_id:
        return thread_id, current

    try:
        links = page.get_by_role(
            "link", name=re.compile(r"^(?:View (?:conversation|message|thread)|Go to (?:conversation|messages))$", re.I)
        )
        if await links.count() != 1 or not await links.first.is_visible():
            return None, ""
        href = urljoin(current, await links.first.get_attribute("href") or "")
        if urlsplit(href).netloc != urlsplit(current).netloc:
            return None, ""
    except Exception:
        return None, ""

    thread_id = extract_thread_id(href or "")
    return (thread_id, href) if thread_id else (None, "")


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower()).strip()


def _similarity(left: str, right: str) -> float:
    left, right = _normalise(left), _normalise(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def score_match(deal: Deal, host_name: str, listing_title: str) -> float:
    """How strongly a scraped thread looks like it belongs to ``deal``.

    Host name dominates: Airbnb truncates listing titles in the inbox, but the
    host name is shown in full.
    """
    host_score = _similarity(deal.host_name, host_name)
    title_score = _similarity(deal.place_name, listing_title)
    if not deal.place_name or not listing_title:
        return host_score
    return 0.65 * host_score + 0.35 * title_score


def match_thread_to_deals(
    host_name: str,
    listing_title: str,
    candidates: Sequence[Deal],
) -> Optional[Deal]:
    """Best unlinked deal for a scraped thread, or ``None`` when unsure.

    Refusing an ambiguous match is deliberate: mislinking a thread would attach
    a host's reply to the wrong property and poison the negotiation context.
    """
    unlinked = [d for d in candidates if not d.is_linked]
    if not unlinked:
        return None

    scored = sorted(
        ((score_match(d, host_name, listing_title), d) for d in unlinked),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best_deal = scored[0]
    if best_score < MATCH_THRESHOLD:
        logger.debug(
            "No deal matches thread for host %r (best %.2f)", host_name, best_score
        )
        return None

    if len(scored) > 1 and best_score - scored[1][0] < MATCH_MARGIN:
        logger.warning(
            "Ambiguous thread match for host %r: %.2f vs %.2f — leaving unlinked",
            host_name,
            best_score,
            scored[1][0],
        )
        return None

    return best_deal
