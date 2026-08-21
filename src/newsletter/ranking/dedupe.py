"""Deterministic deduplication.

Runs *before* any model call, because the cheapest assessment is the one never
requested. Three passes, cheapest and most certain first:

1. canonical URL comparison key -- the same page reached two ways;
2. content hash -- identical text republished at a different URL (syndication);
3. normalized title -- the same story rewritten with the same headline.

Which copy survives is decided by rule, never by chance: the reserved-slot source
first when one is named, then highest source priority, then earliest publication,
then lowest article id. Two runs over the same inputs always keep the same copy.

Semantic collapse of *different* stories about the same event runs later in the
pipeline, after analysis and scoring (PRD section 22), in two passes:
:func:`collapse_duplicate_events` on the analyzer event fingerprint, then
:func:`collapse_similar_events` on the article text itself. The first is an exact
key and runs over every candidate; the second is a judgement about text and folds
only candidates that could actually be published.

:class:`PublishedKeys` carries the first two identity keys -- not the title --
across editions, so a story printed last week is not printed again this week.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from newsletter.logging_setup import get_logger
from newsletter.models import ArticleAssessment, NormalizedArticle, RankedArticle
from newsletter.normalization.urls import dedupe_key

logger = get_logger("dedupe")

_NON_ALPHANUMERIC = re.compile(r"[^\w\s]", re.UNICODE)

#: A leading article carries no identity: "the ChatGPT for Teens" names the same
#: thing as "ChatGPT for Teens".
_LEADING_ARTICLES = frozenset({"a", "an", "the"})

#: Below this length a normalized title is too generic to be evidence of a duplicate.
MIN_TITLE_KEY_LENGTH = 15

REASON_URL = "duplicate_url"
REASON_CONTENT = "duplicate_content"
REASON_TITLE = "duplicate_title"


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-free, whitespace-collapsed comparison key."""
    stripped = _NON_ALPHANUMERIC.sub(" ", title.lower())
    return " ".join(stripped.split())


def normalize_entity(value: str) -> str:
    """Hard comparison key for one event field.

    Case-folds, strips accents, drops punctuation, collapses internal whitespace
    and removes leading articles, so ``"The ChatGPT-for-Teens"`` and
    ``"chatgpt for teens"`` are recognisably the same thing. Returns ``""`` for
    anything that normalizes away to nothing.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    unaccented = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = _NON_ALPHANUMERIC.sub(" ", unaccented.casefold()).split()
    while words and words[0] in _LEADING_ARTICLES:
        words.pop(0)
    return " ".join(words)


@dataclass(frozen=True)
class DroppedArticle:
    """One discarded duplicate, with the reason and the copy that survived."""

    article: NormalizedArticle
    reason: str
    kept_article_id: str


@dataclass
class DedupeResult:
    kept: list[NormalizedArticle] = field(default_factory=list)
    dropped: list[DroppedArticle] = field(default_factory=list)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.dropped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def _preference_key(
    article: NormalizedArticle, priorities: Mapping[str, int], preferred: str | None
) -> tuple[int, int, str, str]:
    """Best copy first: the preferred source, then highest priority, then earliest, then id."""
    return (
        0 if preferred is not None and article.source_id == preferred else 1,
        -priorities.get(article.source_id, 0),
        article.published_at.isoformat(),
        article.article_id,
    )


def deduplicate(
    articles: Iterable[NormalizedArticle],
    *,
    priorities: Mapping[str, int] | None = None,
    preferred_source_id: str | None = None,
) -> DedupeResult:
    """Collapse duplicates deterministically.

    ``priorities`` maps source id to its configured priority; a source missing
    from the mapping is treated as priority 0.

    ``preferred_source_id`` names a source whose copy wins a collision outright,
    whatever its priority. It exists for reader submissions while slots are
    reserved for them: when a reader's link turns out to be the same page a
    configured source also carries, keeping the reader's copy is what keeps the
    reserved slot real -- the source's copy would have to earn its place on score
    instead, and could lose it. ``None`` leaves the ordering exactly as priority
    alone decides.
    """
    ranking = priorities or {}
    ordered = sorted(articles, key=lambda a: _preference_key(a, ranking, preferred_source_id))

    result = DedupeResult()
    by_url: dict[str, str] = {}
    by_content: dict[str, str] = {}
    by_title: dict[str, str] = {}

    for article in ordered:
        url_key = dedupe_key(article.canonical_url)
        title_key = normalize_title(article.title)

        winner = by_url.get(url_key)
        reason = REASON_URL
        if winner is None:
            winner = by_content.get(article.content_hash)
            reason = REASON_CONTENT
        if winner is None and len(title_key) >= MIN_TITLE_KEY_LENGTH:
            winner = by_title.get(title_key)
            reason = REASON_TITLE

        if winner is not None:
            result.dropped.append(
                DroppedArticle(article=article, reason=reason, kept_article_id=winner)
            )
            continue

        by_url[url_key] = article.article_id
        by_content[article.content_hash] = article.article_id
        if len(title_key) >= MIN_TITLE_KEY_LENGTH:
            by_title[title_key] = article.article_id
        result.kept.append(article)

    if result.dropped:
        logger.info(
            "deduplication: kept %d, dropped %d %s",
            len(result.kept),
            result.dropped_count,
            result.reasons(),
        )
    return result


# --------------------------------------------------------------------------- #
# semantic collapse — after analysis, using the structured event fingerprint
# --------------------------------------------------------------------------- #


def event_collapse_key(assessment: ArticleAssessment) -> str | None:
    """``subject|object`` for within-run collapse, or None when underspecified.

    Deliberately narrower than :meth:`ArticleAssessment.event_fingerprint`, which
    keeps its full four-part identity for anything that needs it. Action and date
    are dropped here because that is precisely where two accounts of one
    announcement diverge: one outlet writes "launches", the next "introduces" and
    the third "announces", and the date is stated in one report and left null in
    another. Keying on the two stable halves -- who, and what -- is what stops one
    launch running as three separate stories in a single edition.

    Both halves are required. An event missing either is kept, because an unknown
    event is not evidence of a duplicate.
    """
    subject = normalize_entity(assessment.event_subject or "")
    obj = normalize_entity(assessment.event_object or "")
    if not subject or not obj:
        return None
    return f"{subject}|{obj}"


def collapse_order(
    preferred_source_id: str | None,
) -> Callable[[RankedArticle], tuple[int, int, str, str]]:
    """Ranking order, with one source moved to the front of it.

    The collapse passes keep whichever copy they visit first, so this is how a
    reserved reader submission survives a collision with a configured source's
    account of the same event instead of folding into it. ``None`` is plain
    ranking order, which is what every caller uses when nothing is reserved.
    """
    from newsletter.ranking.scoring import ranking_key

    def key(item: RankedArticle) -> tuple[int, int, str, str]:
        preferred = (
            preferred_source_id is not None and item.article.source_id == preferred_source_id
        )
        return (0 if preferred else 1, *ranking_key(item))

    return key


def collapse_duplicate_events(
    ranked: Iterable[RankedArticle],
    *,
    preferred_source_id: str | None = None,
) -> tuple[list[RankedArticle], list[RankedArticle]]:
    """Collapse different articles that describe the same event.

    Two outlets covering one announcement name the same subject and the same
    object even when their wording, URLs and content hashes differ, so the
    deterministic passes above cannot see the duplication. The analyzer supplies
    that structured fingerprint; the choice of which copy survives is still made
    here, by rule: highest score, then earliest publication, then lowest article
    id.

    An article whose event is incomplete is always kept -- an unknown event is not
    evidence of a duplicate. Returns ``(kept, collapsed)``, ``kept`` in ranking
    order whatever ``preferred_source_id`` did to the visiting order.
    """
    from newsletter.ranking.scoring import ranking_key

    ordered = sorted(ranked, key=collapse_order(preferred_source_id))
    winners: dict[str, RankedArticle] = {}
    kept: list[RankedArticle] = []
    collapsed: list[RankedArticle] = []

    for article in ordered:
        fingerprint = event_collapse_key(article.assessment)
        if fingerprint is None:
            kept.append(article)
            continue
        if fingerprint in winners:
            collapsed.append(article)
            continue
        winners[fingerprint] = article
        kept.append(article)

    if collapsed:
        logger.info("event collapse: kept %d, dropped %d", len(kept), len(collapsed))
    kept.sort(key=ranking_key)
    return kept, collapsed


# --------------------------------------------------------------------------- #
# content similarity — the second collapse pass, within one run only
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"[a-z0-9]+")

#: A term printed on at least half of one source's articles is site furniture --
#: the masthead, the navigation, the byline template, the "latest stories" rail --
#: not evidence about *this* story. Measured per source and per run, because that
#: is the only place the chrome is visible: it is what a source repeats and its
#: stories do not. Without this, two unrelated articles from one outlet look more
#: alike than two outlets covering the same launch, which is exactly backwards.
SOURCE_CHROME_RATIO = 0.5

#: Below this many articles a source has no measurable chrome, so nothing is
#: removed: with two articles, "at least half" would delete every shared word.
MIN_SOURCE_ARTICLES_FOR_CHROME = 3

#: Below this many words an article is a stub -- a paywall notice, a bot check, a
#: one-line brief -- and carries too little text to be evidence of anything. Stubs
#: are kept, never collapsed.
MIN_SIMILARITY_WORDS = 40


def _content_terms(article: NormalizedArticle) -> list[str]:
    """Lowercased word tokens of the title and body, in reading order."""
    return _WORD.findall(f"{article.title}\n{article.clean_text}".casefold())


def similarity_profiles(articles: Sequence[NormalizedArticle]) -> list[dict[str, float]]:
    """One L2-normalized TF-IDF vector per article, in the order given.

    The representation is deliberately plain: single words, weighted by
    ``(1 + log tf) * idf``, cosine-compared. Three outlets covering one launch
    share their vocabulary and their proper names but almost none of their
    phrasing, so anything that keys on verbatim runs of words -- shingles, n-gram
    hashes -- under-matches them, while raw word overlap over-matches everything
    that is merely about the same industry. Inverse document frequency is the part
    that discriminates: "the", "ai" and "company" appear everywhere and count for
    nothing, while "teens", "parental" and "safeguards" appear in a handful of
    articles and count for a great deal. Rare terms and proper names are, in
    practice, what two reports of one event actually have in common.

    Document frequency is computed over the articles passed in -- the run's own
    candidates -- so the same stored inputs always produce the same weights (AC9).
    Terms are inserted in sorted order so every dot product is summed in the same
    sequence on every run, and set iteration never reaches a result.

    An article with no usable text gets an empty profile, which never matches.
    """
    counted = [Counter(_content_terms(article)) for article in articles]
    total = len(articles)

    document_frequency: Counter[str] = Counter()
    for counts in counted:
        document_frequency.update(counts.keys())

    per_source_articles: Counter[str] = Counter(article.source_id for article in articles)
    per_source_frequency: dict[str, Counter[str]] = defaultdict(Counter)
    for article, counts in zip(articles, counted, strict=True):
        per_source_frequency[article.source_id].update(counts.keys())

    chrome: dict[str, frozenset[str]] = {}
    for source_id, published in per_source_articles.items():
        if published < MIN_SOURCE_ARTICLES_FOR_CHROME:
            chrome[source_id] = frozenset()
            continue
        chrome[source_id] = frozenset(
            term
            for term, seen in per_source_frequency[source_id].items()
            if seen >= published * SOURCE_CHROME_RATIO
        )

    profiles: list[dict[str, float]] = []
    for article, counts in zip(articles, counted, strict=True):
        if sum(counts.values()) < MIN_SIMILARITY_WORDS:
            profiles.append({})
            continue
        furniture = chrome[article.source_id]
        weights: dict[str, float] = {}
        for term in sorted(counts):
            if term in furniture:
                continue
            idf = math.log((total + 1) / (document_frequency[term] + 1)) + 1.0
            weights[term] = (1.0 + math.log(counts[term])) * idf
        norm = math.sqrt(sum(weight * weight for weight in weights.values()))
        profiles.append({term: weight / norm for term, weight in weights.items()} if norm else {})
    return profiles


def content_similarity(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    """Cosine similarity of two normalized profiles, in ``[0, 1]``."""
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(term, 0.0) for term, weight in left.items())


def collapse_similar_events(
    ranked: Iterable[RankedArticle],
    *,
    threshold: float,
    min_score: int,
    preferred_source_id: str | None = None,
) -> tuple[list[RankedArticle], list[tuple[RankedArticle, RankedArticle]]]:
    """Collapse articles that read like the same event, on their text alone.

    The pass exists because the exact-key collapse above provably cannot catch the
    case that prompted it: three outlets reporting one launch produce three
    independent analyzer calls with no cross-article consistency mechanism, so one
    writes subject "openai" and object "chatgpt for teens" while the next writes
    "chatgpt" and "teen accounts". Those keys will never be equal. The articles,
    however, are made of the same rare words.

    Runs **within one run only**, never across editions. Over-collapsing inside a
    week costs one story out of that week's line-up; across weeks it would bury
    every follow-up on a story permanently, which is the trade
    :class:`PublishedKeys` already refuses to make.

    **Only a publishable article is ever folded.** ``min_score`` is the edition's
    publication floor, and a candidate below it cannot reach the page whatever
    this pass decides, so collapsing it changes nothing and can only be wrong.
    Restricting the fold to articles at or above the floor is what makes the
    known false positives -- unrelated stories that share a page's furniture --
    structurally unable to affect an edition rather than merely unlucky enough not
    to have (ADR-0034).

    The *statistics* are still measured over every candidate passed in, not over
    the publishable subset. Inverse document frequency and the per-source chrome
    rule are properties of the run's whole corpus and are only observable there:
    an outlet with two publishable stories this week has no measurable chrome, and
    dropping its other eight articles from the count leaves its "latest stories"
    rail in the vector, where it makes unrelated pairs look like the same event.
    Scope the fold, not the evidence.

    Greedy and ordered: candidates are visited best first, and a candidate joins
    the first already-surviving story it resembles, so the survivor is always the
    higher-ranked one. Because the order is score-descending, every eligible
    article is visited before every ineligible one, and a sub-threshold article
    never becomes a survivor that could absorb a publishable story. No model call,
    no randomness, no clock. Returns ``(kept, collapsed)``, where each collapsed
    entry is ``(folded, survivor)`` and ``kept`` is in ranking order.

    ``preferred_source_id`` is the one exception to both rules above, and it is
    the reserved-slot source. Its candidates are visited first and are eligible
    whatever they scored, because a reserved submission *is* publishable -- the
    floor does not apply to it -- so leaving it out of the pass would let a
    reader's link and a source's account of the same event both print. It
    survives the fold rather than joining it, for the reason
    :func:`deduplicate` keeps the reader's copy: the slot belongs to the
    submission.
    """
    from newsletter.ranking.scoring import ranking_key

    ordered = sorted(ranked, key=collapse_order(preferred_source_id))
    profiles = similarity_profiles([item.article for item in ordered])

    kept: list[RankedArticle] = []
    collapsed: list[tuple[RankedArticle, RankedArticle]] = []
    survivors: list[tuple[RankedArticle, Mapping[str, float]]] = []

    for article, profile in zip(ordered, profiles, strict=True):
        reserved = (
            preferred_source_id is not None and article.article.source_id == preferred_source_id
        )
        if article.final_score < min_score and not reserved:
            kept.append(article)
            continue
        survivor = None
        if profile:
            for candidate, candidate_profile in survivors:
                if content_similarity(profile, candidate_profile) >= threshold:
                    survivor = candidate
                    break
        if survivor is not None:
            collapsed.append((article, survivor))
            continue
        survivors.append((article, profile))
        kept.append(article)

    if collapsed:
        logger.info("similarity collapse: kept %d, dropped %d", len(kept), len(collapsed))
    kept.sort(key=ranking_key)
    return kept, collapsed


# --------------------------------------------------------------------------- #
# across editions — "every news should be posted only once"
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PublishedKeys:
    """Identity keys of the stories previous editions already printed.

    Identity **only**, and only the two keys that cannot be wrong: the article id
    and the content hash. Deliberately *not* the event subject and object, and --
    unlike :func:`deduplicate` -- deliberately not the normalized title either.

    Suppressing a subject across editions would read as the same guarantee and
    behave like censorship: "OpenAI / ChatGPT for teens" can legitimately be a
    launch this week and a regulatory fight next month, and a topic-level block
    would silently kill the second story forever, with no evidence in the edition
    that anything was withheld. So the durable promise is narrow and defensible --
    *this exact article* is printed once -- and a genuine follow-up still competes
    on its merits.

    The title key is dropped here for the same reason, one step weaker. Inside one
    run it is good evidence and cheap to be wrong about: two stories sharing a
    headline in the same week almost certainly *are* one story, and the cost of a
    mistake is one story in one edition. Across editions the same key carries a
    permanent consequence, and headlines repeat on a recurring beat -- "YouTube
    changes its monetization rules" is a plausible headline in March and again in
    September. Blocking the second one forever, invisibly, is not a price worth
    paying for a key that only guesses. An identical article id or an identical
    content hash is not a guess.

    Each mapping points at the issue label that printed the story first, so a
    suppressed candidate can explain itself ("already published in 2026-W33").
    Empty mappings mean nothing has been published yet and nothing is suppressed.
    """

    by_article_id: Mapping[str, str] = field(default_factory=dict)
    by_content_hash: Mapping[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_article_id or self.by_content_hash)

    def issue_for(self, article: NormalizedArticle) -> str | None:
        """The issue that already printed this article, or None. Cheapest key first."""
        issue = self.by_article_id.get(article.article_id)
        if issue is not None:
            return issue
        return self.by_content_hash.get(article.content_hash)
