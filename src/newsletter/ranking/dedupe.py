"""Deterministic deduplication.

Runs *before* any model call, because the cheapest assessment is the one never
requested. Three passes, cheapest and most certain first:

1. canonical URL comparison key -- the same page reached two ways;
2. content hash -- identical text republished at a different URL (syndication);
3. normalized title -- the same story rewritten with the same headline.

Which copy survives is decided by rule, never by chance: highest source priority,
then earliest publication, then lowest article id. Two runs over the same inputs
always keep the same copy.

Semantic collapse of *different* stories about the same event needs the analyzer
event fingerprint, so :func:`collapse_duplicate_events` runs later in the pipeline,
after analysis and scoring (PRD section 22).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from newsletter.logging_setup import get_logger
from newsletter.models import NormalizedArticle, RankedArticle
from newsletter.normalization.urls import dedupe_key

logger = get_logger("dedupe")

_NON_ALPHANUMERIC = re.compile(r"[^\w\s]", re.UNICODE)

#: Below this length a normalized title is too generic to be evidence of a duplicate.
MIN_TITLE_KEY_LENGTH = 15

REASON_URL = "duplicate_url"
REASON_CONTENT = "duplicate_content"
REASON_TITLE = "duplicate_title"


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-free, whitespace-collapsed comparison key."""
    stripped = _NON_ALPHANUMERIC.sub(" ", title.lower())
    return " ".join(stripped.split())


@dataclass(frozen=True)
class DroppedArticle:
    """One discarded duplicate, with the reason and the copy that survived."""

    article: NormalizedArticle
    reason: str
    kept_article_id: str


@dataclass
class DedupeResult:
    kept: list[NormalizedArticle] = field(default_factory=list)
    dropped: list[DroppedArticle] = field(default_factory=list)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.dropped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def _preference_key(
    article: NormalizedArticle, priorities: Mapping[str, int]
) -> tuple[int, str, str]:
    """Best copy first: highest priority, then earliest published, then lowest id."""
    return (
        -priorities.get(article.source_id, 0),
        article.published_at.isoformat(),
        article.article_id,
    )


def deduplicate(
    articles: Iterable[NormalizedArticle],
    *,
    priorities: Mapping[str, int] | None = None,
) -> DedupeResult:
    """Collapse duplicates deterministically.

    ``priorities`` maps source id to its configured priority; a source missing
    from the mapping is treated as priority 0.
    """
    ranking = priorities or {}
    ordered = sorted(articles, key=lambda a: _preference_key(a, ranking))

    result = DedupeResult()
    by_url: dict[str, str] = {}
    by_content: dict[str, str] = {}
    by_title: dict[str, str] = {}

    for article in ordered:
        url_key = dedupe_key(article.canonical_url)
        title_key = normalize_title(article.title)

        winner = by_url.get(url_key)
        reason = REASON_URL
        if winner is None:
            winner = by_content.get(article.content_hash)
            reason = REASON_CONTENT
        if winner is None and len(title_key) >= MIN_TITLE_KEY_LENGTH:
            winner = by_title.get(title_key)
            reason = REASON_TITLE

        if winner is not None:
            result.dropped.append(
                DroppedArticle(article=article, reason=reason, kept_article_id=winner)
            )
            continue

        by_url[url_key] = article.article_id
        by_content[article.content_hash] = article.article_id
        if len(title_key) >= MIN_TITLE_KEY_LENGTH:
            by_title[title_key] = article.article_id
        result.kept.append(article)

    if result.dropped:
        logger.info(
            "deduplication: kept %d, dropped %d %s",
            len(result.kept),
            result.dropped_count,
            result.reasons(),
        )
    return result


# --------------------------------------------------------------------------- #
# semantic collapse — after analysis, using the structured event fingerprint
# --------------------------------------------------------------------------- #


def collapse_duplicate_events(
    ranked: Iterable[RankedArticle],
) -> tuple[list[RankedArticle], list[RankedArticle]]:
    """Collapse different articles that describe the same event.

    Two outlets covering one announcement share a subject, action and object even
    when their wording, URLs and content hashes differ, so the deterministic
    passes above cannot see the duplication. The analyzer supplies that structured
    fingerprint; the choice of which copy survives is still made here, by rule:
    highest score, then earliest publication, then lowest article id.

    An article whose fingerprint is incomplete is always kept -- an unknown event
    is not evidence of a duplicate. Returns ``(kept, collapsed)``.
    """
    from newsletter.ranking.scoring import ranking_key

    ordered = sorted(ranked, key=ranking_key)
    winners: dict[str, RankedArticle] = {}
    kept: list[RankedArticle] = []
    collapsed: list[RankedArticle] = []

    for article in ordered:
        fingerprint = article.assessment.event_fingerprint()
        if fingerprint is None:
            kept.append(article)
            continue
        if fingerprint in winners:
            collapsed.append(article)
            continue
        winners[fingerprint] = article
        kept.append(article)

    if collapsed:
        logger.info("event collapse: kept %d, dropped %d", len(kept), len(collapsed))
    return kept, collapsed
