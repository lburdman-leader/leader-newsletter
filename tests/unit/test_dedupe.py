"""Deterministic deduplication before any model call."""

from __future__ import annotations

from datetime import UTC, datetime

from newsletter.models import NormalizedArticle
from newsletter.normalization.article import compute_article_id
from newsletter.ranking.dedupe import (
    REASON_CONTENT,
    REASON_TITLE,
    REASON_URL,
    deduplicate,
    normalize_title,
)

EARLY = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
LATE = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
RETRIEVED = datetime(2026, 8, 18, tzinfo=UTC)

PRIORITIES = {"wire": 9, "blog": 5, "aggregator": 2}


def make_article(
    url: str,
    *,
    source_id: str = "wire",
    title: str = "Example Labs ships a reasoning model",
    text: str = "A long enough article body about the launch of a new model.",
    published_at: datetime = EARLY,
) -> NormalizedArticle:
    return NormalizedArticle(
        article_id=compute_article_id(url),
        source_id=source_id,
        canonical_url=url,
        title=title,
        published_at=published_at,
        clean_text=text,
        content_hash=f"hash-{hash(text) & 0xFFFFFFFF:08x}",
        retrieved_at=RETRIEVED,
    )


# --------------------------------------------------------------------------- #
# title normalization
# --------------------------------------------------------------------------- #


def test_title_key_ignores_case_punctuation_and_spacing() -> None:
    assert normalize_title("Example Labs: Ships a Model!") == normalize_title(
        "example labs   ships a model"
    )


def test_different_titles_keep_different_keys() -> None:
    assert normalize_title("Model A ships") != normalize_title("Model B ships")


# --------------------------------------------------------------------------- #
# the three passes
# --------------------------------------------------------------------------- #


def test_same_page_reached_two_ways_collapses() -> None:
    canonical = make_article("https://wire.example/story")
    tracked = make_article("https://www.wire.example/story/?utm_source=rss")

    result = deduplicate([canonical, tracked], priorities=PRIORITIES)

    assert len(result.kept) == 1
    assert result.dropped[0].reason == REASON_URL
    assert result.dropped[0].kept_article_id == result.kept[0].article_id


def test_syndicated_copy_with_identical_text_collapses() -> None:
    original = make_article("https://wire.example/story", source_id="wire")
    syndicated = make_article(
        "https://aggregator.example/reprint", source_id="aggregator", title="A different headline"
    )

    result = deduplicate([original, syndicated], priorities=PRIORITIES)

    assert len(result.kept) == 1
    assert result.kept[0].source_id == "wire"
    assert result.dropped[0].reason == REASON_CONTENT


def test_same_headline_rewritten_collapses() -> None:
    first = make_article("https://wire.example/story", text="One version of the article body here.")
    second = make_article(
        "https://blog.example/story",
        source_id="blog",
        text="A completely different rewrite of the same news event, worded differently.",
    )

    result = deduplicate([first, second], priorities=PRIORITIES)

    assert len(result.kept) == 1
    assert result.dropped[0].reason == REASON_TITLE


def test_distinct_stories_all_survive() -> None:
    articles = [
        make_article(
            "https://wire.example/a", title="First distinct headline here", text="AAA aaa"
        ),
        make_article(
            "https://wire.example/b", title="Second distinct headline here", text="BBB bbb"
        ),
        make_article(
            "https://wire.example/c", title="Third distinct headline here", text="CCC ccc"
        ),
    ]
    result = deduplicate(articles, priorities=PRIORITIES)
    assert len(result.kept) == 3
    assert result.dropped == []


def test_very_short_titles_are_not_treated_as_duplicates() -> None:
    """A generic short headline is not evidence of the same story."""
    a = make_article("https://wire.example/a", title="AI news", text="First body text here.")
    b = make_article(
        "https://blog.example/b", source_id="blog", title="AI news", text="Other body."
    )
    assert len(deduplicate([a, b], priorities=PRIORITIES).kept) == 2


# --------------------------------------------------------------------------- #
# which copy survives — by rule, not by chance
# --------------------------------------------------------------------------- #


def test_highest_source_priority_wins() -> None:
    low = make_article("https://aggregator.example/x", source_id="aggregator")
    high = make_article("https://wire.example/y", source_id="wire")

    for order in ([low, high], [high, low]):
        result = deduplicate(order, priorities=PRIORITIES)
        assert result.kept[0].source_id == "wire"


def test_earliest_publication_breaks_a_priority_tie() -> None:
    later = make_article("https://wire.example/x", published_at=LATE)
    earlier = make_article("https://wire.example/y", published_at=EARLY)

    result = deduplicate([later, earlier], priorities=PRIORITIES)

    assert result.kept[0].published_at == EARLY


def test_article_id_breaks_a_full_tie() -> None:
    first = make_article("https://wire.example/x")
    second = make_article("https://wire.example/y")
    expected = min(first.article_id, second.article_id)

    result = deduplicate([first, second], priorities=PRIORITIES)

    assert result.kept[0].article_id == expected


def test_result_is_independent_of_input_order() -> None:
    articles = [
        make_article("https://wire.example/story", source_id="wire"),
        make_article("https://blog.example/story", source_id="blog", title="Another headline"),
        make_article(
            "https://aggregator.example/story", source_id="aggregator", text="Unique text"
        ),
    ]
    forward = deduplicate(articles, priorities=PRIORITIES)
    backward = deduplicate(list(reversed(articles)), priorities=PRIORITIES)

    assert [a.article_id for a in forward.kept] == [a.article_id for a in backward.kept]


def test_missing_priority_defaults_to_zero() -> None:
    known = make_article("https://wire.example/x", source_id="wire")
    unknown = make_article("https://unknown.example/y", source_id="unknown")

    result = deduplicate([unknown, known], priorities=PRIORITIES)

    assert result.kept[0].source_id == "wire"


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def test_dropped_articles_are_reported_with_reasons() -> None:
    articles = [
        make_article("https://wire.example/story"),
        make_article("https://www.wire.example/story/"),
        make_article(
            "https://blog.example/other",
            source_id="blog",
            title="An entirely unrelated headline",
            text="Distinct body text.",
        ),
    ]
    result = deduplicate(articles, priorities=PRIORITIES)

    assert result.dropped_count == 1
    assert result.reasons() == {REASON_URL: 1}
    assert result.dropped[0].article.canonical_url.startswith("https://")


def test_empty_input_is_handled() -> None:
    result = deduplicate([], priorities=PRIORITIES)
    assert result.kept == [] and result.dropped == []
