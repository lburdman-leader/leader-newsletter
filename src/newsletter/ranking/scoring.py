"""The score. Computed here, in Python, and nowhere else (AC8).

The model rates four dimensions from 0 to 5. This module turns those ratings plus
the configured source priority into a single 0-100 number using fixed weights:

===============  =======  =========
dimension        weight   range
===============  =======  =========
topic_relevance  x6       0-30
business_impact  x5       0-25
novelty          x4       0-20
actionability    x3       0-15
source priority  x1       0-10
**total**                 **0-100**
===============  =======  =========

`ArticleAssessment` has no score field, so the model cannot express one even if it
wanted to. Changing the weights changes editorial policy, so they live here as
named constants rather than in configuration where they could drift silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from newsletter.logging_setup import get_logger
from newsletter.models import (
    ArticleAssessment,
    AssessmentRecord,
    NormalizedArticle,
    RankedArticle,
    SourceConfig,
)

logger = get_logger("scoring")

TOPIC_RELEVANCE_WEIGHT = 6
BUSINESS_IMPACT_WEIGHT = 5
NOVELTY_WEIGHT = 4
ACTIONABILITY_WEIGHT = 3

MIN_SCORE = 0
MAX_SCORE = 100


def compute_score(assessment: ArticleAssessment, source: SourceConfig) -> int:
    """The one and only score formula."""
    return (
        assessment.topic_relevance * TOPIC_RELEVANCE_WEIGHT
        + assessment.business_impact * BUSINESS_IMPACT_WEIGHT
        + assessment.novelty * NOVELTY_WEIGHT
        + assessment.actionability * ACTIONABILITY_WEIGHT
        + source.priority
    )


@dataclass(frozen=True)
class ScoreBreakdown:
    """Per-component contributions, for the manifest and for explaining a result."""

    topic_relevance: int
    business_impact: int
    novelty: int
    actionability: int
    source_priority: int

    @property
    def total(self) -> int:
        return (
            self.topic_relevance
            + self.business_impact
            + self.novelty
            + self.actionability
            + self.source_priority
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "topic_relevance": self.topic_relevance,
            "business_impact": self.business_impact,
            "novelty": self.novelty,
            "actionability": self.actionability,
            "source_priority": self.source_priority,
            "total": self.total,
        }


def score_components(assessment: ArticleAssessment, source_priority: int) -> ScoreBreakdown:
    """Itemised contributions. Always agrees with :func:`compute_score`."""
    return ScoreBreakdown(
        topic_relevance=assessment.topic_relevance * TOPIC_RELEVANCE_WEIGHT,
        business_impact=assessment.business_impact * BUSINESS_IMPACT_WEIGHT,
        novelty=assessment.novelty * NOVELTY_WEIGHT,
        actionability=assessment.actionability * ACTIONABILITY_WEIGHT,
        source_priority=source_priority,
    )


def score_breakdown(assessment: ArticleAssessment, source: SourceConfig) -> ScoreBreakdown:
    """The same arithmetic, itemised, for a configured source."""
    return score_components(assessment, source.priority)


def rank_article(
    article: NormalizedArticle,
    assessment: ArticleAssessment,
    source: SourceConfig,
) -> RankedArticle:
    """Attach the computed score to an assessed article."""
    return RankedArticle(
        article=article,
        assessment=assessment,
        source_name=source.name,
        source_priority=source.priority,
        final_score=compute_score(assessment, source),
    )


def rank_all(
    assessed: Iterable[tuple[NormalizedArticle, AssessmentRecord]],
    sources_by_id: Mapping[str, SourceConfig],
) -> list[RankedArticle]:
    """Score every assessed article, highest first.

    Ordering is fully determined: score descending, then earliest publication,
    then article id. No two runs over the same data can differ.
    """
    ranked = [
        rank_article(article, record.assessment, sources_by_id[article.source_id])
        for article, record in assessed
        if article.source_id in sources_by_id
    ]
    ranked.sort(key=ranking_key)
    if ranked:
        logger.info(
            "scored %d articles (max %d, min %d)",
            len(ranked),
            ranked[0].final_score,
            ranked[-1].final_score,
        )
    return ranked


def ranking_key(ranked: RankedArticle) -> tuple[int, str, str]:
    """Sort key: best first, with every tie broken by data, never by chance."""
    return (
        -ranked.final_score,
        ranked.article.published_at.isoformat(),
        ranked.article.article_id,
    )
