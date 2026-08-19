"""Deterministic deduplication before any model call."""

from __future__ import annotations

from datetime import UTC, datetime

from newsletter.config import NewsletterSettings
from newsletter.models import ArticleAssessment, NormalizedArticle, RankedArticle, TopicCategory
from newsletter.normalization.article import compute_article_id
from newsletter.ranking.dedupe import (
    REASON_CONTENT,
    REASON_TITLE,
    REASON_URL,
    PublishedKeys,
    collapse_similar_events,
    deduplicate,
    event_collapse_key,
    normalize_entity,
    normalize_title,
)

#: The tests exercise the threshold the newsletter actually ships with, so a
#: retuning that breaks the case this pass exists for fails here.
SIMILAR_EVENT_THRESHOLD = NewsletterSettings().similar_event_threshold
#: The pass folds only what could be published, so every similarity test has
#: to state the floor it is working above.
SIMILAR_EVENT_MIN_SCORE = NewsletterSettings().min_score

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


# --------------------------------------------------------------------------- #
# the collapse key — one event, one story
# --------------------------------------------------------------------------- #


def make_assessment(
    subject: str | None,
    action: str | None,
    obj: str | None,
    date: str | None = None,
) -> ArticleAssessment:
    return ArticleAssessment(
        category=TopicCategory.AI_MODELS,
        topic_relevance=5,
        business_impact=4,
        novelty=5,
        actionability=3,
        confidence=0.9,
        summary="A summary of the announcement.",
        why_it_matters="It matters for enterprise readers.",
        event_subject=subject,
        event_action=action,
        event_object=obj,
        event_date=date,
    )


#: One launch as three outlets filed it: a different verb each time, different
#: punctuation and casing, a date in one report and none in the others.
CHATGPT_FOR_TEENS = (
    ("OpenAI", "launches", "ChatGPT for Teens", None),
    ("OpenAI", "introduces", "chatgpt-for-teens", "2026-08-14"),
    ("openai", "announces", "The ChatGPT For Teens", None),
)


def test_one_launch_reported_three_ways_produces_a_single_collapse_key() -> None:
    keys = {event_collapse_key(make_assessment(*event)) for event in CHATGPT_FOR_TEENS}
    assert keys == {"openai|chatgpt for teens"}


def test_the_verb_and_the_date_are_not_part_of_the_collapse_key() -> None:
    """Exactly where two accounts of one announcement diverge."""
    assert event_collapse_key(make_assessment("OpenAI", "launches", "Sora", "2026-08-14")) == (
        event_collapse_key(make_assessment("OpenAI", "announces", "Sora", None))
    )


def test_two_different_events_from_one_company_keep_different_keys() -> None:
    launch = make_assessment("OpenAI", "launches", "ChatGPT for Teens", None)
    settlement = make_assessment("OpenAI", "settles", "a copyright lawsuit", "2026-08-15")
    assert event_collapse_key(launch) != event_collapse_key(settlement)


def test_accents_and_spacing_never_split_one_event() -> None:
    accented = make_assessment("Telefónica", "lanza", "  Vídeo   IA ", None)
    plain = make_assessment("TELEFONICA", "presenta", "video ia", "2026-08-12")
    assert event_collapse_key(accented) == event_collapse_key(plain) == "telefonica|video ia"


def test_an_event_missing_a_subject_or_an_object_has_no_collapse_key() -> None:
    """Fail open: an unknown event is not evidence of a duplicate."""
    assert event_collapse_key(make_assessment(None, "launches", "ChatGPT for Teens")) is None
    assert event_collapse_key(make_assessment("OpenAI", "launches", None)) is None
    assert event_collapse_key(make_assessment("The", "launches", "ChatGPT for Teens")) is None


def test_the_four_part_event_fingerprint_still_means_what_it_meant() -> None:
    """The collapse key is a new function, not a redefinition of the old one."""
    assessment = make_assessment("OpenAI", "launches", "ChatGPT for Teens", "2026-08-14")
    assert assessment.event_fingerprint() == "openai|launches|chatgpt for teens|2026-08-14"
    assert event_collapse_key(assessment) == "openai|chatgpt for teens"


def test_entity_normalization_drops_only_a_leading_article() -> None:
    assert normalize_entity("The Verge") == "verge"
    assert normalize_entity("A Startup") == "startup"
    assert normalize_entity("Netflix and the BBC") == "netflix and the bbc"
    assert normalize_entity("   ") == ""


# --------------------------------------------------------------------------- #
# published identity keys — the durable "printed only once" guarantee
# --------------------------------------------------------------------------- #


def test_empty_published_keys_suppress_nothing() -> None:
    keys = PublishedKeys()
    assert not keys
    assert keys.issue_for(make_article("https://wire.example/story")) is None


def test_a_published_article_is_recognized_by_its_id() -> None:
    article = make_article("https://wire.example/story")
    keys = PublishedKeys(by_article_id={article.article_id: "2026-W33"})
    assert keys.issue_for(article) == "2026-W33"


def test_the_same_text_at_a_new_url_is_recognized_by_its_content_hash() -> None:
    original = make_article("https://wire.example/story")
    syndicated = make_article("https://blog.example/reprint", title="A completely other headline")
    keys = PublishedKeys(by_content_hash={original.content_hash: "2026-W30"})
    assert keys.issue_for(syndicated) == "2026-W30"


def test_a_headline_an_earlier_edition_used_does_not_suppress_a_new_story() -> None:
    """Across editions the title is a guess, and a guess must not be permanent.

    "YouTube changes its monetization rules" is a plausible headline in March and
    again in September. Inside one run that repetition is evidence and costs at
    most one story; across editions it would bury the September story forever.
    """
    published = make_article("https://wire.example/story")
    rewritten = make_article("https://blog.example/again", text="Entirely different body text.")
    assert rewritten.title == published.title
    assert rewritten.article_id != published.article_id
    assert rewritten.content_hash != published.content_hash

    keys = PublishedKeys(by_article_id={published.article_id: "2026-W31"})
    assert keys.issue_for(rewritten) is None


def test_the_title_still_collapses_duplicates_inside_one_run() -> None:
    """The same key, kept exactly where its consequence lasts a week."""
    result = deduplicate(
        [
            make_article("https://wire.example/story"),
            make_article("https://blog.example/again", text="Entirely different body text."),
        ],
        priorities=PRIORITIES,
    )
    assert len(result.kept) == 1
    assert [dropped.reason for dropped in result.dropped] == [REASON_TITLE]


def test_published_keys_carry_no_title_mapping_at_all() -> None:
    """Not merely unused: the key is gone, so nothing can reintroduce it quietly."""
    assert not hasattr(PublishedKeys(), "by_title_key")


# --------------------------------------------------------------------------- #
# content similarity — the collapse the exact keys cannot reach
# --------------------------------------------------------------------------- #


def make_ranked(
    article_id: str,
    *,
    source_id: str,
    title: str,
    text: str,
    score: int = 80,
) -> RankedArticle:
    return RankedArticle(
        article=make_article(
            f"https://{source_id}.example/{article_id}",
            source_id=source_id,
            title=title,
            text=text,
        ),
        assessment=make_assessment("Example Labs", "launches", article_id),
        source_name=source_id,
        source_priority=PRIORITIES.get(source_id, 5),
        final_score=score,
    )


#: One launch as three outlets actually file it: the same rare words -- teens,
#: parental, safeguards, distress -- and almost no shared phrasing. That is the
#: case verbatim shingling misses and the analyzer fingerprint disagrees about.
VENDOR_POST = """
Introducing ChatGPT for Teens, built for learning and backed by parental controls.
Starting today, teenagers aged 13 to 17 are moved into a separate ChatGPT
experience with age prediction, parental controls and stricter safeguards around
self-harm, disordered eating and romantic roleplay. A parent can link an account,
set quiet hours and receive a notification when our systems detect a teenager in
acute distress. Schools can turn the homework mode on by default.
"""

OUTLET_REPORT = """
ChatGPT is getting a dedicated mode for teens. The company said on Tuesday that
teenagers between 13 and 17 will be moved into a separate experience carrying
parental controls, age prediction and tighter safeguards covering self-harm,
disordered eating and romantic roleplay. Parents will be able to link an account,
set quiet hours and get a notification if the company believes their teenager is
in acute distress. The homework mode reaches schools later this year.
"""

RIVAL_REPORT = """
A safer ChatGPT for teens arrives years after teenagers started using it anyway.
Teen accounts for 13- to 17-year-olds bring parental controls, age prediction and
new safeguards around self-harm, disordered eating and romantic roleplay. Parents
get quiet hours and a distress notification, and schools get a homework mode.
Critics point out that teenagers have been doing homework with the chatbot,
largely unsupervised, since the day it launched.
"""

#: The same company and the same product, a completely different event. Exactly
#: the pair a topic-level collapse would destroy.
SAME_COMPANY_OTHER_EVENT = """
The company has appointed a new chief revenue officer, hired away from a payments
business, to run enterprise sales. The appointment follows a year in which
corporate subscriptions overtook consumer ones as the largest single line of
revenue, and it lands while the finance team prepares a funding round that several
investors expect to value the business above one hundred billion dollars.
"""


def three_outlets_on_one_launch() -> list[RankedArticle]:
    return [
        make_ranked(
            "vendor-post",
            source_id="vendor",
            title="Introducing ChatGPT for Teens",
            text=VENDOR_POST,
            score=88,
        ),
        make_ranked(
            "outlet-report",
            source_id="outlet",
            title="ChatGPT is getting a dedicated mode for teens",
            text=OUTLET_REPORT,
            score=82,
        ),
        make_ranked(
            "rival-report",
            source_id="rival",
            title="A safer ChatGPT for teens, years late",
            text=RIVAL_REPORT,
            score=80,
        ),
    ]


def slug(ranked: RankedArticle) -> str:
    """The name a test gave an article. ``article_id`` is derived from the URL."""
    return ranked.article.canonical_url.rsplit("/", 1)[-1]


def one_other_story() -> RankedArticle:
    return make_ranked(
        "appointment",
        source_id="rival",
        title="The company names a new chief revenue officer",
        text=SAME_COMPANY_OTHER_EVENT,
        score=79,
    )


def test_three_outlets_on_one_launch_become_one_story() -> None:
    """The owner's complaint, reproduced: three of eight stories, one event."""
    kept, collapsed = collapse_similar_events(
        three_outlets_on_one_launch(),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["vendor-post"]
    assert [slug(folded) for folded, _ in collapsed] == [
        "outlet-report",
        "rival-report",
    ]


def test_the_analyzer_fingerprint_could_never_have_caught_them() -> None:
    """Why this pass exists: each article is assessed alone, so the keys disagree."""
    keys = {
        event_collapse_key(make_assessment(*event))
        for event in (
            ("OpenAI", "launches", "ChatGPT for Teens", None),
            ("ChatGPT", "adds", "teen accounts", None),
            ("OpenAI", "introduces", "a teen mode", None),
        )
    }
    assert len(keys) == 3


def test_the_survivor_is_the_best_of_them_not_the_first_seen() -> None:
    kept, collapsed = collapse_similar_events(
        list(reversed(three_outlets_on_one_launch())),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["vendor-post"]
    assert all(slug(survivor) == "vendor-post" for _, survivor in collapsed)


def test_two_events_from_one_company_are_not_one_story() -> None:
    """Same company, same product, different news. Collapsing these is censorship."""
    launch = make_ranked(
        "launch",
        source_id="outlet",
        title="ChatGPT is getting a dedicated mode for teens",
        text=OUTLET_REPORT,
        score=88,
    )
    kept, collapsed = collapse_similar_events(
        [launch, one_other_story()],
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["launch", "appointment"]
    assert collapsed == []


def test_the_pass_is_deterministic_across_repeated_runs() -> None:
    """AC9: same stored inputs, same collapse, every time and in any input order."""
    ranked = [*three_outlets_on_one_launch(), one_other_story()]
    orders = (ranked, list(reversed(ranked)), [ranked[2], ranked[0], ranked[3], ranked[1]])
    outcomes = set()
    for order in orders:
        kept, collapsed = collapse_similar_events(
            order, threshold=SIMILAR_EVENT_THRESHOLD, min_score=SIMILAR_EVENT_MIN_SCORE
        )
        outcomes.add(
            (
                tuple(slug(item) for item in kept),
                tuple((slug(folded), slug(survivor)) for folded, survivor in collapsed),
            )
        )

    assert outcomes == {
        (
            ("vendor-post", "appointment"),
            (("outlet-report", "vendor-post"), ("rival-report", "vendor-post")),
        )
    }


def test_a_higher_threshold_keeps_every_report() -> None:
    """The knob is real: 1.0 demands identity, which two rewrites never reach."""
    kept, collapsed = collapse_similar_events(
        three_outlets_on_one_launch(), threshold=1.0, min_score=SIMILAR_EVENT_MIN_SCORE
    )
    assert len(kept) == 3
    assert collapsed == []


def test_a_stub_page_is_never_folded_into_anything() -> None:
    """A bot check or a paywall notice is not evidence about any story."""
    stub = make_ranked(
        "blocked",
        source_id="outlet",
        title="JavaScript is disabled",
        text="In order to continue, we need to verify that you are not a robot.",
        score=90,
    )
    twin = make_ranked(
        "blocked-too",
        source_id="rival",
        title="JavaScript is disabled",
        text="In order to continue, we need to verify that you are not a robot!",
        score=85,
    )
    kept, collapsed = collapse_similar_events(
        [stub, twin], threshold=SIMILAR_EVENT_THRESHOLD, min_score=SIMILAR_EVENT_MIN_SCORE
    )

    assert len(kept) == 2
    assert collapsed == []


def test_site_furniture_never_makes_two_stories_look_alike() -> None:
    """One outlet's masthead, repeated on every page, is not a shared story."""
    furniture = (
        "Subscribe to our daily newsletter and follow us everywhere. Latest stories, "
        "most popular stories, more from this author, sponsored content, advertisement."
    )
    stories = [
        make_ranked(
            f"story-{index}",
            source_id="outlet",
            title=f"Story number {index}",
            text=f"{furniture} {body}",
            score=90 - index,
        )
        for index, body in enumerate(
            (
                "A senate committee opened an investigation into a gaming platform "
                "over the safety of the children who play on it every afternoon.",
                "A studio announced four theme park attractions based on the animated "
                "films it released over the previous decade, opening in 2029.",
                "A chipmaker invested one and a half billion dollars in the data centre "
                "developer building the campus its newest accelerators will fill.",
            )
        )
    ]
    kept, collapsed = collapse_similar_events(
        stories, threshold=SIMILAR_EVENT_THRESHOLD, min_score=SIMILAR_EVENT_MIN_SCORE
    )

    assert len(kept) == 3
    assert collapsed == []


def test_an_empty_candidate_list_is_handled() -> None:
    assert collapse_similar_events(
        [], threshold=SIMILAR_EVENT_THRESHOLD, min_score=SIMILAR_EVENT_MIN_SCORE
    ) == ([], [])


# --------------------------------------------------------------------------- #
# the publication floor -- only what could be printed is ever folded
# --------------------------------------------------------------------------- #

#: A second near-duplicate pair, used on both sides of the floor so the floor
#: test cannot pass by accident on two articles that never matched anyway.
FIRST_ACCOUNT = """
A chipmaker will invest one and a half billion dollars in the data centre
developer building the campus its newest accelerators are meant to fill. The deal,
disclosed on Monday, hands the chipmaker a minority stake in the developer and
reserves capacity on the campus through 2029. Analysts read it as an attempt to
secure power and floor space ahead of a shortage rather than as a financial bet.
"""

SECOND_ACCOUNT = """
The data centre developer said on Monday that a chipmaker had committed one and a
half billion dollars to it, taking a minority stake and reserving campus capacity
through 2029. The developer is building the site its newest accelerators will
fill. Executives framed the money as a hedge against a shortage of power and floor
space rather than as an ordinary financial investment.
"""


def two_accounts_of_one_investment(*, scores: tuple[int, int]) -> list[RankedArticle]:
    return [
        make_ranked(
            "first-account",
            source_id="outlet",
            title="A chipmaker puts $1.5B into a data centre developer",
            text=FIRST_ACCOUNT,
            score=scores[0],
        ),
        make_ranked(
            "second-account",
            source_id="rival",
            title="Data centre developer takes $1.5B from a chipmaker",
            text=SECOND_ACCOUNT,
            score=scores[1],
        ),
    ]


def test_a_near_duplicate_pair_below_the_floor_is_left_alone() -> None:
    """Nothing under ``min_score`` can print, so folding it can only be wrong.

    This is where every false positive measured on the real 2026-W34 edition
    lived -- unrelated articles pushed together by their site's furniture, none of
    them publishable. They are out of scope by construction now, not by luck.
    """
    below = (SIMILAR_EVENT_MIN_SCORE - 1, SIMILAR_EVENT_MIN_SCORE - 6)
    kept, collapsed = collapse_similar_events(
        two_accounts_of_one_investment(scores=below),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["first-account", "second-account"]
    assert collapsed == []


def test_the_same_pair_above_the_floor_is_collapsed() -> None:
    """The other half of the previous test: these two really are one event."""
    above = (SIMILAR_EVENT_MIN_SCORE + 4, SIMILAR_EVENT_MIN_SCORE + 1)
    kept, collapsed = collapse_similar_events(
        two_accounts_of_one_investment(scores=above),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["first-account"]
    assert [(slug(folded), slug(survivor)) for folded, survivor in collapsed] == [
        ("second-account", "first-account")
    ]


def test_a_trio_above_the_floor_still_becomes_one_story() -> None:
    """The floor narrows the scope of the pass; it does not blunt it."""
    kept, collapsed = collapse_similar_events(
        three_outlets_on_one_launch(),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["vendor-post"]
    assert [slug(survivor) for _, survivor in collapsed] == ["vendor-post", "vendor-post"]


def test_a_story_below_the_floor_never_absorbs_a_publishable_one() -> None:
    """Every survivor is publishable, so a fold can never cost the edition a story."""
    straddling = (SIMILAR_EVENT_MIN_SCORE - 20, SIMILAR_EVENT_MIN_SCORE + 5)
    kept, collapsed = collapse_similar_events(
        two_accounts_of_one_investment(scores=straddling),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == ["second-account", "first-account"]
    assert collapsed == []


def test_the_floor_scopes_the_fold_and_not_the_term_statistics() -> None:
    """Chrome is measured over the run's whole corpus, not the publishable part.

    Three articles from one outlet share its "latest stories" rail, and only two
    of them clear the floor. Two articles are too few to measure chrome against
    (:data:`MIN_SOURCE_ARTICLES_FOR_CHROME`), so a pass that narrowed its term
    statistics to the publishable subset would leave the rail in both vectors and
    fold two unrelated stories together. On the real 2026-W34 pool that mistake
    cost two published stories, so it is pinned here.
    """
    furniture = (
        "Subscribe to our daily newsletter and follow us everywhere. Latest stories, "
        "most popular stories, more from this author, sponsored content, advertisement."
    )
    bodies = (
        "A senate committee opened an investigation into a gaming platform "
        "over the safety of the children who play on it every afternoon.",
        "A studio announced four theme park attractions based on the animated "
        "films it released over the previous decade, opening in 2029.",
        "A chipmaker invested one and a half billion dollars in the data centre "
        "developer building the campus its newest accelerators will fill.",
    )
    scores = (
        SIMILAR_EVENT_MIN_SCORE + 5,
        SIMILAR_EVENT_MIN_SCORE + 1,
        SIMILAR_EVENT_MIN_SCORE - 10,
    )
    stories = [
        make_ranked(
            f"story-{index}",
            source_id="outlet",
            title=f"Story number {index}",
            text=f"{furniture} {body}",
            score=score,
        )
        for index, (body, score) in enumerate(zip(bodies, scores, strict=True))
    ]
    kept, collapsed = collapse_similar_events(
        stories, threshold=SIMILAR_EVENT_THRESHOLD, min_score=SIMILAR_EVENT_MIN_SCORE
    )

    assert len(kept) == 3
    assert collapsed == []


def test_the_floor_is_deterministic_in_any_input_order() -> None:
    """AC9 over a mixed pool: repeated, reversed and shuffled give one outcome."""
    below = (SIMILAR_EVENT_MIN_SCORE - 1, SIMILAR_EVENT_MIN_SCORE - 6)
    ranked = [
        *three_outlets_on_one_launch(),
        one_other_story(),
        *two_accounts_of_one_investment(scores=below),
    ]
    orders = (
        ranked,
        list(reversed(ranked)),
        [ranked[4], ranked[1], ranked[5], ranked[0], ranked[3], ranked[2]],
    )
    outcomes = set()
    for order in orders:
        kept, collapsed = collapse_similar_events(
            order, threshold=SIMILAR_EVENT_THRESHOLD, min_score=SIMILAR_EVENT_MIN_SCORE
        )
        outcomes.add(
            (
                tuple(slug(item) for item in kept),
                tuple((slug(folded), slug(survivor)) for folded, survivor in collapsed),
            )
        )

    assert outcomes == {
        (
            ("vendor-post", "appointment", "first-account", "second-account"),
            (("outlet-report", "vendor-post"), ("rival-report", "vendor-post")),
        )
    }
