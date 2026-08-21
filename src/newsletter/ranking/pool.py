"""The order candidates are assessed in, and how many of them are.

Assessment is the expensive stage: one model call per candidate, and a real week
produces well over a hundred candidates for an edition of ten. Bounding that pool
turns exhaustive rating into sampling, so the *order* decides what the sample
contains -- and that makes this module editorial policy, not an optimisation.

**Round-robin across sources, newest first within each.** The obvious ordering --
source priority descending -- is the wrong one: the priority-7 sources alone can
fill any plausible budget, so Cartoon Brew, Tubefilter and Unite.AI would never be
read at all and the heterogeneity the edition is built on (ADR-0030/0032/0040)
would be undone silently, by a budget rather than by a decision. Round-robin makes
the cap trim *depth* -- the eleventh story from one outlet -- instead of removing
whole beats. Within one round the sources are visited by priority, so a tie for the
last slot still favours the more trusted publication.

Every tie is broken by data (``article_id``), never by dictionary or set order, so
the same pool always yields the same order and the same batches (AC9).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from newsletter.models import NormalizedArticle


def _newest_first(queue: list[NormalizedArticle]) -> list[NormalizedArticle]:
    """Most recent first, ties broken by ``article_id`` ascending.

    Two passes rather than one composite key: the two components sort in opposite
    directions, and Python's sort is stable, so ordering by id and then by date
    gives the exact answer without inventing a reversible date key.
    """
    return sorted(
        sorted(queue, key=lambda a: a.article_id), key=lambda a: a.published_at, reverse=True
    )


def round_robin(
    articles: Iterable[NormalizedArticle],
    *,
    priorities: Mapping[str, int],
) -> list[NormalizedArticle]:
    """One article per source, then the next from each, until all are placed.

    ``priorities`` orders the sources within a round; a source the mapping does
    not name is treated as priority 0 rather than dropped, because an article
    that reached this point has already been normalized and belongs in the pool.
    """
    grouped: dict[str, list[NormalizedArticle]] = {}
    for article in articles:
        grouped.setdefault(article.source_id, []).append(article)
    grouped = {source_id: _newest_first(queue) for source_id, queue in grouped.items()}

    sources = sorted(grouped, key=lambda source_id: (-priorities.get(source_id, 0), source_id))
    ordered: list[NormalizedArticle] = []
    for depth in range(max((len(q) for q in grouped.values()), default=0)):
        for source_id in sources:
            queue = grouped[source_id]
            if depth < len(queue):
                ordered.append(queue[depth])
    return ordered


def split_reserved(
    articles: Sequence[NormalizedArticle], *, reserved_source_id: str | None
) -> tuple[list[NormalizedArticle], list[NormalizedArticle]]:
    """Separate the articles that hold a slot by right from the rest.

    Reader submissions are assessed first and outside the budget entirely: a link
    a reader was promised a slot for must never be crowded out by a cost ceiling,
    and there are at most ``submissions.max_per_run`` of them anyway.
    """
    if reserved_source_id is None:
        return [], list(articles)
    reserved = [a for a in articles if a.source_id == reserved_source_id]
    rest = [a for a in articles if a.source_id != reserved_source_id]
    return reserved, rest


def batches(total: int, *, size: int, cap: int | None) -> list[tuple[int, int]]:
    """The half-open slices the pool is assessed in, largest index last.

    A pure function of three numbers, so the batch boundaries of a run can be
    reasoned about -- and tested -- without assessing anything.
    """
    budget = total if cap is None else min(total, cap)
    return [(start, min(start + size, budget)) for start in range(0, budget, max(size, 1))]
