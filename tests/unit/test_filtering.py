"""The authoritative date filter (AC6): deterministic, half-open, Python-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from newsletter.models import DateWindow, NormalizedArticle
from newsletter.normalization.filtering import filter_by_window

START = datetime(2026, 8, 11, tzinfo=UTC)
END = datetime(2026, 8, 18, tzinfo=UTC)
WINDOW = DateWindow(start=START, end=END)


def make_article(published_at: datetime, article_id: str = "a1") -> NormalizedArticle:
    return NormalizedArticle(
        article_id=article_id,
        source_id="news",
        canonical_url=f"https://news.example/{article_id}",
        title=f"Story {article_id}",
        published_at=published_at,
        clean_text="Body text long enough to be a real article body for testing.",
        content_hash=f"contenthash-{article_id}",
        retrieved_at=END,
    )


def test_window_start_is_inclusive() -> None:
    inside, outside = filter_by_window([make_article(START)], WINDOW)
    assert len(inside) == 1 and not outside


def test_window_end_is_exclusive() -> None:
    inside, outside = filter_by_window([make_article(END)], WINDOW)
    assert not inside and len(outside) == 1


def test_the_last_instant_before_the_end_is_included() -> None:
    inside, _ = filter_by_window([make_article(END - timedelta(microseconds=1))], WINDOW)
    assert len(inside) == 1


def test_the_instant_before_the_start_is_excluded() -> None:
    _, outside = filter_by_window([make_article(START - timedelta(microseconds=1))], WINDOW)
    assert len(outside) == 1


def test_articles_are_partitioned_without_loss() -> None:
    articles = [
        make_article(datetime(2026, 8, 10, tzinfo=UTC), "before"),
        make_article(datetime(2026, 8, 12, tzinfo=UTC), "inside1"),
        make_article(datetime(2026, 8, 17, 23, 59, tzinfo=UTC), "inside2"),
        make_article(datetime(2026, 8, 20, tzinfo=UTC), "after"),
    ]
    inside, outside = filter_by_window(articles, WINDOW)

    assert [a.article_id for a in inside] == ["inside1", "inside2"]
    assert [a.article_id for a in outside] == ["before", "after"]
    assert len(inside) + len(outside) == len(articles)


def test_input_order_is_preserved() -> None:
    articles = [
        make_article(datetime(2026, 8, 16, tzinfo=UTC), "second"),
        make_article(datetime(2026, 8, 12, tzinfo=UTC), "first"),
    ]
    inside, _ = filter_by_window(articles, WINDOW)
    assert [a.article_id for a in inside] == ["second", "first"]


def test_comparison_is_correct_across_timezones() -> None:
    """21:00 on the 17th in UTC-4 is 01:00 on the 18th UTC: outside the window."""
    late = datetime(2026, 8, 17, 21, 0, tzinfo=timezone(timedelta(hours=-4)))
    inside, outside = filter_by_window([make_article(late)], WINDOW)
    assert not inside and len(outside) == 1


def test_filtering_is_repeatable() -> None:
    articles = [make_article(datetime(2026, 8, 12, tzinfo=UTC), f"a{i}") for i in range(5)]
    first = [a.article_id for a in filter_by_window(articles, WINDOW)[0]]
    second = [a.article_id for a in filter_by_window(articles, WINDOW)[0]]
    assert first == second


def test_empty_input_is_handled() -> None:
    assert filter_by_window([], WINDOW) == ([], [])
