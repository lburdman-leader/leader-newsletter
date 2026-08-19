"""URL canonicalization: publishable form vs comparison key."""

from __future__ import annotations

import pytest

from newsletter.normalization.urls import (
    canonicalize_url,
    dedupe_key,
    is_tracking_param,
    same_site,
    strip_tracking_params,
)

ARTICLE = "https://news.example/2026/08/story"


# --------------------------------------------------------------------------- #
# tracking parameters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["utm_source", "utm_medium", "UTM_Campaign", "fbclid", "gclid", "mc_cid", "igshid", "si"],
)
def test_analytics_parameters_are_recognised(name: str) -> None:
    assert is_tracking_param(name) is True


@pytest.mark.parametrize("name", ["id", "page", "q", "lang", "ref", "source", "v"])
def test_content_bearing_parameters_are_kept(name: str) -> None:
    """Dropping a parameter that selects content would break the published link."""
    assert is_tracking_param(name) is False


def test_tracking_parameters_are_removed_and_the_rest_sorted() -> None:
    assert strip_tracking_params("b=2&utm_source=x&a=1&fbclid=y") == "a=1&b=2"


def test_query_that_is_entirely_tracking_disappears() -> None:
    assert canonicalize_url(f"{ARTICLE}?utm_source=news&utm_medium=email") == ARTICLE


def test_tracked_and_untracked_urls_canonicalize_identically() -> None:
    tracked = f"{ARTICLE}?utm_source=twitter&utm_campaign=launch#section-2"
    assert canonicalize_url(tracked) == canonicalize_url(ARTICLE) == ARTICLE


# --------------------------------------------------------------------------- #
# canonicalize_url — conservative, publishable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://News.Example/Path", "https://news.example/Path"),
        ("https://news.example:443/a", "https://news.example/a"),
        ("http://news.example:80/a", "http://news.example/a"),
        ("https://news.example:8443/a", "https://news.example:8443/a"),
        ("https://news.example/a#anchor", "https://news.example/a"),
        ("  https://news.example/a  ", "https://news.example/a"),
    ],
)
def test_canonicalization_rules(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_path_case_and_trailing_slash_are_preserved_for_publication() -> None:
    """The published link must keep working; only comparison may be aggressive."""
    assert canonicalize_url("https://news.example/Section/Story/") == (
        "https://news.example/Section/Story/"
    )
    assert canonicalize_url("https://www.news.example/a") == "https://www.news.example/a"


def test_content_parameters_survive_canonicalization() -> None:
    assert canonicalize_url(f"{ARTICLE}?page=2&utm_source=x") == f"{ARTICLE}?page=2"


def test_canonicalization_is_idempotent() -> None:
    once = canonicalize_url(f"{ARTICLE}?utm_source=x#frag")
    assert canonicalize_url(once) == once


@pytest.mark.parametrize(
    "bad", ["javascript:alert(1)", "file:///etc/passwd", "/relative", "", "https://"]
)
def test_unpublishable_urls_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_url(bad)


# --------------------------------------------------------------------------- #
# dedupe_key — aggressive, never published
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "variant",
    [
        "https://news.example/2026/08/story",
        "https://www.news.example/2026/08/story",
        "https://news.example/2026/08/story/",
        "https://news.example/2026/08/story/index.html",
        "https://news.example/2026/08/story?utm_source=rss",
        "HTTPS://NEWS.EXAMPLE/2026/08/story#top",
    ],
)
def test_equivalent_urls_share_one_dedupe_key(variant: str) -> None:
    assert dedupe_key(variant) == dedupe_key(ARTICLE)


def test_different_articles_keep_different_keys() -> None:
    assert dedupe_key(ARTICLE) != dedupe_key("https://news.example/2026/08/other-story")
    assert dedupe_key(ARTICLE) != dedupe_key("https://other.example/2026/08/story")


def test_dedupe_key_keeps_content_parameters() -> None:
    assert dedupe_key(f"{ARTICLE}?page=2") != dedupe_key(ARTICLE)


# --------------------------------------------------------------------------- #
# same_site — the attribution guard
# --------------------------------------------------------------------------- #


def test_same_site_ignores_www_and_scheme_case() -> None:
    assert same_site("https://www.news.example/a", "https://news.example/b") is True


def test_same_site_rejects_other_hosts() -> None:
    assert same_site("https://evil.example/a", "https://news.example/a") is False
    assert same_site("https://news.example.evil.com/a", "https://news.example/a") is False


def test_same_site_is_false_for_unusable_input() -> None:
    assert same_site("javascript:alert(1)", "https://news.example/a") is False
