"""Normalization: untrusted HTML in, typed canonical record out."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from newsletter.models import (
    DiscoveredArticle,
    NormalizedArticle,
    PipelineStage,
    RawArticle,
    RunManifest,
    SourceConfig,
)
from newsletter.normalization.article import (
    MAX_TREE_DEPTH,
    NormalizationError,
    compute_article_id,
    compute_content_hash,
    headline_from_prose,
    is_account_title,
    normalize_all,
    normalize_article,
    normalize_text,
    unwrap_social_title,
)

RETRIEVED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
ARTICLE_URL = "https://news.example/2026/08/story"

BODY = (
    "<p>The company announced a new model with a larger context window, lower "
    "pricing and same-day availability for enterprise customers.</p>"
    "<p>Analysts expect competitors to respond within the quarter.</p>"
)


def build_html(
    *,
    head: str = "",
    body: str = BODY,
    title_tag: str = "<title>Doc Title</title>",
) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">{title_tag}{head}</head>
    <body><nav>Home About</nav><article>{body}</article><footer>Copyright</footer></body></html>"""


def make_raw(html: str, *, url: str = ARTICLE_URL, final_url: str | None = None) -> RawArticle:
    return RawArticle(
        source_id="news",
        url=url,
        final_url=final_url or url,
        raw_content=html,
        retrieved_at=RETRIEVED,
    )


def make_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "news",
        "name": "News Example",
        "entrypoint": "https://news.example/feed",
        "strategy": "rss",
        "priority": 7,
    }
    values.update(overrides)
    return SourceConfig(**values)  # type: ignore[arg-type]


def make_hint(**overrides: object) -> DiscoveredArticle:
    values: dict[str, object] = {
        "source_id": "news",
        "url": ARTICLE_URL,
        "title_hint": "Hinted title",
        "published_at_hint": PUBLISHED,
    }
    values.update(overrides)
    return DiscoveredArticle(**values)  # type: ignore[arg-type]


DATE_META = '<meta property="article:published_time" content="2026-08-17T10:00:00Z">'


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_normalizes_a_well_formed_page() -> None:
    html = build_html(
        head=(
            '<meta property="og:title" content="Example Labs ships a reasoning model">'
            f'{DATE_META}<meta name="author" content="Jane Doe">'
            '<link rel="canonical" href="https://news.example/2026/08/story">'
        )
    )
    article = normalize_article(make_raw(html), make_source())

    assert isinstance(article, NormalizedArticle)
    assert article.title == "Example Labs ships a reasoning model"
    assert article.canonical_url == ARTICLE_URL
    assert article.published_at == PUBLISHED
    assert article.author == "Jane Doe"
    assert "larger context window" in article.clean_text
    assert article.retrieved_at == RETRIEVED
    assert len(article.content_hash) == 64


def test_navigation_and_footer_noise_stay_out_of_the_text() -> None:
    article = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    assert "Home About" not in article.clean_text
    assert "Copyright" not in article.clean_text


def test_scripts_and_styles_never_reach_the_text() -> None:
    body = BODY + "<script>var tracker='NOISE';</script><style>.x{color:red}</style>"
    article = normalize_article(make_raw(build_html(head=DATE_META, body=body)), make_source())
    assert "NOISE" not in article.clean_text
    assert "color:red" not in article.clean_text


def test_a_pathologically_nested_page_still_yields_its_text() -> None:
    """A page nested past MAX_TREE_DEPTH must end in text, not in a RecursionError.

    Rule 7: one broken source may cost its own article, never the run. The guard
    stops descending and keeps whatever text it is standing on, which is the
    conservative half of the trade.
    """
    depth = MAX_TREE_DEPTH * 3
    body = "<div>" * depth + BODY + "</div>" * depth
    article = normalize_article(make_raw(build_html(head=DATE_META, body=body)), make_source())
    assert "larger context window" in article.clean_text


def test_configured_content_selector_wins() -> None:
    body = f'<div class="teaser">Ignore me entirely.</div><div class="real">{BODY}</div>'
    html = build_html(head=DATE_META, body=body)
    source = make_source(selectors={"content": "div.real"})
    article = normalize_article(make_raw(html), source)
    assert "Ignore me entirely" not in article.clean_text


# --------------------------------------------------------------------------- #
# titles
# --------------------------------------------------------------------------- #


def test_title_falls_back_through_the_chain() -> None:
    json_ld = (
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","headline":"LD Headline",'
        '"datePublished":"2026-08-17T10:00:00Z"}</script>'
    )
    from_ld = normalize_article(make_raw(build_html(head=json_ld)), make_source())
    assert from_ld.title == "LD Headline"

    from_h1 = normalize_article(
        make_raw(build_html(head=DATE_META, body=f"<h1>H1 Headline</h1>{BODY}")), make_source()
    )
    assert from_h1.title == "H1 Headline"

    from_title_tag = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    assert from_title_tag.title == "Doc Title"


def test_title_hint_is_the_last_resort() -> None:
    html = build_html(head=DATE_META, title_tag="")
    article = normalize_article(make_raw(html), make_source(), hint=make_hint())
    assert article.title == "Hinted title"


def test_untitled_page_is_rejected() -> None:
    html = build_html(head=DATE_META, title_tag="")
    with pytest.raises(NormalizationError, match="no title"):
        normalize_article(make_raw(html), make_source())


# --------------------------------------------------------------------------- #
# dates — never invented
# --------------------------------------------------------------------------- #


def test_date_comes_from_page_metadata() -> None:
    article = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    assert article.published_at == PUBLISHED


def test_date_can_come_from_json_ld() -> None:
    json_ld = (
        '<script type="application/ld+json">'
        '{"@type":"NewsArticle","datePublished":"2026-08-17T10:00:00Z"}</script>'
    )
    assert normalize_article(make_raw(build_html(head=json_ld)), make_source()).published_at == (
        PUBLISHED
    )


def test_date_can_come_from_a_time_element() -> None:
    body = f'<time datetime="2026-08-17T10:00:00Z">17 Aug</time>{BODY}'
    article = normalize_article(make_raw(build_html(body=body)), make_source())
    assert article.published_at == PUBLISHED


def test_discovery_hint_is_the_date_fallback() -> None:
    article = normalize_article(make_raw(build_html()), make_source(), hint=make_hint())
    assert article.published_at == PUBLISHED


def test_page_metadata_beats_the_feed_hint() -> None:
    """Both are the publisher's claim; the article page is the more specific one."""
    html = build_html(
        head='<meta property="article:published_time" content="2026-08-15T09:00:00Z">'
    )
    article = normalize_article(make_raw(html), make_source(), hint=make_hint())
    assert article.published_at == datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def test_an_undated_article_is_rejected_not_dated_now() -> None:
    hint = make_hint(published_at_hint=None)
    with pytest.raises(NormalizationError, match="refusing to invent"):
        normalize_article(make_raw(build_html()), make_source(), hint=hint)


def test_an_unparseable_date_is_treated_as_missing() -> None:
    html = build_html(head='<meta property="article:published_time" content="last Tuesday">')
    with pytest.raises(NormalizationError, match="refusing to invent"):
        normalize_article(make_raw(html), make_source())


# --------------------------------------------------------------------------- #
# canonical URL — attribution cannot be hijacked
# --------------------------------------------------------------------------- #


def test_page_canonical_is_honoured_within_the_same_site() -> None:
    head = DATE_META + '<link rel="canonical" href="https://www.news.example/canonical-path">'
    raw = make_raw(build_html(head=head), url=f"{ARTICLE_URL}?utm_source=rss")
    article = normalize_article(raw, make_source())
    assert article.canonical_url == "https://www.news.example/canonical-path"


def test_cross_site_canonical_is_ignored() -> None:
    """Untrusted markup must not be able to redirect credit to another domain."""
    head = DATE_META + '<link rel="canonical" href="https://content-farm.example/stolen">'
    article = normalize_article(make_raw(build_html(head=head)), make_source())
    assert article.canonical_url == ARTICLE_URL


def test_tracking_parameters_are_stripped_from_the_canonical_url() -> None:
    raw = make_raw(build_html(head=DATE_META), url=f"{ARTICLE_URL}?utm_source=rss&fbclid=abc")
    assert normalize_article(raw, make_source()).canonical_url == ARTICLE_URL


def test_the_final_url_after_redirects_is_used() -> None:
    raw = make_raw(
        build_html(head=DATE_META),
        url="https://news.example/short-link",
        final_url=ARTICLE_URL,
    )
    assert normalize_article(raw, make_source()).canonical_url == ARTICLE_URL


# --------------------------------------------------------------------------- #
# hashing and identity
# --------------------------------------------------------------------------- #


def test_identical_content_hashes_identically() -> None:
    first = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    second = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    assert first.content_hash == second.content_hash


def test_whitespace_differences_do_not_change_the_hash() -> None:
    spaced = BODY.replace("<p>", "<p>\n   ").replace("</p>", "  \n</p>")
    a = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    b = normalize_article(make_raw(build_html(head=DATE_META, body=spaced)), make_source())
    assert a.content_hash == b.content_hash


def test_different_content_hashes_differently() -> None:
    other = BODY.replace("larger context window", "smaller context window")
    a = normalize_article(make_raw(build_html(head=DATE_META)), make_source())
    b = normalize_article(make_raw(build_html(head=DATE_META, body=other)), make_source())
    assert a.content_hash != b.content_hash


def test_article_id_is_stable_across_url_variants() -> None:
    assert compute_article_id(ARTICLE_URL) == compute_article_id(f"{ARTICLE_URL}/")
    assert compute_article_id(ARTICLE_URL) == compute_article_id(
        "https://www.news.example/2026/08/story?utm_source=x"
    )
    assert compute_article_id(ARTICLE_URL) != compute_article_id(
        "https://news.example/2026/08/other"
    )


def test_normalize_text_preserves_paragraphs_but_collapses_spaces() -> None:
    assert normalize_text("  a   b  \n\n\n  c  ") == "a b\nc"


def test_content_hash_is_a_pure_function_of_text() -> None:
    assert compute_content_hash("abc") == compute_content_hash("abc")
    assert compute_content_hash("abc") != compute_content_hash("abd")


# --------------------------------------------------------------------------- #
# rejection and failure isolation
# --------------------------------------------------------------------------- #


def test_a_page_with_almost_no_text_is_rejected() -> None:
    html = build_html(head=DATE_META, body="<p>Too short.</p>")
    with pytest.raises(NormalizationError, match="too short"):
        normalize_article(make_raw(html), make_source())


def test_normalize_all_skips_failures_and_records_them() -> None:
    good = make_raw(build_html(head=DATE_META), url=ARTICLE_URL)
    undated = make_raw(build_html(), url="https://news.example/2026/08/undated")
    also_good = make_raw(build_html(head=DATE_META), url="https://news.example/2026/08/second")
    manifest = RunManifest(run_id="r1", started_at=RETRIEVED)

    articles = normalize_all([good, undated, also_good], {"news": make_source()}, manifest=manifest)

    assert len(articles) == 2
    assert len(manifest.errors) == 1
    assert manifest.errors[0].stage is PipelineStage.NORMALIZE
    assert manifest.errors[0].source_id == "news"
    assert "invent" in manifest.errors[0].message


def test_normalize_all_uses_hints_keyed_by_url() -> None:
    raw = make_raw(build_html(), url=ARTICLE_URL)
    manifest = RunManifest(run_id="r1", started_at=RETRIEVED)

    articles = normalize_all(
        [raw], {"news": make_source()}, manifest=manifest, hints={ARTICLE_URL: make_hint()}
    )

    assert len(articles) == 1
    assert articles[0].published_at == PUBLISHED
    assert manifest.errors == []


# --------------------------------------------------------------------------- #
# headline fallback — a social page titles itself by its account
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title",
    ["Grok (@grok) on X", "@grok on X", "Some Long Product Name (@handle) on Bluesky"],
)
def test_a_title_that_names_the_account_is_recognised(title: str) -> None:
    assert is_account_title(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "Asana cleared five years of engineering work in two weeks",
        "Firefox's Smart Window promises a better AI browser",
        "OpenAI expands ChatGPT Ads to 31 European markets",
    ],
)
def test_a_real_headline_is_left_alone(title: str) -> None:
    assert is_account_title(title) is False


def test_an_account_title_falls_back_to_the_description() -> None:
    """What a post says is a better headline than who posted it."""
    head = (
        '<meta property="og:title" content="Grok (@grok) on X">'
        '<meta property="og:description" content="Homer had a lyre. You have Grok Imagine. '
        'Create a scene from The Odyssey and win $100K.">' + DATE_META
    )
    article = normalize_article(make_raw(build_html(head=head, title_tag="")), make_source())
    assert article.title == "Homer had a lyre. You have Grok Imagine."


def test_the_social_wrapper_is_stripped_from_a_title() -> None:
    assert unwrap_social_title('Grok on X: "Homer had a lyre and a plan"') == (
        "Homer had a lyre and a plan"
    )
    assert unwrap_social_title("A normal headline") == "A normal headline"


def test_an_account_title_survives_when_there_is_nothing_better() -> None:
    """A poor headline still beats rejecting the article outright."""
    head = '<meta property="og:title" content="Grok (@grok) on X">' + DATE_META
    article = normalize_article(make_raw(build_html(head=head, title_tag="")), make_source())
    assert article.title == "Grok (@grok) on X"


def test_a_fallback_headline_is_cut_on_a_sentence() -> None:
    prose = "The lab shipped a model. It costs less than the last one. Availability starts today."
    assert headline_from_prose(prose) == "The lab shipped a model. It costs less than the last one."


def test_a_fallback_headline_is_cut_on_a_word_when_the_sentence_runs_long() -> None:
    prose = "The laboratory announced " + "a very substantial and detailed change " * 6
    headline = headline_from_prose(prose)
    assert len(headline) <= 121
    assert headline.endswith("…")
    assert not headline[:-1].endswith(" ")


def test_a_short_headline_is_returned_whole() -> None:
    assert headline_from_prose("Model ships today.") == "Model ships today."
