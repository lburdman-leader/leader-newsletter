"""The entity-fidelity guard.

The edition is Spanish, the sources are English, so the only prose that can be
compared word for word is a proper name. These tests pin both halves of that
narrow rule: which tokens are worth checking, and when a checked token counts as
unsupported.
"""

from __future__ import annotations

from newsletter.intelligence.editor import build_edition
from newsletter.intelligence.fidelity import (
    EntityViolation,
    checkable_tokens,
    find_unsupported_entities,
    is_checkable,
    trusted_text,
    unsupported_in_assessment,
    unsupported_in_edition,
)
from newsletter.ranking.selection import select
from tests.unit.test_renderer import NOW, SETTINGS, STORIES, WINDOW, make_ranked

SOURCE = (
    "YouTube said the Partner Program will change payout tiers in October. "
    "OpenAI shipped GPT-4 to enterprise customers on the same day."
)


# --------------------------------------------------------------------------- #
# which tokens are worth checking at all
# --------------------------------------------------------------------------- #


def test_a_token_with_an_inner_capital_is_checkable() -> None:
    for token in ("YouTube", "UTube", "OpenAI", "ChatGPT", "TikTok", "xAI"):
        assert is_checkable(token), token


def test_an_all_caps_acronym_is_never_checkable() -> None:
    """IA is AI, EE.UU. is the US: legitimately different in the two languages."""
    for token in ("IA", "EE", "UU", "CEO", "API", "HTML"):
        assert not is_checkable(token), token


def test_an_ordinary_capitalised_word_is_never_checkable() -> None:
    for token in ("Google", "España", "Octubre", "semana"):
        assert not is_checkable(token), token


def test_a_token_mixing_letters_and_digits_is_checkable() -> None:
    for token in ("GPT-4", "H100", "S3", "COVID-19"):
        assert is_checkable(token), token


def test_a_bare_number_is_never_checkable() -> None:
    for token in ("2026", "30", "2026-08-17", "1.000.000"):
        assert not is_checkable(token), token


def test_surrounding_punctuation_is_stripped_before_the_token_is_judged() -> None:
    assert checkable_tokens('«YouTube», (GPT-4). "UTube"!') == ["YouTube", "GPT-4", "UTube"]


def test_a_repeated_token_is_reported_once() -> None:
    assert checkable_tokens("YouTube y youtube y YOUTUBE") == ["YouTube"]


# --------------------------------------------------------------------------- #
# supported or not
# --------------------------------------------------------------------------- #


def test_a_corrupted_brand_is_not_supported_by_the_source() -> None:
    """The defect this module exists for: no fabricated fact, a mangled name."""
    violations = find_unsupported_entities({"summary": "UTube cambió los pagos."}, SOURCE)

    assert [(v.field, v.token) for v in violations] == [("summary", "UTube")]


def test_a_brand_the_source_actually_names_is_clean() -> None:
    assert find_unsupported_entities({"summary": "YouTube cambió los pagos."}, SOURCE) == []


def test_the_source_is_matched_case_insensitively() -> None:
    """Deliberately permissive: a false negative costs less than a dropped story."""
    assert find_unsupported_entities({"summary": "Habló OpenAi sobre GPT-4."}, SOURCE) == []


def test_a_spanish_acronym_the_english_source_never_uses_is_still_clean() -> None:
    text = "La IA generativa y los CEO de EE.UU. discuten la API."
    assert find_unsupported_entities({"summary": text}, SOURCE) == []


def test_a_model_number_the_source_never_mentions_is_a_violation() -> None:
    violations = find_unsupported_entities({"summary": "Presentaron GPT-5 y la H100."}, SOURCE)

    assert [v.token for v in violations] == ["GPT-5", "H100"]


def test_every_reader_visible_field_is_checked_including_lists() -> None:
    violations = find_unsupported_entities(
        {
            "headline": "UTube paga menos",
            "why_it_matters": "Afecta a YouTube.",
            "key_facts": ["Vigente en octubre", "Anunciado por MetaVerse"],
        },
        SOURCE,
        article_id="art1",
    )

    assert [(v.field, v.token) for v in violations] == [
        ("headline", "UTube"),
        ("key_facts", "MetaVerse"),
    ]
    assert all(v.article_id == "art1" for v in violations)


def test_violations_come_back_in_a_stable_order() -> None:
    """Same inputs, same artifacts (AC9), whatever order the prose was written in."""
    fields = {"summary": "ZetaCorp y AlphaBot", "headline": "MegaThing"}
    first = find_unsupported_entities(fields, SOURCE)
    second = find_unsupported_entities(dict(reversed(list(fields.items()))), SOURCE)

    assert first == second
    assert [(v.field, v.token) for v in first] == [
        ("headline", "MegaThing"),
        ("summary", "AlphaBot"),
        ("summary", "ZetaCorp"),
    ]


# --------------------------------------------------------------------------- #
# punctuation and morphology must not delete a faithful story
# --------------------------------------------------------------------------- #


def test_a_hyphen_the_source_does_not_write_is_not_a_violation() -> None:
    """Scraped English drops the dash a model almost always prints."""
    rows = [
        ("OpenAI released GPT4 today.", "OpenAI presentó GPT-4 a las empresas."),
        ("Nvidia H100 shipments rose.", "Los chips H-100 de Nvidia."),
    ]
    for source, prose in rows:
        assert find_unsupported_entities({"summary": prose}, source) == [], prose


def test_a_hyphen_the_source_writes_as_a_space_is_not_a_violation() -> None:
    """`YouTube Shorts` in the article, `YouTube-Shorts` in the edition."""
    source = "YouTube Shorts grew fast."

    assert find_unsupported_entities({"summary": "YouTube-Shorts creció rápido."}, source) == []


def test_spanish_morphology_longer_than_the_source_is_not_a_violation() -> None:
    """`YouTubers` is ordinary Spanish tech vocabulary; the source says `YouTube`."""
    source = "YouTube changed its rules."

    assert find_unsupported_entities({"summary": "Los YouTubers ganan menos ahora."}, source) == []


def test_the_corruption_survives_all_of_that_and_is_still_caught() -> None:
    """The whole reason the module exists: `utube` is a substring of `youtube`."""
    source = "YouTube already used this for Shorts."
    violations = find_unsupported_entities(
        {"summary": "Esta política, que UTube aplicó antes."}, source
    )

    assert [v.token for v in violations] == ["UTube"]


def test_spanish_acronyms_stay_clean_against_an_unrelated_source() -> None:
    source = "YouTube changed its rules."

    assert (
        find_unsupported_entities({"summary": "La IA generativa y el CEO según la API."}, source)
        == []
    )


def test_merging_adjacent_words_cannot_manufacture_a_corrupted_brand() -> None:
    """Merging across whitespace is permissive, but it never starts mid-word."""
    source = "You use the tube for video on YouTube every day."
    violations = find_unsupported_entities({"summary": "UTube paga menos."}, source)

    assert [v.token for v in violations] == ["UTube"]


def test_a_three_letter_source_stem_supports_a_longer_model_number() -> None:
    assert find_unsupported_entities({"summary": "Llegó GPT-4."}, "GPT is here.") == []


def test_a_source_stem_shorter_than_three_characters_vouches_for_nothing() -> None:
    """Without a floor, a bidirectional prefix rule stops guarding anything."""
    prose = {"summary": "UTube paga menos."}

    assert [v.token for v in find_unsupported_entities(prose, "Ut.")] == ["UTube"]
    assert find_unsupported_entities(prose, "UTu.") == []


def test_ignoring_punctuation_does_not_excuse_the_wrong_model_number() -> None:
    source = "OpenAI released GPT4 today."

    assert [v.token for v in find_unsupported_entities({"summary": "Llegó GPT-5."}, source)] == [
        "GPT-5"
    ]


def test_a_violation_names_the_field_the_token_and_the_story() -> None:
    violation = find_unsupported_entities({"headline": "UTube"}, SOURCE, article_id="art9")[0]

    assert violation == EntityViolation(article_id="art9", field="headline", token="UTube")


# --------------------------------------------------------------------------- #
# against real pipeline objects
# --------------------------------------------------------------------------- #


def test_the_title_and_source_name_count_as_supporting_text() -> None:
    """Both reach the analyst as trusted metadata, so neither can be an invention."""
    ranked = make_ranked(1, *STORIES[0])

    assert ranked.source_name in trusted_text(ranked)
    assert ranked.article.title in trusted_text(ranked)
    assert unsupported_in_assessment(ranked) == []


def test_an_analyst_that_corrupts_a_name_is_caught_on_its_own_article() -> None:
    ranked = make_ranked(1, *STORIES[0])
    corrupted = ranked.model_copy(
        update={
            "assessment": ranked.assessment.model_copy(
                update={"why_it_matters": "Cambia lo que pagan en UTube."}
            )
        }
    )

    violations = unsupported_in_assessment(corrupted)
    assert [(v.field, v.token) for v in violations] == [("why_it_matters", "UTube")]
    assert violations[0].article_id == corrupted.article.article_id


def test_a_clean_edition_reports_nothing() -> None:
    ranked = [make_ranked(i, *story) for i, story in enumerate(STORIES, start=1)]
    selection = select(ranked, SETTINGS)
    edition = build_edition(selection, SETTINGS, WINDOW, now=NOW)

    assert unsupported_in_edition(edition, selection.selected) == []


def test_a_corrupted_headline_is_caught_on_the_finished_edition() -> None:
    ranked = [make_ranked(i, *story) for i, story in enumerate(STORIES, start=1)]
    selection = select(ranked, SETTINGS)
    edition = build_edition(selection, SETTINGS, WINDOW, now=NOW)
    lead = edition.lead_story.model_copy(update={"headline": "UTube paga menos"})
    edition = edition.model_copy(update={"lead_story": lead})

    violations = unsupported_in_edition(edition, selection.selected)
    assert [(v.field, v.token) for v in violations] == [("headline", "UTube")]


def test_the_brief_may_lean_on_any_published_story() -> None:
    """It summarises the week, not one article, so the whole line-up supports it."""
    ranked = [make_ranked(i, *story) for i, story in enumerate(STORIES, start=1)]
    last = ranked[-1]
    ranked[-1] = last.model_copy(
        update={"article": last.article.model_copy(update={"clean_text": "Only YouTube said so."})}
    )
    selection = select(ranked, SETTINGS)
    assert selection.selected[-1].article.article_id == "art4"  # the story that carries the name

    edition = build_edition(selection, SETTINGS, WINDOW, now=NOW)
    supported = edition.model_copy(update={"executive_summary": ["Otra semana para YouTube."]})
    invented = edition.model_copy(update={"executive_summary": ["Otra semana para NewsCorp."]})

    assert unsupported_in_edition(supported, selection.selected) == []
    assert [v.token for v in unsupported_in_edition(invented, selection.selected)] == ["NewsCorp"]
