"""The Warden: deterministic review of every message before it reaches a host.

Prompts leak. A system instruction saying "never promise specific dates" is a
suggestion an LLM can talk itself out of, and with full autonomy there is no
human between the model and the host. So the guardrails live here, in code, and
run *after* generation on the final text.

The Warden never rewrites a draft. It allows it or blocks it, and a blocked
draft escalates its deal to ``NEEDS_HUMAN``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.config import get_follower_claim_ceiling
from app.deals import count_agent_replies
from app.models import Deal
from app.policy import GuardrailPolicy, load_policy, sending_enabled

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2000


class Rule:
    """Guardrail identifiers, used as the blocked_reason prefix."""

    KILL_SWITCH = "kill_switch"
    EMPTY = "empty_message"
    TOO_LONG = "too_long"
    CONTACT_INFO = "contact_info"
    OFF_PLATFORM = "off_platform"
    SPECIFIC_DATES = "specific_dates"
    PRICE_CEILING = "price_ceiling"
    DELIVERABLES = "deliverables"
    CREDENTIALS = "credentials"
    REPLY_CAP = "reply_cap"


@dataclass(frozen=True)
class Violation:
    """One broken guardrail."""

    rule: str
    detail: str
    excerpt: str = ""

    def __str__(self) -> str:
        suffix = f" — {self.excerpt!r}" if self.excerpt else ""
        return f"[{self.rule}] {self.detail}{suffix}"


@dataclass
class Verdict:
    """The Warden's decision on one draft."""

    allowed: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """Every violation, joined — what goes on the blocked message row."""
        return "; ".join(str(v) for v in self.violations)

    def __bool__(self) -> bool:
        return self.allowed


# --- Patterns --------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
# Indian mobiles, with or without country code and common separators.
_PHONE_RE = re.compile(
    r"(?:(?<!\d)(?:\+?91[\s.-]?)?[6-9]\d{2}[\s.-]?\d{3}[\s.-]?\d{4}(?!\d))"
    r"|(?:(?<!\d)\+\d{1,3}[\s.-]?\d[\d\s.-]{7,13}(?!\d))"
)
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")

_OFF_PLATFORM_PHRASES = (
    "whatsapp",
    "whats app",
    "telegram",
    "my number",
    "call me",
    "text me",
    "email me",
    "reach me at",
    "contact me directly",
    "outside airbnb",
    "off airbnb",
    "off the platform",
    "off platform",
    "book directly",
    "direct booking",
    "pay directly",
    "bank transfer",
    "upi",
    "paytm",
    "gpay",
    "google pay",
    "phonepe",
    "paypal",
    "venmo",
)

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_MONTH_DAY_RE = re.compile(
    rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.IGNORECASE
)
_DAY_MONTH_RE = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b", re.IGNORECASE
)

_AMOUNT_RE = re.compile(
    r"(?:(?P<sym>[₹$€£])\s?(?P<v1>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<code>rs\.?|inr|usd|eur|gbp)\s*(?P<v2>\d[\d,]*(?:\.\d+)?)"
    r"|(?P<v3>\d[\d,]*(?:\.\d+)?)\s*(?:rs\.?|inr|rupees|usd|dollars))",
    re.IGNORECASE,
)
#: An amount only matters if the sentence is committing to it. "your place at
#: ₹8000" is the host's price; "happy to pay ₹8000" is an agreement.
_COMMITMENT_WORDS = (
    "pay",
    "paying",
    "offer",
    "offering",
    "agree",
    "agreed",
    "happy to",
    "glad to",
    "can do",
    "could do",
    "will do",
    "would do",
    "budget",
    "afford",
    "settle",
    "deal at",
    "work with",
    "manage",
)
_COMMITMENT_WINDOW = 70

_FOLLOWER_RE = re.compile(
    r"\b(?P<num>\d[\d,.]*)\s*(?P<mult>[km])?\+?\s*"
    r"(?:followers|subscribers|audience|community|reach)",
    re.IGNORECASE,
)
_FOLLOWER_COUNT_RE = re.compile(r"(\d[\d,.]*)\s*([km])?", re.IGNORECASE)

_CONTENT_NOUNS = (
    "reels?",
    "photos?",
    "photographs?",
    "videos?",
    "posts?",
    "stories",
    "story",
    "reviews?",
    "blogs?",
    "articles?",
    "vlogs?",
    "shorts?",
)
_CONTENT_NOUN_RE = "|".join(_CONTENT_NOUNS)
_PROMISE_RE = re.compile(
    rf"\b(?P<qty>\d+)\s+(?:[\w-]+\s+){{0,3}}?(?P<noun>{_CONTENT_NOUN_RE})\b",
    re.IGNORECASE,
)
_UNBOUNDED_RE = re.compile(
    rf"\b(?:unlimited|as many|any number of|countless|endless)\s+"
    rf"(?:[\w-]+\s+){{0,3}}?(?:{_CONTENT_NOUN_RE})\b",
    re.IGNORECASE,
)
_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_WORD_PROMISE_RE = re.compile(
    rf"\b(?P<qty>{'|'.join(_WORD_NUMBERS)})\s+(?:[\w-]+\s+){{0,3}}?"
    rf"(?P<noun>{_CONTENT_NOUN_RE})\b",
    re.IGNORECASE,
)


def _singular(noun: str) -> str:
    noun = noun.lower()
    if noun == "stories":
        return "story"
    if noun.endswith("es") and noun[:-2].endswith(("ch", "sh", "s", "x")):
        return noun[:-2]
    return noun[:-1] if noun.endswith("s") else noun


def _excerpt(text: str, start: int, end: int, pad: int = 30) -> str:
    return text[max(0, start - pad) : min(len(text), end + pad)].strip()


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _scale(value: float, multiplier: str) -> float:
    multiplier = (multiplier or "").lower()
    if multiplier == "k":
        return value * 1_000
    if multiplier == "m":
        return value * 1_000_000
    return value


def _follower_ceiling(policy: GuardrailPolicy) -> int:
    """Largest follower count the fact sheet permits an agent to claim."""
    match = _FOLLOWER_COUNT_RE.search(policy.credential_facts.get("followers", ""))
    if not match:
        return get_follower_claim_ceiling()
    return int(_scale(_to_float(match.group(1)), match.group(2)))


# --- Rules -----------------------------------------------------------------


def _check_shape(body: str) -> list[Violation]:
    text = body.strip()
    if not text:
        return [Violation(Rule.EMPTY, "draft is empty")]
    if len(text) > MAX_MESSAGE_CHARS:
        return [
            Violation(
                Rule.TOO_LONG,
                f"{len(text)} chars exceeds the {MAX_MESSAGE_CHARS} char limit",
            )
        ]
    return []


def _check_contact_info(body: str, policy: GuardrailPolicy) -> list[Violation]:
    if policy.allow_off_platform_contact:
        return []

    violations: list[Violation] = []
    for match in _EMAIL_RE.finditer(body):
        violations.append(
            Violation(Rule.CONTACT_INFO, "contains an email address", match.group())
        )
    for match in _PHONE_RE.finditer(body):
        violations.append(
            Violation(Rule.CONTACT_INFO, "contains a phone number", match.group().strip())
        )

    allowed_handles = {
        h.strip().lower()
        for h in policy.credential_facts.get("handles", "").split(",")
        if h.strip()
    }
    # Emails are already reported; masking them stops the local part being
    # re-reported as a rogue social handle.
    without_emails = _EMAIL_RE.sub(lambda m: " " * len(m.group()), body)
    for match in _HANDLE_RE.finditer(without_emails):
        if match.group().lower() not in allowed_handles:
            violations.append(
                Violation(
                    Rule.CONTACT_INFO,
                    "names a social handle that is not on the fact sheet",
                    match.group(),
                )
            )

    lowered = body.lower()
    for phrase in _OFF_PLATFORM_PHRASES:
        index = lowered.find(phrase)
        if index != -1:
            violations.append(
                Violation(
                    Rule.OFF_PLATFORM,
                    f"suggests moving off-platform ({phrase!r})",
                    _excerpt(body, index, index + len(phrase)),
                )
            )
    return violations


def _check_specific_dates(body: str, policy: GuardrailPolicy) -> list[Violation]:
    """Agents commit to availability windows; only a human picks the dates."""
    if policy.allow_specific_dates:
        return []
    violations: list[Violation] = []
    for pattern, label in (
        (_ISO_DATE_RE, "an ISO date"),
        (_NUMERIC_DATE_RE, "a numeric date"),
        (_MONTH_DAY_RE, "a month-and-day date"),
        (_DAY_MONTH_RE, "a day-and-month date"),
    ):
        for match in pattern.finditer(body):
            violations.append(
                Violation(
                    Rule.SPECIFIC_DATES,
                    f"commits to {label}; only date ranges are allowed",
                    match.group().strip(),
                )
            )
    return violations


def _check_price(body: str, policy: GuardrailPolicy) -> list[Violation]:
    violations: list[Violation] = []
    lowered = body.lower()
    for match in _AMOUNT_RE.finditer(body):
        raw = match.group("v1") or match.group("v2") or match.group("v3")
        if raw is None:
            continue
        amount = _to_float(raw)

        window = lowered[
            max(0, match.start() - _COMMITMENT_WINDOW) : match.end() + _COMMITMENT_WINDOW
        ]
        if not any(word in window for word in _COMMITMENT_WORDS):
            continue

        if amount > policy.max_price_per_night:
            limit = (
                "only free stays are permitted"
                if policy.max_price_per_night <= 0
                else f"the ceiling is {policy.max_price_per_night:g} {policy.currency}"
            )
            violations.append(
                Violation(
                    Rule.PRICE_CEILING,
                    f"agrees to {amount:g} per night but {limit}",
                    _excerpt(body, match.start(), match.end()),
                )
            )
    return violations


def _deliverable_caps(policy: GuardrailPolicy) -> dict[str, Optional[int]]:
    """Map each permitted content noun to its maximum quantity (None = uncapped)."""
    caps: dict[str, Optional[int]] = {}
    for entry in policy.allowed_deliverables:
        qty_match = re.match(r"\s*(\d+)", entry)
        qty = int(qty_match.group(1)) if qty_match else None
        for noun_match in re.finditer(_CONTENT_NOUN_RE, entry, re.IGNORECASE):
            noun = _singular(noun_match.group())
            if noun not in caps:
                caps[noun] = qty
            elif qty is None or caps[noun] is None:
                caps[noun] = None
            else:
                caps[noun] = max(caps[noun], qty)
    return caps


def _check_deliverables(body: str, policy: GuardrailPolicy) -> list[Violation]:
    caps = _deliverable_caps(policy)
    violations: list[Violation] = []

    for match in _UNBOUNDED_RE.finditer(body):
        violations.append(
            Violation(
                Rule.DELIVERABLES,
                "promises an unbounded amount of content",
                match.group().strip(),
            )
        )

    promises: list[tuple[str, int, re.Match]] = []
    for match in _PROMISE_RE.finditer(body):
        promises.append((_singular(match.group("noun")), int(match.group("qty")), match))
    for match in _WORD_PROMISE_RE.finditer(body):
        qty = _WORD_NUMBERS[match.group("qty").lower()]
        promises.append((_singular(match.group("noun")), qty, match))

    for noun, qty, match in promises:
        excerpt = match.group().strip()
        if noun not in caps:
            violations.append(
                Violation(
                    Rule.DELIVERABLES,
                    f"promises {noun!r}, which is not an approved deliverable",
                    excerpt,
                )
            )
        elif caps[noun] is not None and qty > caps[noun]:
            violations.append(
                Violation(
                    Rule.DELIVERABLES,
                    f"promises {qty} {noun}(s) but the approved maximum is {caps[noun]}",
                    excerpt,
                )
            )
    return violations


def _check_credentials(body: str, policy: GuardrailPolicy) -> list[Violation]:
    ceiling = _follower_ceiling(policy)
    violations: list[Violation] = []
    for match in _FOLLOWER_RE.finditer(body):
        value = _scale(_to_float(match.group("num")), match.group("mult") or "")
        if ceiling and value > ceiling:
            violations.append(
                Violation(
                    Rule.CREDENTIALS,
                    f"claims {value:,.0f} followers; the fact sheet allows {ceiling:,}",
                    match.group().strip(),
                )
            )
    return violations


def _check_reply_cap(
    deal: Optional[Deal], policy: GuardrailPolicy, db_path: Optional[str]
) -> list[Violation]:
    if deal is None or deal.id is None:
        return []
    sent = count_agent_replies(deal.id, db_path)
    if sent >= policy.max_agent_replies_per_thread:
        return [
            Violation(
                Rule.REPLY_CAP,
                f"{sent} agent replies already sent on this thread; "
                f"the cap is {policy.max_agent_replies_per_thread}",
            )
        ]
    return []


# --- Entry point -----------------------------------------------------------


def review(
    body: str,
    *,
    deal: Optional[Deal] = None,
    policy: Optional[GuardrailPolicy] = None,
    check_kill_switch: bool = True,
    db_path: Optional[str] = None,
) -> Verdict:
    """Decide whether a draft may be sent.

    ``deal`` is optional so drafts can be reviewed before a deal exists, but
    the per-thread reply cap only applies when it is supplied.
    """
    active = policy or load_policy(db_path)

    violations: list[Violation] = []
    if check_kill_switch and not sending_enabled(db_path):
        violations.append(
            Violation(Rule.KILL_SWITCH, "sending is frozen by the kill switch")
        )

    shape = _check_shape(body)
    if shape:
        # A malformed draft makes every other rule noise.
        return Verdict(allowed=False, violations=violations + shape)

    violations.extend(_check_contact_info(body, active))
    violations.extend(_check_specific_dates(body, active))
    violations.extend(_check_price(body, active))
    violations.extend(_check_deliverables(body, active))
    violations.extend(_check_credentials(body, active))
    violations.extend(_check_reply_cap(deal, active, db_path))

    verdict = Verdict(allowed=not violations, violations=violations)
    if not verdict.allowed:
        logger.warning(
            "Warden blocked a draft for deal %s: %s",
            deal.id if deal else "-",
            verdict.reason,
        )
    return verdict
