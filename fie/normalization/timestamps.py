"""Timestamp normalization: everything becomes timezone-aware UTC."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Philippine sources publish in PHT (UTC+8) unless stated otherwise.
PHT = timezone(timedelta(hours=8))

_ABSOLUTE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%m/%d/%Y",
)

_RELATIVE_RE = re.compile(
    r"(\d+)\s*(minute|min|hour|hr|day|week)s?\s+ago", re.IGNORECASE
)

_DATE_MENTION_RE = re.compile(
    r"(?:as of|effective|updated:?|posted:?)\s+"
    r"((?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|"
    r"nov|dec)\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=PHT).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp(raw: str, now: datetime | None = None) -> datetime | None:
    """Parse an absolute or relative ("2 hours ago") timestamp string."""
    text = raw.strip()
    if not text:
        return None

    match = _RELATIVE_RE.search(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {
            "minute": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "hr": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }[unit]
        base = now or datetime.now(timezone.utc)
        return base - delta

    try:
        return to_utc(datetime.fromisoformat(text))
    except ValueError:
        pass
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return to_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def find_date_mention(text: str) -> datetime | None:
    """Find an "as of <date>" / "effective <date>" mention in page text."""
    match = _DATE_MENTION_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).replace(".", "").replace(",", ", ")
    candidate = re.sub(r"\s+", " ", candidate).replace(" ,", ",").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return to_utc(datetime.strptime(candidate, fmt))
        except ValueError:
            continue
    return None
