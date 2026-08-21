"""Extraction against real, chrome-heavy pages: the body survives, the furniture does not.

Every fixture under the four directories used here is a live page captured
verbatim on 2026-08-20 -- navigation, rails, footers, inline CSS and all. Nothing
is trimmed, because the chrome *is* the subject: synthetic markup cannot prove
anything about extraction, since whoever writes the markup also decides what the
extractor will find.

The four sources were chosen for the four shapes that matter:

* **The Verge** wraps every article in a mega-navigation of topic panels, each
  repeating "Posts from this topic will be added to your daily email digest";
* **TechCrunch** ends every article with a rotating "latest stories" rail, which
  is what made two unrelated TechCrunch stories score 0.4637 against each other
  in the run recorded in ADR-0034 -- higher than three real reports of one event;
* **Cartoon Brew** carries ~200KB of inline CSS and a related-stories rail;
* **Tubefilter** states neither ``<article>`` nor ``<main>``, so it exercises the
  ``<body>`` fallback on a page that names no container at all.

Two pages per source, so the same-source pair can be compared: two articles from
one outlet share their chrome and nothing else, which is exactly the false
positive the similarity pass had to be taught to work around.

Everything here is offline; ``tests/conftest.py`` enforces that.
"""

from __future__ import annotations

from functools import cache

import pytest
from scrapling import Selector

from newsletter.config import NewsletterSettings
from newsletter.models import NormalizedArticle, SourceConfig
from newsletter.normalization.article import (
    MAX_LINK_DENSITY,
    compute_content_hash,
    extract_text,
    link_density,
)
from newsletter.ranking.dedupe import content_similarity, similarity_profiles
from tests.conftest import WINDOW_START, read_fixture

#: The captured pages, by ``<source id>/<fixture>``.
PAGES: tuple[str, ...] = (
    "techcrunch-ai/article-crypto-lure",
    "techcrunch-ai/article-publisher-controls",
    "cartoon-brew/article-golden-axe",
    "cartoon-brew/article-merger-job-losses",
    "theverge-ai/article-2xko",
    "theverge-ai/article-roblox-australia",
    "tubefilter/article-uno-twitch",
    "tubefilter/article-vine-successor",
)


def source_for(source_id: str) -> SourceConfig:
    """A source declaring no content selector, as all four do in production."""
    return SourceConfig(
        id=source_id,
        name=source_id,
        entrypoint=f"https://{source_id}.invalid/feed",
        strategy="rss",
        priority=5,
    )


def parse(page_id: str) -> Selector:
    source_id, name = page_id.split("/")
    return Selector(
        read_fixture(source_id, f"{name}.html"), url=f"https://{source_id}.invalid/story"
    )


@cache
def clean_text(page_id: str) -> str:
    """The extracted body of one captured page. Cached: the pages are large."""
    source_id, _ = page_id.split("/")
    return extract_text(parse(page_id), source_for(source_id))


def raw_html(page_id: str) -> str:
    source_id, name = page_id.split("/")
    return read_fixture(source_id, f"{name}.html")


# --------------------------------------------------------------------------- #
# the body survives — the failure that would matter most
# --------------------------------------------------------------------------- #

#: First and last sentence of each article's real body, read off the page.
BODY_ENDS: dict[str, tuple[str, str]] = {
    "techcrunch-ai/article-crypto-lure": (
        "If you are a malicious hacker, cybersecurity professionals may very well be the "
        "worst people in the world to try to hack",
        "Google did not immediately when TechCrunch reached out asking if the company had "
        "seen this hacking campaign, or similar ones.",
    ),
    "techcrunch-ai/article-publisher-controls": (
        "As AI continues to kill traffic to websites, Google on Thursday threw a bone to "
        "those publishers negatively impacted by the change.",
        "In addition to personalizing the Discover feed, Google says Android users will be "
        "able to customize their audio daily briefings in the Google News app, as well.",
    ),
    "cartoon-brew/article-golden-axe": (
        "Paramount+ will premiere all ten episodes of its animated Golden Axe adaptation on "
        "September 16 and has released a trailer for the upcoming series.",
        "The voice cast features Matthew Rhys as the ill-tempered dwarf Gilius Thunderhead",
    ),
    "cartoon-brew/article-merger-job-losses": (
        "If allowed to proceed, Paramount Skydance\u2019s proposed acquisition of Warner "
        "Bros. Discovery could be even more disastrous than many originally thought.",
        "As the county report emphasizes, however, a release commitment does not guarantee "
        "production, animation, VFX, or post-production work in Los Angeles, or even the U.S.",
    ),
    "theverge-ai/article-2xko": (
        "Riot Games is already winding down work on 2XKO, the free-to-play League of Legends "
        "fighting game, less than a year after its initial launch.",
        "Even Fortnite isn\u2019t immune to problems, as developer Epic Games laid off staff "
        "in March following a \u201cdownturn in Fortnite engagement.\u201d",
    ),
    "theverge-ai/article-roblox-australia": (
        "Roblox is promising more changes to its child safety features following testing "
        "from Australia\u2019s online safety regulator, eSafety.",
        "In the US, Roblox is under investigation by a Senate subcommittee and has been sued "
        "by multiple states over alleged child safety issues, though it has settled some "
        "state lawsuits.",
    ),
    "tubefilter/article-uno-twitch": (
        "It\u2019s a headline that seems ripe for satire: UNO is trying to become the hot new "
        "esport.",
        "The biggest names in turn-based cards will be in L.A. on November 11.",
    ),
    "tubefilter/article-vine-successor": (
        "A new video sharing service backed by former Twitter chief Jack Dorsey is now open "
        "to the public, and Taco Bell has come on board as the app\u2019s first official "
        "brand partner.",
        "If you want to see what the app\u2019s community looks like at launch, you can fire "
        "up that Taco Bell code and check it out.",
    ),
}


@pytest.mark.parametrize("page_id", PAGES)
def test_the_article_body_survives_from_first_sentence_to_last(page_id: str) -> None:
    """Over-stripping is the failure to fear: it degrades every later judgement silently."""
    text = clean_text(page_id)
    opening, closing = BODY_ENDS[page_id]
    assert opening in text
    assert closing in text
    assert text.index(opening) < text.index(closing)


# --------------------------------------------------------------------------- #
# the chrome does not
# --------------------------------------------------------------------------- #

#: ``(page, phrase)`` -- text the page really carries and the body really does not.
CHROME_PHRASES: tuple[tuple[str, str], ...] = (
    # The Verge's topic panels, repeated once per topic on every article.
    ("theverge-ai/article-2xko", "Posts from this topic will be added to your daily email"),
    ("theverge-ai/article-roblox-australia", "Advertiser Content From"),
    # TechCrunch's rotating rail: another story's headline, and another author's byline.
    ("techcrunch-ai/article-crypto-lure", "Cursor capitalizes on GitHub frustration"),
    (
        "techcrunch-ai/article-publisher-controls",
        "7 desk gadgets that can make your workday better",
    ),
    # Cartoon Brew's related-stories rail, and its 200KB of inline CSS.
    ("cartoon-brew/article-golden-axe", "Strawberry Vampire"),
    ("cartoon-brew/article-golden-axe", "font-family"),
    ("cartoon-brew/article-merger-job-losses", "Studio Ghibli On Its Next Film"),
    # Tubefilter's rails, reached through the <body> fallback.
    ("tubefilter/article-uno-twitch", "As Twitch tries to crack down on viewbots"),
    ("tubefilter/article-vine-successor", "Trending Stories"),
)


@pytest.mark.parametrize(("page_id", "phrase"), CHROME_PHRASES)
def test_page_chrome_never_reaches_the_extracted_body(page_id: str, phrase: str) -> None:
    """The page states it; the body must not. Asserting both keeps the test honest."""
    assert phrase in raw_html(page_id), "the fixture no longer carries this chrome"
    assert phrase not in clean_text(page_id)


def test_link_density_tells_a_rail_from_a_paragraph_that_cites_sources() -> None:
    """The measurement itself, on one real page: a rail is links, prose is words."""
    page = parse("tubefilter/article-uno-twitch")

    rails = [node for node in page.css("ul") if "crack down on viewbots" in node.get_all_text()]
    prose = [node for node in page.css("p") if "Once upon a time, chess" in node.get_all_text()]
    assert rails and prose, "the fixture no longer holds both shapes"

    assert link_density(rails[0]) >= MAX_LINK_DENSITY
    assert link_density(prose[0]) < MAX_LINK_DENSITY

    text = clean_text("tubefilter/article-uno-twitch")
    assert "Once upon a time, chess" in text  # a paragraph that links out is still prose


@pytest.mark.parametrize(
    "page_id", ("tubefilter/article-uno-twitch", "tubefilter/article-vine-successor")
)
def test_a_page_that_names_no_container_still_yields_its_body(page_id: str) -> None:
    """Tubefilter states neither <article> nor <main>: the <body> fallback carries it."""
    page = parse(page_id)
    assert not len(page.css("article"))
    assert not len(page.css("main"))

    text = clean_text(page_id)
    opening, closing = BODY_ENDS[page_id]
    assert opening in text
    assert closing in text
    assert len(text) > 3_000


@pytest.mark.parametrize("page_id", PAGES)
def test_identical_html_always_yields_identical_text(page_id: str) -> None:
    """AC9. Same bytes in, same bytes out -- and so the same content hash."""
    source_id, _ = page_id.split("/")
    source = source_for(source_id)
    first = extract_text(parse(page_id), source)
    second = extract_text(parse(page_id), source)
    assert first == second
    assert compute_content_hash(first) == compute_content_hash(second)


# --------------------------------------------------------------------------- #
# what the chrome was doing to the similarity pass
# --------------------------------------------------------------------------- #


def as_article(index: int, page_id: str) -> NormalizedArticle:
    source_id, name = page_id.split("/")
    return NormalizedArticle(
        article_id=f"{index:016d}",
        source_id=source_id,
        canonical_url=f"https://{source_id}.invalid/{name}",
        title=name,
        published_at=WINDOW_START,
        author=None,
        clean_text=clean_text(page_id),
        content_hash=compute_content_hash(clean_text(page_id)),
        retrieved_at=WINDOW_START,
    )


#: Ceiling per same-source pair, and what the pair scored on the same eight
#: documents before the chrome came out: TechCrunch 0.3714, Cartoon Brew 0.4756,
#: The Verge 0.4771, Tubefilter 0.3191. The Cartoon Brew pair is the one ADR-0034
#: names, so it is held to the configured collapse threshold; the others are held
#: below the level a shared rail produces. Eight documents are too few for inverse
#: document frequency to discount ordinary English, so what is left in these
#: numbers is mostly "the", "to" and "a" -- on the 166-page corpus this change was
#: measured against, the TechCrunch pair fell from 0.4547 to 0.2081.
PAIR_CEILINGS: tuple[tuple[str, float], ...] = (
    ("cartoon-brew", NewsletterSettings().similar_event_threshold),
    ("theverge-ai", 0.30),
    ("techcrunch-ai", 0.30),
    ("tubefilter", 0.30),
)


@pytest.mark.parametrize(("source_id", "ceiling"), PAIR_CEILINGS)
def test_two_articles_from_one_source_no_longer_share_a_rail(
    source_id: str, ceiling: float
) -> None:
    """Unrelated stories from one outlet must not read as one event."""
    articles = [as_article(index, page_id) for index, page_id in enumerate(PAGES)]
    profiles = similarity_profiles(articles)
    pair = [index for index, article in enumerate(articles) if article.source_id == source_id]
    assert len(pair) == 2

    assert content_similarity(profiles[pair[0]], profiles[pair[1]]) < ceiling
