"""Publication-date parsing for ingestion.

One rule governs this module: **never invent a date.** Every function returns
``None`` when the input does not contain a real, parseable timestamp, and the
caller decides what to do with a dateless candidate. A fabricated date would
silently corrupt the deterministic time window, which is the one thing the
pipeline must get right.

A parsed timestamp with no timezone is normalized to UTC. That is an explicit,
documented assumption -- not an invention -- and it is recorded here so the
behaviour is testable rather than incidental.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

__all__ = ["ensure_aware", "from_struct_time", "parse_datetime"]


def ensure_aware(moment: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; pass through aware ones."""
    if moment is None:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment


def from_struct_time(value: time.struct_time | None) -> datetime | None:
    """Convert a feedparser ``*_parsed`` struct_time (always UTC) to a datetime."""
    if value is None:
        return None
    try:
        return datetime(*value[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 or RFC 2822 timestamp. Returns None when unparseable.

    Covers the shapes actually seen in ``<time datetime>`` attributes, JSON-LD
    ``datePublished`` and feed date fields. Python 3.11+ ``fromisoformat``
    already handles a trailing ``Z``, date-only values and sub-second precision
    beyond microseconds, so only two extra cases are handled here: trailing junk
    after a valid prefix (such as a timezone *name*), and RFC 2822.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    try:
        return ensure_aware(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    for length in (19, 10):  # "YYYY-MM-DDTHH:MM:SS" then "YYYY-MM-DD"
        try:
            return ensure_aware(datetime.fromisoformat(candidate[:length]))
        except ValueError:
            continue

    try:
        return ensure_aware(parsedate_to_datetime(candidate))
    except (TypeError, ValueError):
        return None
