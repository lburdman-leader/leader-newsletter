"""Deterministic story selection.

Given the same scored articles and the same configuration, this module always
produces the same line-up in the same order (AC9). Nothing here consults a model,
a clock or a random source.

The rules, applied in order to articles sorted best-first:

1. drop anything a previous edition already printed;
2. collapse several articles covering one event into the best of them, first on
   the analyzer fingerprint and then -- for publishable candidates only -- on the
   article text;
3. drop excluded categories (``other`` by default);
4. drop anything below ``min_score``;
5. respect the per-category cap, so one topic cannot monopolise the edition;
6. respect the per-source cap, so one publication cannot either;
7. respect the per-subject cap, so one company cannot either;
8. stop at ``max_items``.

Every rejection is recorded with its reason, and the reasons that need one carry
a free-text detail as well. Those same reasons also reach the run manifest, so an
empty or thin edition can be explained from the artifact instead of guessed at.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from newsletter.config import NewsletterSettings
from newsletter.logging_setup import get_logger
from newsletter.models import RankedArticle, RunManifest, TopicCategory
from newsletter.ranking.dedupe import (
    PublishedKeys,
    collapse_duplicate_events,
    collapse_similar_events,
    normalize_entity,
)
from newsletter.ranking.scoring import ranking_key

logger = get_logger("selection")

REASON_EXCLUDED_CATEGORY = "category_excluded"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_CATEGORY_LIMIT = "category_limit"
REASON_SOURCE_LIMIT = "source_limit"
REASON_SUBJECT_LIMIT = "subject_limit"
REASON_MAX_ITEMS = "max_items"
REASON_DUPLICATE_EVENT = "duplicate_event"
REASON_SIMILAR_EVENT = "similar_event"
REASON_ALREADY_PUBLISHED = "already_published"
#: Not applied by :func:`select`. The entity-fidelity guard runs after selection
#: and records its drops here, so ``reasons()`` still explains a thin edition.
REASON_UNSUPPORTED_ENTITY = "unsupported_entity"

#: Rejections that withhold one identified story for one specific circumstance,
#: and therefore belong in the run manifest rather than only in a log line. The
#: rest are policy arithmetic anyone can re-derive from the counts and the config:
#: a category was excluded, a score was too low, a cap or ``max_items`` was full.
MANIFEST_REASONS = (REASON_ALREADY_PUBLISHED, REASON_SIMILAR_EVENT, REASON_SUBJECT_LIMIT)


@dataclass(frozen=True)
class RejectedArticle:
    """A scored article that did not make the edition, and why.

    ``detail`` names the specific circumstance when the reason alone is not
    enough to act on -- which issue already printed the story, which subject hit
    its cap. A suppressed story that cannot explain itself is a silent failure.
    """

    ranked: RankedArticle
    reason: str
    detail: str | None = None


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

    def rejections_for(self, reason: str) -> list[RejectedArticle]:
        """Every rejection carrying ``reason``, in selection order."""
        return [item for item in self.rejected if item.reason == reason]


def select(
    ranked: Iterable[RankedArticle],
    settings: NewsletterSettings,
    *,
    manifest: RunManifest | None = None,
    published: PublishedKeys | None = None,
) -> SelectionResult:
    """Choose the edition line-up. Pure function of its inputs.

    ``published`` carries the identity keys of stories previous editions already
    printed. It is passed in rather than read here: selection stays a pure
    function of its arguments, so the same inputs always produce the same
    line-up (AC9), and the pipeline owns the database.
    """
    candidates: Sequence[RankedArticle] = sorted(ranked, key=ranking_key)
    result = SelectionResult()
    already_published = published or PublishedKeys()

    # Before the collapse, not after it. A copy that ran in an earlier edition
    # would otherwise win its event -- it usually scores highest, which is why it
    # was published -- and take a live follow-up down with it.
    if already_published:
        surviving: list[RankedArticle] = []
        for article in candidates:
            issue = already_published.issue_for(article.article)
            if issue is None:
                surviving.append(article)
                continue
            result.rejected.append(
                RejectedArticle(article, REASON_ALREADY_PUBLISHED, f"already published in {issue}")
            )
        candidates = surviving

    if settings.collapse_events:
        candidates, collapsed = collapse_duplicate_events(candidates)
        result.rejected.extend(
            RejectedArticle(ranked=item, reason=REASON_DUPLICATE_EVENT) for item in collapsed
        )

    # Second, and only after the exact keys have had their chance: the reports of
    # one event whose analyzer keys disagree, caught on the text they share. It is
    # given ``min_score`` because it folds only what could be published -- a
    # candidate that rule 4 below will reject anyway gains nothing from being
    # collapsed, and is where every measured false positive lives. The whole
    # candidate list still goes in, because the pass measures its term statistics
    # over the run's full corpus.
    if settings.collapse_similar_events:
        candidates, folded = collapse_similar_events(
            candidates,
            threshold=settings.similar_event_threshold,
            min_score=settings.min_score,
        )
        result.rejected.extend(
            RejectedArticle(
                ranked=item,
                reason=REASON_SIMILAR_EVENT,
                detail=f"same event as {survivor.article.title!r}",
            )
            for item, survivor in folded
        )

    excluded = set(settings.excluded_categories)
    per_category: dict[TopicCategory, int] = {}
    per_source: dict[str, int] = {}
    per_subject: dict[str, int] = {}

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

        # One company may hold several genuinely distinct stories in a week. The
        # cap keeps the edition heterogeneous anyway. An article whose analyst
        # named no subject is uncapped: an unknown subject is not evidence of
        # dominance.
        subject = normalize_entity(article.assessment.event_subject or "")
        about_subject = per_subject.get(subject, 0)
        if (
            subject
            and settings.max_per_subject is not None
            and about_subject >= settings.max_per_subject
        ):
            result.rejected.append(
                RejectedArticle(
                    article,
                    REASON_SUBJECT_LIMIT,
                    f"{settings.max_per_subject} stories already cover {subject!r}",
                )
            )
            continue

        per_category[category] = taken + 1
        per_source[source_id] = from_source + 1
        if subject:
            per_subject[subject] = about_subject + 1
        result.selected.append(article)

    if manifest is not None:
        manifest.articles_above_threshold = result.above_threshold
        manifest.articles_selected = len(result.selected)
        # Rule 7: nothing is dropped silently, and the console is not an audit
        # surface. A withheld story is not a failure, so it is recorded as an
        # omission rather than an error, which would mark a healthy run as failed.
        for item in result.rejected:
            if item.reason in MANIFEST_REASONS:
                manifest.record_withheld(
                    article_id=item.ranked.article.article_id,
                    url=item.ranked.article.canonical_url,
                    title=item.ranked.article.title,
                    reason=item.reason,
                    detail=item.detail,
                )

    for item in result.rejected:
        if item.detail is not None:
            logger.info(
                "rejected %s (%s): %s", item.ranked.article.article_id, item.reason, item.detail
            )

    logger.info(
        "selection: %d selected of %d above threshold (%s)",
        len(result.selected),
        result.above_threshold,
        result.reasons() or "no rejections",
    )
    return result
