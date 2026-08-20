"""Entity fidelity — a deterministic guard against corrupted brand names.

The edition is written in Spanish from English sources, so almost nothing in the
published prose can be compared with the article word for word. Proper names are
the exception: ``YouTube`` is ``YouTube`` in every language, and when the model
prints ``UTube`` it has put a company that does not exist into print. No fact was
fabricated; the name was corrupted, which is just as wrong on the page.

This module catches that one class and nothing else, with no model call. It sits
beside :mod:`newsletter.intelligence.schemas` and does the same job: the model is
*asked* for faithful prose, and Python *checks* it afterwards.

Two narrow rules, applied in order:

* a token is **checkable** only when its shape survives translation — an
  uppercase letter somewhere other than the first position (``YouTube``,
  ``OpenAI``, ``ChatGPT``, ``xAI``), or letters mixed with digits (``GPT-4``,
  ``H100``, ``S3``). All-caps acronyms are skipped on purpose: ``IA`` in Spanish
  is ``AI`` in English, and ``EE``, ``UU``, ``CEO`` and ``API`` are all ordinary
  in an edition whose source never spells them that way;
* a checkable token is a **violation** only when no *word* of the trusted text
  ingestion collected for that story vouches for it, compared without regard to
  case or punctuation.

Both halves are deliberately permissive. Dropping a real story costs the reader
a story; missing one corruption costs the reader one wrong name, so every
ambiguous case is resolved in favour of publishing. What is *not* negotiable is
the left-hand word boundary: ``utube`` is a plain substring of ``youtube``, so
a bare substring test would call the very defect this module exists for
perfectly supported. Every comparison here therefore starts at a word, and the
only freedom is how far to the right it may run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import islice

from newsletter.models import NewsletterEdition, RankedArticle, ValueModel


class EntityFidelityError(Exception):
    """Reader-visible prose named an entity its own source never does."""


class EntityViolation(ValueModel):
    """One named entity that the story's trusted text does not support."""

    #: The story the prose belongs to, or the edition id for the brief.
    article_id: str
    #: Which reader-visible field printed it, e.g. ``"headline"``.
    field: str
    #: The offending token, exactly as it was printed.
    token: str


# --------------------------------------------------------------------------- #
# tokenizing
# --------------------------------------------------------------------------- #


def _trim(token: str) -> str:
    """Drop quotes, brackets and punctuation from both ends, keeping the inside.

    ``«YouTube».`` becomes ``YouTube`` and ``(GPT-4,`` becomes ``GPT-4``. The
    interior is left alone so hyphenated and dotted names stay whole: splitting
    ``GPT-4`` would leave ``GPT`` (an acronym, skipped) and ``4`` (a number,
    skipped), and the corruption this module exists to catch would walk through.
    """
    start, end = 0, len(token)
    while start < end and not token[start].isalnum():
        start += 1
    while end > start and not token[end - 1].isalnum():
        end -= 1
    return token[start:end]


def is_checkable(token: str) -> bool:
    """True when the token reads the same in Spanish as it does in English."""
    letters = [character for character in token if character.isalpha()]
    if not letters:
        return False  # a bare number or symbol carries no name
    if any(character.isdigit() for character in token):
        return True  # letters and digits together: GPT-4, H100, S3
    if all(character.isupper() for character in letters):
        return False  # IA, EE, UU, CEO, API — legitimately different per language
    return any(character.isupper() for character in letters[1:])


def checkable_tokens(text: str) -> list[str]:
    """Every checkable token in ``text``, in reading order, without repeats."""
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in text.split():
        token = _trim(raw)
        if not token or not is_checkable(token):
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


# --------------------------------------------------------------------------- #
# checking
# --------------------------------------------------------------------------- #


#: The shortest source stem allowed to vouch for a longer prose token. Three,
#: because a source writing ``GPT`` has to keep supporting prose writing
#: ``GPT-4``, and that stem is three characters long. Two would be reckless: a
#: rule that lets any two-character stem vouch for anything starting with it
#: turns ``AI``, ``EE`` or ``Op`` into a licence for every brand in the
#: alphabet, and the guard would stop guarding without ever saying so.
MIN_STEM = 3


def normalize(token: str) -> str:
    """The comparable form of a token: alphanumerics only, case folded.

    Punctuation carries no meaning across the translation. Scraped English
    routinely writes ``GPT4`` and ``H100`` where the Spanish prose writes
    ``GPT-4`` and ``H-100``; both sides collapse to ``gpt4`` and ``h100`` here,
    so a hyphen can never cost a story its place in the edition.
    """
    return "".join(character for character in token if character.isalnum()).casefold()


def source_words(source_text: str) -> list[str]:
    """The trusted text as normalized words, in reading order.

    A word is whitespace-delimited, so the punctuation *inside* it is absorbed
    rather than treated as a boundary: ``GPT-4`` yields one unit, ``gpt4``, and
    never the bare stem ``gpt``. That matters — a source that says ``GPT-4``
    must not vouch for prose that says ``GPT-5``, which is exactly what a loose
    ``gpt`` unit would do.
    """
    return [word for word in (normalize(raw) for raw in source_text.split()) if word]


def supported(words: Sequence[str], token: str) -> bool:
    """True when a word, or a run of consecutive words, vouches for ``token``.

    ``token`` is already normalized. Starting at each word in turn, words are
    merged until the growing stem settles the question — merging is what lets a
    source writing ``YouTube Shorts`` support prose writing ``YouTube-Shorts``.
    A match counts in either length direction:

    * the stem is the shorter side (``GPT`` for ``GPT-4``) — allowed once the
      stem reaches :data:`MIN_STEM`, so short fragments cannot vouch for
      everything;
    * the token is the shorter side (``YouTube`` for a source ``YouTubers``) —
      always allowed, as it was before, since the source is the trusted side.

    Both directions are anchored at a word, so ``utube`` is still unsupported by
    an article that only ever writes ``YouTube``.
    """
    if not token:
        return False
    for start in range(len(words)):
        stem = ""
        for word in islice(words, start, None):
            stem += word
            if stem == token:
                return True
            if len(stem) >= len(token):
                if stem.startswith(token):
                    return True  # the source carries a longer form of the name
                break
            if not token.startswith(stem):
                break  # merging further words can only push the stem away
            if len(stem) >= MIN_STEM:
                return True
    return False


def mentions(source_text: str, token: str) -> bool:
    """True when ``source_text`` vouches for ``token``, ignoring case and dashes."""
    return supported(source_words(source_text), normalize(token))


def find_unsupported_entities(
    fields: Mapping[str, str | Sequence[str]],
    source_text: str,
    *,
    article_id: str = "",
) -> list[EntityViolation]:
    """Every named entity in ``fields`` that ``source_text`` never mentions.

    ``fields`` maps a reader-visible field name to its prose; a value may be a
    single string or a list of them (``key_facts``, ``executive_summary``). The
    result is sorted by field and token, so the same inputs always produce the
    same report and the same edition (AC9).
    """
    violations: list[EntityViolation] = []
    words = source_words(source_text)  # normalized once, reused by every token

    for name, value in fields.items():
        parts = [value] if isinstance(value, str) else list(value)
        reported: set[str] = set()
        for part in parts:
            for token in checkable_tokens(part):
                key = token.casefold()
                if key in reported or supported(words, normalize(token)):
                    continue
                reported.add(key)
                violations.append(EntityViolation(article_id=article_id, field=name, token=token))

    return sorted(violations, key=lambda violation: (violation.field, violation.token))


def trusted_text(ranked: RankedArticle) -> str:
    """Everything about a story that did not come from a model.

    The source name, the title and the article body are exactly what
    ``analyzer.build_content`` puts in front of the analyst, all of it from
    ingestion or configuration. A name the model could only have read here is
    supported by definition.
    """
    return "\n".join((ranked.source_name, ranked.article.title, ranked.article.clean_text))


def describe_violations(violations: Sequence[EntityViolation]) -> str:
    """A one-line, human-readable account of what was printed and where."""
    return "; ".join(f"{violation.field} says {violation.token!r}" for violation in violations)


def unsupported_in_assessment(ranked: RankedArticle) -> list[EntityViolation]:
    """Analyst-authored prose that names an entity its own article never does.

    ``why_it_matters`` is deliberately excluded. It is the one field whose job is
    inference rather than reporting: ADR-0032 puts the audience in the rubric, so
    it is *supposed* to name Leader Entertainment's world -- YouTube, Shorts,
    kids' content -- for a story whose source never mentions any of them. Guarding
    it cost a real edition the most relevant story it had, an OpenAI teen-safety
    launch, because the interpretation said "YouTube" and OpenAI's post did not.
    Fabricated entities are a reporting failure, so the guard belongs on the
    reporting fields; an unsupported *inference* is an editorial problem a
    string comparison cannot see.
    """
    return find_unsupported_entities(
        {
            "summary": ranked.assessment.summary,
            "key_facts": list(ranked.assessment.key_facts),
        },
        trusted_text(ranked),
        article_id=ranked.article.article_id,
    )


def unsupported_in_edition(
    edition: NewsletterEdition, selected: Sequence[RankedArticle]
) -> list[EntityViolation]:
    """Editor-authored prose that names an entity no published source does.

    Headlines are checked against their own story; ``why_it_matters`` is not, for
    the reason given in :func:`unsupported_in_assessment` -- the editor may
    override it, and it is interpretation either way. The executive brief is
    written about the week rather than about one article, so any published story
    may support it.
    """
    by_id = {ranked.article.article_id: ranked for ranked in selected}
    violations: list[EntityViolation] = []

    for item in edition.all_items():
        ranked = by_id.get(item.article_id)
        if ranked is None:  # unreachable: the edition is assembled from `selected`
            continue
        violations.extend(
            find_unsupported_entities(
                {"headline": item.headline},
                trusted_text(ranked),
                article_id=item.article_id,
            )
        )

    violations.extend(
        find_unsupported_entities(
            {"executive_summary": list(edition.executive_summary)},
            "\n".join(trusted_text(ranked) for ranked in selected),
            article_id=edition.edition_id,
        )
    )
    return violations
