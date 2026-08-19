"""The authoritative publication-date filter (AC6).

Discovery drops candidates whose date is already known to be outside the window,
but that is an optimisation. *This* is the gate: every article that reaches
analysis has a real publication date inside the configured window, decided by
Python against a half-open ``[start, end)`` interval -- never by a model.
"""

from __future__ import annotations

from collections.abc import Iterable

from newsletter.logging_setup import get_logger
from newsletter.models import DateWindow, NormalizedArticle

logger = get_logger("filtering")


def filter_by_window(
    articles: Iterable[NormalizedArticle], window: DateWindow
) -> tuple[list[NormalizedArticle], list[NormalizedArticle]]:
    """Split articles into ``(inside, outside)`` the window, order preserved."""
    inside: list[NormalizedArticle] = []
    outside: list[NormalizedArticle] = []

    for article in articles:
        (inside if window.contains(article.published_at) else outside).append(article)

    if outside:
        logger.info(
            "date filter: %d inside, %d outside %s..%s",
            len(inside),
            len(outside),
            window.start.isoformat(),
            window.end.isoformat(),
        )
    return inside, outside
