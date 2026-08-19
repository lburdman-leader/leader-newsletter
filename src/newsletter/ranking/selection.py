"""Deterministic story selection.

Given the same scored articles and the same configuration, this module always
produces the same line-up in the same order (AC9). Nothing here consults a model,
a clock or a random source.

The rules, applied in order to articles sorted best-first:

1. drop excluded categories (``other`` by default);
2. drop anything below ``min_score``;
3. respect the per-category cap, so one topic cannot monopolise the edition;
4. respect the per-source cap, so one publication cannot either;
5. stop at ``max_items``.

Every rejection is recorded with its reason, so an empty or thin edition can be
explained from the run report instead of guessed at.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from newsletter.config import NewsletterSettings
from newsletter.logging_setup import get_logger
from newsletter.models import RankedArticle, RunManifest, TopicCategory
from newsletter.ranking.dedupe import collapse_duplicate_events
from newsletter.ranking.scoring import ranking_key

logger = get_logger("selection")

REASON_EXCLUDED_CATEGORY = "category_excluded"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_CATEGORY_LIMIT = "category_limit"
REASON_SOURCE_LIMIT = "source_limit"
REASON_MAX_ITEMS = "max_items"
REASON_DUPLICATE_EVENT = "duplicate_event"


@dataclass(frozen=True)
class RejectedArticle:
    """A scored article that did not make the edition, and why."""

    ranked: RankedArticle
    reason: str


@dataclass
class SelectionResult:
    """The chosen line-up plus a full account of what was left out."""

    selected: list[RankedArticle] = field(default_factory=list)
    rejected: list[RejectedArticle] = field(default_factory=list)
    above_threshold: int = 0

    @property
    def lead(self) -> RankedArticle | None:
        """The highest-ranked selected story. The editor may reword it, never replace it."""
        return self.selected[0] if self.selected else None

    @property
    def is_empty(self) -> bool:
        return not self.selected

    def sections(
        self, settings: NewsletterSettings
    ) -> list[tuple[TopicCategory, list[RankedArticle]]]:
        """Selected stories grouped by category, in configured publication order.

        The lead story is excluded: it is displayed on its own, and printing it
        twice would read as an editing mistake.
        """
        lead = self.lead
        grouped: list[tuple[TopicCategory, list[RankedArticle]]] = []
        for category in settings.ordered_categories():
            items = [
                ranked
                for ranked in self.selected
                if ranked.assessment.category is category and ranked is not lead
            ]
            if items:
                grouped.append((category, items))
        return grouped

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def select(
    ranked: Iterable[RankedArticle],
    settings: NewsletterSettings,
    *,
    manifest: RunManifest | None = None,
) -> SelectionResult:
    """Choose the edition line-up. Pure function of its inputs."""
    candidates: Sequence[RankedArticle] = sorted(ranked, key=ranking_key)
    result = SelectionResult()

    if settings.collapse_events:
        candidates, collapsed = collapse_duplicate_events(candidates)
        result.rejected.extend(
            RejectedArticle(ranked=item, reason=REASON_DUPLICATE_EVENT) for item in collapsed
        )

    excluded = set(settings.excluded_categories)
    per_category: dict[TopicCategory, int] = {}
    per_source: dict[str, int] = {}

    for article in candidates:
        category = article.assessment.category

        if category in excluded:
            result.rejected.append(RejectedArticle(article, REASON_EXCLUDED_CATEGORY))
            continue

        if article.final_score < settings.min_score:
            result.rejected.append(RejectedArticle(article, REASON_BELOW_THRESHOLD))
            continue

        result.above_threshold += 1

        if len(result.selected) >= settings.max_items:
            result.rejected.append(RejectedArticle(article, REASON_MAX_ITEMS))
            continue

        taken = per_category.get(category, 0)
        if taken >= settings.limit_for(category):
            result.rejected.append(RejectedArticle(article, REASON_CATEGORY_LIMIT))
            continue

        source_id = article.article.source_id
        from_source = per_source.get(source_id, 0)
        if settings.max_per_source is not None and from_source >= settings.max_per_source:
            result.rejected.append(RejectedArticle(article, REASON_SOURCE_LIMIT))
            continue

        per_category[category] = taken + 1
        per_source[source_id] = from_source + 1
        result.selected.append(article)

    if manifest is not None:
        manifest.articles_above_threshold = result.above_threshold
        manifest.articles_selected = len(result.selected)

    logger.info(
        "selection: %d selected of %d above threshold (%s)",
        len(result.selected),
        result.above_threshold,
        result.reasons() or "no rejections",
    )
    return result
