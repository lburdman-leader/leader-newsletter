#!/usr/bin/env python3
"""Measure the score distribution of the assessments already in the cache.

Every calibration number this engine runs on -- ``min_score`` above all -- was
argued from a handful of hand-counted articles. That was affordable while the
rubric was stable. It stops being affordable the moment the rubric changes,
because a new rubric moves the whole distribution and the old threshold then
means something else: too low and the edition fills with filler, too high and
the coverage floor starves and the newspaper prints short.

So this reads what the run already paid for. Assessments are cached with their
four ratings, so a distribution costs no model call at all -- only a join and the
production formula, imported rather than reimplemented. A harness that disagrees
with ``ranking.scoring`` would be worse than no harness, so it does not have its
own arithmetic to disagree with: it calls :func:`~newsletter.ranking.scoring.rank_all`.

**The number to recalibrate against is own-beat headroom, not the pass rate.**
``selection.py`` makes the coverage floor obey ``min_score``: a floor story is
seated only if it clears the threshold like any other. Raise the threshold above
the mass of own-beat assessments and the floor can no longer be met, and the
edition publishes short -- which looks like a thin week and is in fact a
mis-set number. The headroom section counts exactly that.

The database is opened read-only, through the ``Storage`` contract, so this can
be pointed at a live database without altering the thing it is reporting on.

Usage::

    python scripts/score_distribution.py
    python scripts/score_distribution.py --prompt-version v2 --min-score 62
    python scripts/score_distribution.py --prompt-version v3 --category
    python scripts/score_distribution.py --database-url sqlite:///newsletter.sqlite --json

Exit code 0 when a distribution was measured, 1 when there is nothing to measure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from newsletter.config import DEFAULT_CONFIG_DIR, AppConfig, ConfigError, load_config
from newsletter.models import RankedArticle, TopicCategory
from newsletter.persistence.base import PersistenceError
from newsletter.persistence.factory import create_storage
from newsletter.ranking.scoring import MAX_SCORE, rank_all

#: Width of one histogram band. Ten bands over 0-100, the last one closed.
BAND = 10


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Distribution:
    """What a set of scores looks like, and how many of them clear a threshold."""

    scores: list[int]
    threshold: int

    @property
    def count(self) -> int:
        return len(self.scores)

    @property
    def minimum(self) -> int:
        return self.scores[0]

    @property
    def maximum(self) -> int:
        return self.scores[-1]

    @property
    def median(self) -> int:
        return percentile(self.scores, 0.5)

    @property
    def p90(self) -> int:
        return percentile(self.scores, 0.9)

    @property
    def passing(self) -> int:
        return sum(1 for score in self.scores if score >= self.threshold)

    @property
    def pass_rate(self) -> float:
        return self.passing / self.count if self.count else 0.0

    def histogram(self) -> list[tuple[int, int, int]]:
        """``(low, high, count)`` per decile band, the last band closed at 100."""
        counts = Counter(min(score // BAND, (MAX_SCORE // BAND) - 1) for score in self.scores)
        bands = []
        for index in range(MAX_SCORE // BAND):
            low = index * BAND
            high = low + BAND - 1 if low + BAND < MAX_SCORE else MAX_SCORE
            bands.append((low, high, counts.get(index, 0)))
        return bands


def percentile(sorted_scores: list[int], quantile: float) -> int:
    """Nearest-rank percentile, so every reported value is a score that exists.

    Interpolating would invent a 56.5 that no article ever scored, and these
    numbers are read as candidate thresholds.
    """
    if not sorted_scores:
        return 0
    rank = max(math.ceil(quantile * len(sorted_scores)), 1)
    return sorted_scores[rank - 1]


def measure(scores: list[int], threshold: int) -> Distribution:
    return Distribution(scores=sorted(scores), threshold=threshold)


def score_everything(
    database_url: str, *, prompt_version: str | None = None
) -> list[RankedArticle]:
    """Every cached assessment, scored by the production formula.

    Read-only and through the contract: ``create_storage`` picks the backend from
    the URL, ``stored_assessments`` does the assessment/article/source join, and
    ``rank_all`` applies the one formula there is.
    """
    storage = create_storage(database_url, read_only=True)
    with storage as database:
        rows = database.stored_assessments(prompt_version=prompt_version)

    return rank_all(
        [(row.article, row.record) for row in rows],
        {row.source.id: row.source for row in rows},
    )


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def own_beat_groups(config: AppConfig) -> dict[str, tuple[list[TopicCategory], int]]:
    """The configured coverage floors: which categories count, and how many are needed."""
    return {
        name: (list(floor.categories), floor.minimum)
        for name, floor in config.newsletter.coverage_floors.items()
    }


def render(
    ranked: list[RankedArticle],
    *,
    config: AppConfig,
    prompt_version: str | None,
    threshold: int,
    with_categories: bool,
) -> str:
    scores = [article.final_score for article in ranked]
    overall = measure(scores, threshold)
    rubric = prompt_version or "all versions"

    lines = [
        f"Score distribution -- prompt {rubric}, threshold {threshold}",
        "",
        f"  count    {overall.count:>6}",
        f"  min      {overall.minimum:>6}",
        f"  median   {overall.median:>6}",
        f"  p90      {overall.p90:>6}",
        f"  max      {overall.maximum:>6}",
        "",
        "  score band     count",
    ]
    widest = max((count for _, _, count in overall.histogram()), default=1) or 1
    for low, high, count in overall.histogram():
        bar = "#" * round(40 * count / widest)
        lines.append(f"  {low:>3}-{high:<3}   {count:>6}  {bar}")

    lines += [
        "",
        f"  at or above {threshold}: {overall.passing} of {overall.count} "
        f"({overall.pass_rate:.1%})",
    ]

    groups = own_beat_groups(config)
    if groups:
        lines += [
            "",
            "  own-beat headroom -- the coverage floor obeys min_score, so a",
            "  threshold above this mass starves the floor and prints short.",
        ]
        for name, (categories, minimum) in groups.items():
            in_group = [a for a in ranked if a.assessment.category in categories]
            clearing = [a for a in in_group if a.final_score >= threshold]
            verdict = "ok" if len(clearing) >= minimum else "STARVED"
            lines.append(f"    {name} (needs {minimum} per edition)   {verdict}")
            for category in categories:
                total = sum(1 for a in in_group if a.assessment.category is category)
                clear = sum(1 for a in clearing if a.assessment.category is category)
                lines.append(f"      {category.value:<22} {clear:>5} of {total:>5}")
            lines.append(f"      {'total':<22} {len(clearing):>5} of {len(in_group):>5}")

    if with_categories:
        lines += [
            "",
            "  by category -- `relevance>=3` is the rubric-inflation tripwire:",
            "  a rubric that starts calling general AI news relevant shows up here",
            "  before it shows up in the edition.",
            "",
            f"    {'category':<22} {'n':>5} {'median':>7} {'>=thr':>7} {'rel>=3':>8}",
        ]
        for category in TopicCategory:
            rows = [a for a in ranked if a.assessment.category is category]
            if not rows:
                continue
            block = measure([a.final_score for a in rows], threshold)
            relevant = sum(1 for a in rows if a.assessment.topic_relevance >= 3)
            lines.append(
                f"    {category.value:<22} {block.count:>5} {block.median:>7} "
                f"{block.passing:>7} {relevant / block.count:>7.0%}"
            )

    return "\n".join(lines)


def as_json(
    ranked: list[RankedArticle],
    *,
    config: AppConfig,
    prompt_version: str | None,
    threshold: int,
) -> dict[str, object]:
    overall = measure([a.final_score for a in ranked], threshold)
    payload: dict[str, object] = {
        "prompt_version": prompt_version,
        "threshold": threshold,
        "count": overall.count,
        "min": overall.minimum,
        "median": overall.median,
        "p90": overall.p90,
        "max": overall.maximum,
        "passing": overall.passing,
        "pass_rate": round(overall.pass_rate, 4),
        "histogram": [
            {"low": low, "high": high, "count": count} for low, high, count in overall.histogram()
        ],
        "own_beat": {
            name: {
                "minimum": minimum,
                "clearing": sum(
                    1
                    for a in ranked
                    if a.assessment.category in categories and a.final_score >= threshold
                ),
                "assessed": sum(1 for a in ranked if a.assessment.category in categories),
            }
            for name, (categories, minimum) in own_beat_groups(config).items()
        },
        "by_category": {},
    }
    by_category: dict[str, object] = {}
    for category in TopicCategory:
        rows = [a for a in ranked if a.assessment.category is category]
        if not rows:
            continue
        block = measure([a.final_score for a in rows], threshold)
        by_category[category.value] = {
            "count": block.count,
            "median": block.median,
            "passing": block.passing,
            "topic_relevance_ge_3": sum(1 for a in rows if a.assessment.topic_relevance >= 3),
        }
    payload["by_category"] = by_category
    return payload


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the score distribution of cached assessments. No model calls.",
    )
    parser.add_argument(
        "--config-dir",
        default=str(DEFAULT_CONFIG_DIR),
        help="where newsletter.yaml and sources.yaml live (default: config)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override the configured database; opened read-only either way",
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="measure one rubric only, e.g. v2 -- omit to measure every cached assessment",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="the candidate threshold to report against (default: the configured min_score)",
    )
    parser.add_argument(
        "--category",
        action="store_true",
        help="add a per-category breakdown, including the share rated topic_relevance >= 3",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    database_url = args.database_url or config.runtime.database_url
    threshold = config.newsletter.min_score if args.min_score is None else args.min_score
    if not 0 <= threshold <= MAX_SCORE:
        print(f"Error: --min-score must be between 0 and {MAX_SCORE}", file=sys.stderr)
        return 1

    try:
        ranked = score_everything(database_url, prompt_version=args.prompt_version)
    except (ConfigError, PersistenceError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not ranked:
        which = f" for prompt {args.prompt_version}" if args.prompt_version else ""
        print(f"No cached assessments{which}; nothing to measure.", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                as_json(
                    ranked,
                    config=config,
                    prompt_version=args.prompt_version,
                    threshold=threshold,
                ),
                indent=2,
            )
        )
    else:
        print(
            render(
                ranked,
                config=config,
                prompt_version=args.prompt_version,
                threshold=threshold,
                with_categories=args.category,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
