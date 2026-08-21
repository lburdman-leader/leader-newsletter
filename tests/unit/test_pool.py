"""The candidate pool: what it contains, and in what order a bounded run reads it.

The cap turns exhaustive rating into sampling, so the order is editorial policy.
These tests pin the properties that make the pool defensible: no source is starved
by a budget, an article the engine already ingested rejoins the pool rather than
being lost with the feed that carried it, and the same inputs always produce the
same order (AC9).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from newsletter.models import NormalizedArticle
from newsletter.ranking.pool import merge_stored, round_robin, split_reserved

BASE = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

PRIORITIES = {"official": 7, "trade": 6, "wire": 5}


def make_article(article_id: str, source_id: str, *, age_hours: int = 0) -> NormalizedArticle:
    return NormalizedArticle(
        article_id=article_id,
        source_id=source_id,
        canonical_url=f"https://{source_id}.example/{article_id}",
        title=f"Story {article_id}",
        published_at=BASE - timedelta(hours=age_hours),
        clean_text="A long enough article body for the pool tests to be realistic.",
        content_hash=f"contenthash-{article_id}",
        retrieved_at=BASE,
    )


def ids(articles: list[NormalizedArticle]) -> list[str]:
    return [article.article_id for article in articles]


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #


def test_a_prolific_source_cannot_starve_a_quiet_one() -> None:
    """The point of round-robin: a cap trims depth, never a whole beat.

    Ordered by source priority, the twenty stories from the priority-7 source
    would fill any plausible budget and the trade press would never be read.
    """
    pool = [make_article(f"official-{n:02d}", "official", age_hours=n) for n in range(20)]
    pool += [make_article("trade-0", "trade"), make_article("wire-0", "wire")]

    ordered = round_robin(pool, priorities=PRIORITIES)

    assert ids(ordered)[:3] == ["official-00", "trade-0", "wire-0"]
    assert ids(ordered)[3:] == [f"official-{n:02d}" for n in range(1, 20)]


def test_within_a_source_the_newest_story_is_read_first() -> None:
    pool = [
        make_article("old", "wire", age_hours=48),
        make_article("newest", "wire", age_hours=1),
        make_article("middle", "wire", age_hours=24),
    ]
    assert ids(round_robin(pool, priorities=PRIORITIES)) == ["newest", "middle", "old"]


def test_ties_are_broken_by_data_and_never_by_input_order() -> None:
    """AC9: same pool, same order, whichever way the pool arrives."""
    pool = [
        make_article("b", "trade"),
        make_article("a", "trade"),
        make_article("c", "official"),
    ]
    forward = ids(round_robin(pool, priorities=PRIORITIES))
    assert forward == ids(round_robin(list(reversed(pool)), priorities=PRIORITIES))
    # Equal dates within one source fall back to the id; the round itself is
    # ordered by source priority.
    assert forward == ["c", "a", "b"]


def test_a_source_missing_from_the_priority_map_is_ordered_last_not_dropped() -> None:
    """An article that reached the pool was normalized; it belongs in the order."""
    pool = [make_article("unknown-0", "unknown"), make_article("official-0", "official")]
    assert ids(round_robin(pool, priorities=PRIORITIES)) == ["official-0", "unknown-0"]


# --------------------------------------------------------------------------- #
# the budget
# --------------------------------------------------------------------------- #


def test_submissions_are_separated_from_the_budgeted_pool() -> None:
    pool = [
        make_article("submitted", "reader-submissions"),
        make_article("wire-0", "wire"),
    ]
    reserved, rest = split_reserved(pool, reserved_source_id="reader-submissions")
    assert ids(reserved) == ["submitted"] and ids(rest) == ["wire-0"]


def test_with_no_reserved_source_everything_is_budgeted() -> None:
    pool = [make_article("wire-0", "wire")]
    reserved, rest = split_reserved(pool, reserved_source_id=None)
    assert reserved == [] and rest == pool


# --------------------------------------------------------------------------- #
# recall — the pool is not only what today's feeds still carry
# --------------------------------------------------------------------------- #


def test_a_stored_in_window_article_rejoins_the_pool() -> None:
    """The whole point: a feed that has rolled past a story does not lose it."""
    fresh = [make_article("today", "wire")]
    stored = [make_article("last-week", "trade", age_hours=100)]

    assert ids(merge_stored(fresh, stored)) == ["today", "last-week"]


def test_a_freshly_fetched_copy_wins_over_the_stored_one() -> None:
    """``article_id`` is the canonical URL, so the two are the same page.

    The fresh copy carries the current text and is what ``save_articles`` is about
    to overwrite the stored row with; preferring the stored one would republish
    text the source has since changed.
    """
    fresh = make_article("same", "wire")
    stale = fresh.model_copy(update={"title": "An older headline"})

    merged = merge_stored([fresh], [stale])

    assert merged == [fresh]


def test_recall_order_is_data_driven_and_survives_a_reversed_input() -> None:
    """AC9 reaches the merge: set iteration never decides what the pool looks like."""
    fresh = [make_article("fresh", "wire")]
    stored = [
        make_article("older", "trade", age_hours=50),
        make_article("newer", "trade", age_hours=10),
    ]

    assert ids(merge_stored(fresh, stored)) == ["fresh", "older", "newer"]
    assert ids(merge_stored(fresh, list(reversed(stored)))) == ["fresh", "older", "newer"]
