"""Deterministic deduplication before any model call."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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


#: One row per way two records can turn out to be the same story -- and the two
#: ways they can look alike without being one, which is where a pass over-reaches.
DEDUPE_CASES = {
    "same page reached two ways": (
        lambda: [
            make_article("https://wire.example/story"),
            make_article("https://www.wire.example/story/?utm_source=rss"),
        ],
        1,
        [REASON_URL],
    ),
    "syndicated copy, identical text": (
        lambda: [
            make_article("https://wire.example/story", source_id="wire"),
            make_article(
                "https://aggregator.example/reprint",
                source_id="aggregator",
                title="A different headline",
            ),
        ],
        1,
        [REASON_CONTENT],
    ),
    "same headline, rewritten body": (
        lambda: [
            make_article(
                "https://wire.example/story", text="One version of the article body here."
            ),
            make_article(
                "https://blog.example/story",
                source_id="blog",
                text="A completely different rewrite of the same news event, worded differently.",
            ),
        ],
        1,
        [REASON_TITLE],
    ),
    "three distinct stories": (
        lambda: [
            make_article(f"https://wire.example/{letter}", title=title, text=text)
            for letter, title, text in (
                ("a", "First distinct headline here", "AAA aaa"),
                ("b", "Second distinct headline here", "BBB bbb"),
                ("c", "Third distinct headline here", "CCC ccc"),
            )
        ],
        3,
        [],
    ),
    # A generic short headline is not evidence of the same story.
    "one short generic headline, twice": (
        lambda: [
            make_article("https://wire.example/a", title="AI news", text="First body text here."),
            make_article(
                "https://blog.example/b", source_id="blog", title="AI news", text="Other body."
            ),
        ],
        2,
        [],
    ),
}


@pytest.mark.parametrize(
    ("build", "expected_kept", "expected_reasons"),
    list(DEDUPE_CASES.values()),
    ids=list(DEDUPE_CASES),
)
def test_each_pass_collapses_only_what_it_is_for(build, expected_kept, expected_reasons) -> None:
    result = deduplicate(build(), priorities=PRIORITIES)

    assert len(result.kept) == expected_kept
    assert [dropped.reason for dropped in result.dropped] == expected_reasons
    kept_ids = {article.article_id for article in result.kept}
    assert all(dropped.kept_article_id in kept_ids for dropped in result.dropped)


# --------------------------------------------------------------------------- #
# which copy survives — by rule, not by chance
# --------------------------------------------------------------------------- #


#: The survivor is chosen by rule, in this order. One row per rung of the ladder.
SURVIVOR_CASES = {
    "highest source priority": (
        lambda: [
            make_article("https://aggregator.example/x", source_id="aggregator"),
            make_article("https://wire.example/y", source_id="wire"),
        ],
        "https://wire.example/y",
    ),
    "an unlisted source counts as priority zero": (
        lambda: [
            make_article("https://unknown.example/y", source_id="unknown"),
            make_article("https://wire.example/x", source_id="wire"),
        ],
        "https://wire.example/x",
    ),
    "earliest publication breaks a priority tie": (
        lambda: [
            make_article("https://wire.example/x", published_at=LATE),
            make_article("https://wire.example/y", published_at=EARLY),
        ],
        "https://wire.example/y",
    ),
}


@pytest.mark.parametrize(
    ("build", "expected_url"), list(SURVIVOR_CASES.values()), ids=list(SURVIVOR_CASES)
)
def test_the_surviving_copy_is_chosen_by_rule(build, expected_url: str) -> None:
    result = deduplicate(build(), priorities=PRIORITIES)

    assert [article.canonical_url for article in result.kept] == [expected_url]


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
    """The verb and the date differ across the three rows: neither is part of the key."""
    keys = {event_collapse_key(make_assessment(*event)) for event in CHATGPT_FOR_TEENS}
    assert keys == {"openai|chatgpt for teens"}


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


PUBLISHED_ARTICLE = make_article("https://wire.example/story")
#: Same body, new address, new headline: only the content hash can recognise it.
SYNDICATED_COPY = make_article("https://blog.example/reprint", title="A completely other headline")

IDENTITY_CASES = {
    "a fresh database suppresses nothing": (PublishedKeys(), PUBLISHED_ARTICLE, None),
    "recognised by its article id": (
        PublishedKeys(by_article_id={PUBLISHED_ARTICLE.article_id: "2026-W33"}),
        PUBLISHED_ARTICLE,
        "2026-W33",
    ),
    "recognised by its content hash at a new url": (
        PublishedKeys(by_content_hash={PUBLISHED_ARTICLE.content_hash: "2026-W30"}),
        SYNDICATED_COPY,
        "2026-W30",
    ),
}


@pytest.mark.parametrize(
    ("keys", "article", "expected_issue"), list(IDENTITY_CASES.values()), ids=list(IDENTITY_CASES)
)
def test_suppression_recognises_a_story_by_identity(
    keys: PublishedKeys, article: NormalizedArticle, expected_issue: str | None
) -> None:
    assert keys.issue_for(article) == expected_issue
    assert bool(keys) is (expected_issue is not None)


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


@pytest.mark.parametrize(
    ("threshold", "expected_kept", "expected_collapsed"),
    [
        pytest.param(
            SIMILAR_EVENT_THRESHOLD,
            ["vendor-post"],
            [("outlet-report", "vendor-post"), ("rival-report", "vendor-post")],
            id="the shipped threshold folds one launch into one story",
        ),
        pytest.param(
            1.0,
            ["vendor-post", "outlet-report", "rival-report"],
            [],
            id="1.0 demands identity, which two rewrites never reach",
        ),
    ],
)
def test_three_outlets_on_one_launch_become_one_story(
    threshold: float, expected_kept: list[str], expected_collapsed: list[tuple[str, str]]
) -> None:
    """The owner's complaint, reproduced: three of eight stories, one event."""
    kept, collapsed = collapse_similar_events(
        three_outlets_on_one_launch(),
        threshold=threshold,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == expected_kept
    assert [(slug(folded), slug(survivor)) for folded, survivor in collapsed] == expected_collapsed


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


#: One near-duplicate pair, moved across the floor. Using the same pair on both
#: sides is what stops the below-floor rows passing by accident on two articles
#: that never matched anyway.
FLOOR_CASES = {
    # Every false positive measured on the real 2026-W34 edition lived here --
    # unrelated articles pushed together by their site's furniture, none of them
    # publishable. Out of scope by construction now, not by luck.
    "both one point below the floor: nothing to protect, so nothing is folded": (
        (SIMILAR_EVENT_MIN_SCORE - 1, SIMILAR_EVENT_MIN_SCORE - 6),
        ["first-account", "second-account"],
        [],
    ),
    "both above the floor: these two really are one event": (
        (SIMILAR_EVENT_MIN_SCORE + 4, SIMILAR_EVENT_MIN_SCORE + 1),
        ["first-account"],
        [("second-account", "first-account")],
    ),
    # Every survivor is publishable, so a fold can never cost the edition a story.
    "straddling the floor: the weaker one never absorbs the publishable one": (
        (SIMILAR_EVENT_MIN_SCORE - 20, SIMILAR_EVENT_MIN_SCORE + 5),
        ["second-account", "first-account"],
        [],
    ),
}


@pytest.mark.parametrize(
    ("scores", "expected_kept", "expected_collapsed"),
    list(FLOOR_CASES.values()),
    ids=list(FLOOR_CASES),
)
def test_only_what_could_be_printed_is_ever_folded(
    scores: tuple[int, int],
    expected_kept: list[str],
    expected_collapsed: list[tuple[str, str]],
) -> None:
    kept, collapsed = collapse_similar_events(
        two_accounts_of_one_investment(scores=scores),
        threshold=SIMILAR_EVENT_THRESHOLD,
        min_score=SIMILAR_EVENT_MIN_SCORE,
    )

    assert [slug(item) for item in kept] == expected_kept
    assert [(slug(folded), slug(survivor)) for folded, survivor in collapsed] == expected_collapsed


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


def test_the_pass_is_deterministic_in_any_input_order() -> None:
    """AC9 over a mixed pool: repeated, reversed and shuffled give one outcome.

    The pool spans everything the pass decides -- a trio that folds, an unrelated
    story that must not, and a pair under the floor -- so the pinned outcome is
    also what proves the survivor is the best of a group and not the first seen.
    """
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
