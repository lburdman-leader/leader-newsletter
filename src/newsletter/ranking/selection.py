"""Deterministic story selection.

Given the same scored articles and the same configuration, this module always
produces the same line-up in the same order (AC9). Nothing here consults a model,
a clock or a random source.

The rules, applied in order to articles sorted best-first:

1. drop anything a previous edition already printed;
2. collapse several articles covering one event into the best of them, first on
   the analyzer fingerprint and then -- for publishable candidates only -- on the
   article text;
3. seat the reserved slots: reader submissions take their places before anything
   is earned (see :func:`reserve`);
4. seat the coverage floors, so the edition always carries the beat it is
   published for (see :func:`fill_floors`);
5. drop excluded categories (``other`` by default);
6. drop anything below ``min_score``;
7. respect the per-category cap, so one topic cannot monopolise the edition;
8. respect the per-source cap, so one publication cannot either;
9. respect the per-subject cap, so one company cannot either;
10. stop at ``max_items``.

Every rejection is recorded with its reason, and the reasons that need one carry
a free-text detail as well. Those same reasons also reach the run manifest, so an
empty or thin edition can be explained from the artifact instead of guessed at.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from newsletter.config import NewsletterSettings
from newsletter.logging_setup import get_logger
from newsletter.models import RankedArticle, RunManifest, TopicCategory
from newsletter.ranking.dedupe import (
    PublishedKeys,
    collapse_duplicate_events,
    collapse_similar_events,
    normalize_entity,
)
from newsletter.ranking.scoring import ranking_key

logger = get_logger("selection")

REASON_EXCLUDED_CATEGORY = "category_excluded"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_CATEGORY_LIMIT = "category_limit"
REASON_SOURCE_LIMIT = "source_limit"
REASON_SUBJECT_LIMIT = "subject_limit"
REASON_MAX_ITEMS = "max_items"
REASON_DUPLICATE_EVENT = "duplicate_event"
REASON_SIMILAR_EVENT = "similar_event"
REASON_ALREADY_PUBLISHED = "already_published"
#: A story the rubric had earned that a coverage floor took the slot of. Recorded
#: because a floor deliberately seats a *lower*-scoring story, and an operator who
#: cannot see that happen cannot tell an editorial rule from a scoring bug.
REASON_COVERAGE_FLOOR = "coverage_floor"
#: Not applied by :func:`select`. The entity-fidelity guard runs after selection
#: and records its drops here, so ``reasons()`` still explains a thin edition.
REASON_UNSUPPORTED_ENTITY = "unsupported_entity"

#: Rejections that withhold one identified story for one specific circumstance,
#: and therefore belong in the run manifest rather than only in a log line. The
#: rest are policy arithmetic anyone can re-derive from the counts and the config:
#: a category was excluded, a score was too low, a cap or ``max_items`` was full.
MANIFEST_REASONS = (
    REASON_ALREADY_PUBLISHED,
    REASON_SIMILAR_EVENT,
    REASON_SUBJECT_LIMIT,
    REASON_COVERAGE_FLOOR,
)

#: Marks a manifest record as a reader submission's. While slots are reserved,
#: *every* rejected submission is recorded, whatever the reason: a slot the reader
#: was promised and did not get is exactly the omission an operator has to be able
#: to see, and the arithmetic argument for leaving common reasons out does not
#: hold for a story that was meant to be guaranteed.
SUBMITTED_DETAIL = "reader submission"


def submitted_detail(detail: str | None) -> str:
    """``detail`` marked as a reader submission's, for the run manifest."""
    return f"{SUBMITTED_DETAIL}: {detail}" if detail else SUBMITTED_DETAIL


@dataclass(frozen=True)
class RejectedArticle:
    """A scored article that did not make the edition, and why.

    ``detail`` names the specific circumstance when the reason alone is not
    enough to act on -- which issue already printed the story, which subject hit
    its cap. A suppressed story that cannot explain itself is a silent failure.
    """

    ranked: RankedArticle
    reason: str
    detail: str | None = None


@dataclass
class SelectionResult:
    """The chosen line-up plus a full account of what was left out.

    ``selected`` is the printed order: the reserved slots first, then the stories
    the rubric earned, each group best-first. ``reserved`` holds the same objects
    as the first group, so a later stage can still tell a guaranteed story from an
    earned one after the two have been merged.
    """

    selected: list[RankedArticle] = field(default_factory=list)
    rejected: list[RejectedArticle] = field(default_factory=list)
    above_threshold: int = 0
    reserved: list[RankedArticle] = field(default_factory=list)
    #: Coverage floors the candidate pool could not fill: floor name -> how many
    #: stories short. Empty when every floor was met. Nothing is ever padded to
    #: empty it, so a short edition says so instead of inventing a story.
    floors_unmet: dict[str, int] = field(default_factory=dict)
    #: Whether the line-up reached ``max_items``.
    full: bool = False

    @property
    def reserved_ids(self) -> set[str]:
        """Article ids that hold a reserved slot rather than an earned one."""
        return {ranked.article.article_id for ranked in self.reserved}

    @property
    def is_complete(self) -> bool:
        """Nothing more to gain from assessing another candidate.

        The stopping rule for adaptive assessment, and deliberately *both*
        conditions: an edition that is full of stories from the wrong beat is not
        finished, and stopping on "ten of anything" is exactly how a bounded pool
        would make a coverage floor unsatisfiable.
        """
        return self.full and not self.floors_unmet

    @property
    def lead(self) -> RankedArticle | None:
        """The best story in the line-up. The editor may reword it, never replace it.

        Deliberately *not* ``selected[0]``: reserved slots are seated first, and
        having been submitted is not an argument for leading the edition. The lead
        is chosen by the same key everything else is ranked by, so a submission
        leads only when it genuinely out-scores the field. With nothing reserved,
        ``selected`` is already in ranking order and this is ``selected[0]``.
        """
        return min(self.selected, key=ranking_key) if self.selected else None

    @property
    def is_empty(self) -> bool:
        return not self.selected

    def sections(
        self, settings: NewsletterSettings
    ) -> list[tuple[TopicCategory, list[RankedArticle]]]:
        """Selected stories grouped by category, in configured publication order.

        The lead story is excluded: it is displayed on its own, and printing it
        twice would read as an editing mistake.
        """
        lead = self.lead
        grouped: list[tuple[TopicCategory, list[RankedArticle]]] = []
        for category in settings.ordered_categories():
            items = [
                ranked
                for ranked in self.selected
                if ranked.assessment.category is category and ranked is not lead
            ]
            if items:
                grouped.append((category, items))
        return grouped

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts

    def rejections_for(self, reason: str) -> list[RejectedArticle]:
        """Every rejection carrying ``reason``, in selection order."""
        return [item for item in self.rejected if item.reason == reason]


def reserve(
    candidates: Sequence[RankedArticle],
    settings: NewsletterSettings,
    *,
    source_id: str | None,
    slots: int | None,
    excluded: set[TopicCategory],
) -> list[RankedArticle]:
    """The stories that hold a slot by right rather than by score.

    Reader submissions are the edition's first call on its slots: the reader
    asked for the link, so it does not have to out-score anything to be printed.
    What a reserved slot bypasses is exactly the machinery that *rations* slots
    between competing stories -- ``min_score``, ``max_per_source``,
    ``max_per_subject`` and the section limits -- and nothing else. Correctness is
    not rationing: duplicates, collapsed events, stories an earlier edition
    printed and the excluded categories are refused here as they are anywhere,
    which is why this runs after suppression and both collapse passes rather than
    before them.

    ``candidates`` arrives best-first, so the order in which submissions take the
    slots is the ranking order -- score descending, then earliest publication,
    then article id -- never insertion order and never set iteration (AC9). When
    submissions outnumber the slots, that same order decides which ones are
    seated and ``max_items`` still bounds the edition.
    """
    if source_id is None or slots == 0:
        return []

    available = settings.max_items if slots is None else min(slots, settings.max_items)
    reserved: list[RankedArticle] = []
    for article in candidates:
        if len(reserved) >= available:
            break
        if article.article.source_id != source_id:
            continue
        if article.assessment.category in excluded:
            continue  # rejected below, with its reason, like any other article
        reserved.append(article)
    return reserved


def fill_floors(
    candidates: Sequence[RankedArticle],
    settings: NewsletterSettings,
    *,
    reserved: Sequence[RankedArticle],
) -> tuple[list[tuple[str, RankedArticle]], dict[str, int]]:
    """The stories a coverage floor seats, and what each floor is still short of.

    A floor is the mirror image of ``section_limits``: it holds a group of
    categories up from below instead of capping one from above, so an edition
    published for a YouTube and children's-content business always carries that
    beat even in a week dominated by general AI news.

    It is seated after the reserved slots and before the earned ones, which is
    the whole mechanism: a qualifying story takes its place ahead of a
    higher-scoring story from another beat, without any of the machinery around
    it moving. A floor bypasses **nothing**. It obeys ``min_score`` -- reaching a
    minimum is not a reason to print something the rubric refused -- and it obeys
    the per-category, per-source and per-subject caps, so meeting one floor cannot
    unbalance the edition in some other direction. A reserved submission already
    in the group counts towards the minimum, so a guaranteed slot and a floor slot
    never double-count.

    When the pool genuinely has too few qualifying stories, the shortfall is
    returned rather than closed: nothing is padded, ``min_score`` is never
    lowered, and an excluded category is never admitted (configuration refuses a
    floor that names one).
    """
    if not settings.coverage_floors:
        return [], {}

    per_category: dict[TopicCategory, int] = {}
    per_source: dict[str, int] = {}
    per_subject: dict[str, int] = {}
    seated = list(reserved)
    for article in seated:
        _tally(article, per_category, per_source, per_subject)
    seated_ids = {article.article.article_id for article in seated}

    picks: list[tuple[str, RankedArticle]] = []
    unmet: dict[str, int] = {}
    for name, floor in settings.coverage_floors.items():
        group = set(floor.categories)
        have = sum(1 for article in seated if article.assessment.category in group)
        for article in candidates:
            if have >= floor.minimum or len(seated) >= settings.max_items:
                break
            if article.article.article_id in seated_ids:
                continue
            if article.assessment.category not in group:
                continue
            if article.final_score < settings.min_score:
                continue
            if _capped(article, settings, per_category, per_source, per_subject) is not None:
                continue
            picks.append((name, article))
            seated.append(article)
            seated_ids.add(article.article.article_id)
            _tally(article, per_category, per_source, per_subject)
            have += 1
        if have < floor.minimum:
            unmet[name] = floor.minimum - have
    return picks, unmet


def _capped(
    article: RankedArticle,
    settings: NewsletterSettings,
    per_category: dict[TopicCategory, int],
    per_source: dict[str, int],
    per_subject: dict[str, int],
) -> tuple[str, str | None] | None:
    """The rationing cap this story would breach, as ``(reason, detail)``.

    ``None`` when it breaches none. One implementation, used by both the earned
    pass and the coverage floors, so a floor can never quietly seat a story a cap
    would have refused.
    """
    category = article.assessment.category
    if per_category.get(category, 0) >= settings.limit_for(category):
        return (REASON_CATEGORY_LIMIT, None)

    source_id = article.article.source_id
    if (
        settings.max_per_source is not None
        and per_source.get(source_id, 0) >= settings.max_per_source
    ):
        return (REASON_SOURCE_LIMIT, None)

    # One company may hold several genuinely distinct stories in a week. The cap
    # keeps the edition heterogeneous anyway. An article whose analyst named no
    # subject is uncapped: an unknown subject is not evidence of dominance.
    subject = normalize_entity(article.assessment.event_subject or "")
    if (
        subject
        and settings.max_per_subject is not None
        and per_subject.get(subject, 0) >= settings.max_per_subject
    ):
        return (
            REASON_SUBJECT_LIMIT,
            f"{settings.max_per_subject} stories already cover {subject!r}",
        )
    return None


def _tally(
    article: RankedArticle,
    per_category: dict[TopicCategory, int],
    per_source: dict[str, int],
    per_subject: dict[str, int],
) -> None:
    """Count one selected story against the caps the rest of the edition obeys.

    A reserved story bypasses the caps for itself but still occupies them, so the
    seven earned slots below it are still spread across topics, sources and
    companies.
    """
    category = article.assessment.category
    per_category[category] = per_category.get(category, 0) + 1
    source_id = article.article.source_id
    per_source[source_id] = per_source.get(source_id, 0) + 1
    subject = normalize_entity(article.assessment.event_subject or "")
    if subject:
        per_subject[subject] = per_subject.get(subject, 0) + 1


def _seat_earned(
    candidates: Sequence[RankedArticle],
    settings: NewsletterSettings,
    *,
    preseated: Sequence[RankedArticle],
    excluded: set[TopicCategory],
    rejected: list[RejectedArticle] | None,
) -> tuple[list[RankedArticle], int]:
    """Fill the slots ``preseated`` leaves, and count what cleared the threshold.

    ``rejected`` collects the reasons; pass ``None`` to run the pass purely, which
    is how the cost of a coverage floor is derived without double-recording every
    rejection.
    """
    per_category: dict[TopicCategory, int] = {}
    per_source: dict[str, int] = {}
    per_subject: dict[str, int] = {}
    selected = list(preseated)
    for article in selected:
        _tally(article, per_category, per_source, per_subject)
    seated_ids = {article.article.article_id for article in selected}

    above_threshold = 0
    for article in candidates:
        if article.assessment.category in excluded:
            if rejected is not None:
                rejected.append(RejectedArticle(article, REASON_EXCLUDED_CATEGORY))
            continue

        # Counted before the seated stories are skipped, and before the floor
        # rejects anything: the figure answers "how many stories did the rubric
        # find worth printing this week?", which a guaranteed slot does not change
        # in either direction.
        if article.final_score >= settings.min_score:
            above_threshold += 1

        if article.article.article_id in seated_ids:
            continue  # already seated; no cap applies to it and nothing rejects it

        if article.final_score < settings.min_score:
            if rejected is not None:
                rejected.append(RejectedArticle(article, REASON_BELOW_THRESHOLD))
            continue

        if len(selected) >= settings.max_items:
            if rejected is not None:
                rejected.append(RejectedArticle(article, REASON_MAX_ITEMS))
            continue

        breach = _capped(article, settings, per_category, per_source, per_subject)
        if breach is not None:
            if rejected is not None:
                rejected.append(RejectedArticle(article, breach[0], breach[1]))
            continue

        _tally(article, per_category, per_source, per_subject)
        selected.append(article)
    return selected, above_threshold


def _record_floor_cost(
    result: SelectionResult,
    candidates: Sequence[RankedArticle],
    settings: NewsletterSettings,
    floor_seats: Sequence[tuple[str, RankedArticle]],
    *,
    excluded: set[TopicCategory],
) -> None:
    """Name the stories a coverage floor took the slots of, and which floor did.

    Derived rather than guessed: the edition that would have been printed with
    the floors switched off is computed from the same candidates, and whatever it
    contains that the real line-up does not is exactly what the floors cost. A
    floor that seated a story the rubric had reached anyway costs nothing, and
    this records nothing.

    The two lists are both best-first and the shorter one is the displaced one, so
    pairing them by position attributes each lost slot to the seat that took it.
    """
    without, _ = _seat_earned(
        candidates, settings, preseated=result.reserved, excluded=excluded, rejected=None
    )
    printed = {article.article.article_id for article in result.selected}
    displaced = [article for article in without if article.article.article_id not in printed]

    first_rejection: dict[str, int] = {}
    for index, item in enumerate(result.rejected):
        first_rejection.setdefault(item.ranked.article.article_id, index)

    for position, article in enumerate(displaced):
        name, seat = floor_seats[min(position, len(floor_seats) - 1)]
        # Replaces the reason the earned pass gave -- "the edition was full" is
        # true but useless when what filled it was an editorial rule.
        replacement = RejectedArticle(
            article,
            REASON_COVERAGE_FLOOR,
            f"slot taken by {seat.article.title!r} to meet the {name!r} coverage floor",
        )
        index = first_rejection.get(article.article.article_id)
        if index is None:  # pragma: no cover - a displaced story is always rejected
            result.rejected.append(replacement)
        else:
            result.rejected[index] = replacement


def select(
    ranked: Iterable[RankedArticle],
    settings: NewsletterSettings,
    *,
    manifest: RunManifest | None = None,
    published: PublishedKeys | None = None,
    reserved_source_id: str | None = None,
    reserved_slots: int | None = None,
) -> SelectionResult:
    """Choose the edition line-up. Pure function of its inputs.

    ``published`` carries the identity keys of stories previous editions already
    printed. It is passed in rather than read here: selection stays a pure
    function of its arguments, so the same inputs always produce the same
    line-up (AC9), and the pipeline owns the database.

    ``reserved_source_id`` names the source whose articles hold slots by right --
    in practice the synthetic reader-submission source -- and ``reserved_slots``
    bounds how many, ``None`` meaning "as many as there are, up to
    ``max_items``" and ``0`` meaning "none, everything competes on score". Both
    are arguments rather than settings for the same reason ``published`` is: they
    describe this run, and this module must not have to ask anything about it.
    """
    candidates: Sequence[RankedArticle] = sorted(ranked, key=ranking_key)
    result = SelectionResult()
    already_published = published or PublishedKeys()
    reserved_source = None if reserved_slots == 0 else reserved_source_id

    # Before the collapse, not after it. A copy that ran in an earlier edition
    # would otherwise win its event -- it usually scores highest, which is why it
    # was published -- and take a live follow-up down with it.
    if already_published:
        surviving: list[RankedArticle] = []
        for article in candidates:
            issue = already_published.issue_for(article.article)
            if issue is None:
                surviving.append(article)
                continue
            result.rejected.append(
                RejectedArticle(article, REASON_ALREADY_PUBLISHED, f"already published in {issue}")
            )
        candidates = surviving

    if settings.collapse_events:
        candidates, collapsed = collapse_duplicate_events(
            candidates, preferred_source_id=reserved_source
        )
        result.rejected.extend(
            RejectedArticle(ranked=item, reason=REASON_DUPLICATE_EVENT) for item in collapsed
        )

    # Second, and only after the exact keys have had their chance: the reports of
    # one event whose analyzer keys disagree, caught on the text they share. It is
    # given ``min_score`` because it folds only what could be published -- a
    # candidate the floor below will reject anyway gains nothing from being
    # collapsed, and is where every measured false positive lives. The whole
    # candidate list still goes in, because the pass measures its term statistics
    # over the run's full corpus.
    if settings.collapse_similar_events:
        candidates, folded = collapse_similar_events(
            candidates,
            threshold=settings.similar_event_threshold,
            min_score=settings.min_score,
            preferred_source_id=reserved_source,
        )
        result.rejected.extend(
            RejectedArticle(
                ranked=item,
                reason=REASON_SIMILAR_EVENT,
                detail=f"same event as {survivor.article.title!r}",
            )
            for item, survivor in folded
        )

    excluded = set(settings.excluded_categories)

    # The reserved slots are seated first, and they hold their places against the
    # caps, so everything below fills only what is genuinely left.
    result.reserved = reserve(
        candidates,
        settings,
        source_id=reserved_source,
        slots=reserved_slots,
        excluded=excluded,
    )
    reserved_ids = result.reserved_ids

    # Then the coverage floors, which is what lets a lower-scoring story from the
    # publication's own beat take a slot ahead of a higher-scoring one from
    # elsewhere. Reserved slots are seated before them and never displaced by
    # them: a floor is best-effort against the slots submissions leave free.
    floor_seats, result.floors_unmet = fill_floors(candidates, settings, reserved=result.reserved)
    preseated = [*result.reserved, *(article for _, article in floor_seats)]

    seated, result.above_threshold = _seat_earned(
        candidates,
        settings,
        preseated=preseated,
        excluded=excluded,
        rejected=result.rejected,
    )
    result.full = len(seated) >= settings.max_items

    # Print order is the documented one -- reserved first, then everything else
    # best-first -- so a floor changes *which* stories run, never the order they
    # run in. With no floor configured this is a no-op.
    rest = [article for article in seated if article.article.article_id not in reserved_ids]
    result.selected = [*result.reserved, *sorted(rest, key=ranking_key)]

    # A floor that seated a story the rubric would have reached anyway costs
    # nothing and is recorded as nothing. What an operator has to be able to see
    # is the story a floor actually took the slot of, so it is derived exactly:
    # the same pass, run once without the floors, names the line-up that would
    # have been printed instead.
    if floor_seats:
        _record_floor_cost(result, candidates, settings, floor_seats, excluded=excluded)

    if manifest is not None:
        manifest.articles_above_threshold = result.above_threshold
        manifest.articles_selected = len(result.selected)
        manifest.articles_reserved = len(result.reserved)
        # A floor the week could not fill has no story to attach itself to, so it
        # is the one omission `withheld` cannot express and it gets its own line.
        manifest.coverage_floors_unmet = dict(result.floors_unmet)
        # Rule 7: nothing is dropped silently, and the console is not an audit
        # surface. A withheld story is not a failure, so it is recorded as an
        # omission rather than an error, which would mark a healthy run as failed.
        for item in result.rejected:
            submitted = (
                reserved_source is not None and item.ranked.article.source_id == reserved_source
            )
            if item.reason not in MANIFEST_REASONS and not submitted:
                continue
            manifest.record_withheld(
                article_id=item.ranked.article.article_id,
                url=item.ranked.article.canonical_url,
                title=item.ranked.article.title,
                reason=item.reason,
                detail=submitted_detail(item.detail) if submitted else item.detail,
            )

    for item in result.rejected:
        if item.detail is not None:
            logger.info(
                "rejected %s (%s): %s", item.ranked.article.article_id, item.reason, item.detail
            )

    logger.info(
        "selection: %d selected (%d reserved) of %d above threshold (%s)",
        len(result.selected),
        len(result.reserved),
        result.above_threshold,
        result.reasons() or "no rejections",
    )
    return result
