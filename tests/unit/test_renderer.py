"""Editor and renderer.

The gate for this stage is the **generated artifact**, not the template source,
so these tests read the rendered HTML and Markdown.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from newsletter.config import NewsletterSettings
from newsletter.intelligence.client import ModelRefusal
from newsletter.intelligence.editor import (
    EDITOR_PROMPT_VERSION,
    EditorialPayload,
    NewsletterEditor,
    StoryPolish,
    build_edition,
    build_editor_content,
    load_editor_prompt,
    usable_brief,
    usable_polish,
)
from newsletter.models import (
    ArticleAssessment,
    DateWindow,
    NormalizedArticle,
    RankedArticle,
    RunManifest,
    TopicCategory,
)
from newsletter.ranking.scoring import score_components
from newsletter.ranking.selection import SelectionResult, select
from newsletter.rendering.renderer import (
    RenderError,
    markdown_escape,
    render_html,
    render_json,
    render_markdown,
    validate_edition_links,
    write_edition,
)
from tests.unit.test_analyzer import FakeResponse, make_client

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
WINDOW = DateWindow.from_dates("2026-08-11", "2026-08-17")
SETTINGS = NewsletterSettings(masthead="AI & Digital Intelligence Weekly")

#: (title, category, source priority, source name). Scores come from the formula,
#: never from a hand-picked number, so the artifacts stay internally consistent.
STORIES = [
    ("Example Labs ships a reasoning model", TopicCategory.AI_MODELS, 10, "Wire Example"),
    ("Video model opens to enterprises", TopicCategory.AI_VIDEO, 9, "Reel Report"),
    ("Ad rates shift for mid-size channels", TopicCategory.YOUTUBE_MONETIZATION, 8, "Tubewatch"),
    ("Second model launch of the week", TopicCategory.AI_MODELS, 7, "Wire Example"),
]


def make_ranked(
    index: int, title: str, category: TopicCategory, priority: int, source: str
) -> RankedArticle:
    assessment = ArticleAssessment(
        category=category,
        topic_relevance=5,
        business_impact=4,
        novelty=5,
        actionability=3,
        confidence=0.9,
        summary=f"Factual summary of: {title}.",
        why_it_matters="It changes what enterprise teams pay and plan for.",
        key_facts=["Available today", "30% cheaper"],
    )
    return RankedArticle(
        article=NormalizedArticle(
            article_id=f"art{index}",
            source_id="src",
            canonical_url=f"https://news.example/story-{index}",
            title=title,
            published_at=NOW - timedelta(days=index),
            clean_text="Article body text, long enough to be realistic for rendering tests.",
            content_hash=f"contenthash-{index}",
            retrieved_at=NOW,
        ),
        assessment=assessment,
        source_name=source,
        source_priority=priority,
        final_score=score_components(assessment, priority).total,
    )


def build_fixture_edition():
    """The canonical fixture edition, shared with scripts/refresh_expected_edition.py."""
    ranked = [make_ranked(i, *story) for i, story in enumerate(STORIES, start=1)]
    return build_edition(select(ranked, SETTINGS), SETTINGS, WINDOW, now=NOW), ranked


@pytest.fixture
def ranked() -> list[RankedArticle]:
    return [make_ranked(i, *story) for i, story in enumerate(STORIES, start=1)]


@pytest.fixture
def selection(ranked: list[RankedArticle]) -> SelectionResult:
    return select(ranked, SETTINGS)


@pytest.fixture
def edition(selection: SelectionResult):
    return build_edition(selection, SETTINGS, WINDOW, now=NOW)


@pytest.fixture
def html(edition) -> str:
    return render_html(edition, tagline="Platform, model and monetization intelligence")


@pytest.fixture
def markdown(edition) -> str:
    return render_markdown(edition)


def make_payload(**overrides: Any) -> EditorialPayload:
    values: dict[str, Any] = {
        "executive_summary": ["Model prices fell again.", "YouTube changed payout tiers."],
        "stories": [
            StoryPolish(
                article_id=f"art{i}",
                headline=f"Polished headline {i}",
                why_it_matters=f"Polished interpretation {i}.",
            )
            for i in range(1, 5)
        ],
    }
    values.update(overrides)
    return EditorialPayload(**values)


# --------------------------------------------------------------------------- #
# the editor cannot change the edition, only its wording
# --------------------------------------------------------------------------- #


def test_the_editorial_schema_has_no_field_for_links_or_ordering() -> None:
    assert set(EditorialPayload.model_fields) == {"executive_summary", "stories"}
    assert set(StoryPolish.model_fields) == {"article_id", "headline", "why_it_matters"}


def test_editor_prompt_forbids_inventing_material() -> None:
    prompt = load_editor_prompt(EDITOR_PROMPT_VERSION).lower()
    assert "never add a story" in prompt
    assert "never invent or alter a url" in prompt
    assert "never change which story leads" in prompt


def test_the_editor_receives_structured_records_not_raw_html(selection: SelectionResult) -> None:
    content = build_editor_content(selection, SETTINGS, WINDOW)
    assert "<html" not in content and "<div" not in content
    assert "article_id: art1" in content
    assert "Factual summary of" in content
    # It is not given URLs, so it cannot echo one back.
    assert "https://news.example" not in content


def test_polish_is_applied_when_it_is_clean(selection: SelectionResult) -> None:
    polish = usable_polish(make_payload(), [r.article.article_id for r in selection.selected])
    edition = build_edition(selection, SETTINGS, WINDOW, polish=polish, brief=["A brief."], now=NOW)

    assert edition.lead_story.headline == "Polished headline 1"
    assert edition.lead_story.why_it_matters == "Polished interpretation 1."
    assert edition.executive_summary == ["A brief."]


def test_polish_for_an_unselected_story_is_discarded(selection: SelectionResult) -> None:
    payload = make_payload(
        stories=[StoryPolish(article_id="ghost", headline="Sneaked in", why_it_matters="No.")]
    )
    polish = usable_polish(payload, [r.article.article_id for r in selection.selected])
    assert polish == {}


def test_duplicate_polish_entries_are_ignored(selection: SelectionResult) -> None:
    payload = make_payload(
        stories=[
            StoryPolish(article_id="art1", headline="First wording", why_it_matters="One."),
            StoryPolish(article_id="art1", headline="Second wording", why_it_matters="Two."),
        ]
    )
    polish = usable_polish(payload, [r.article.article_id for r in selection.selected])
    assert polish["art1"].headline == "First wording"


@pytest.mark.parametrize(
    "hostile",
    [
        "Read more at https://spam.example",
        '<a href="https://spam.example">Click</a>',
        "See [here](https://spam.example)",
    ],
)
def test_a_headline_containing_a_link_is_rejected(selection: SelectionResult, hostile: str) -> None:
    """A model-manufactured URL must never reach publication (AC13)."""
    payload = make_payload(
        stories=[StoryPolish(article_id="art1", headline=hostile, why_it_matters="Fine.")]
    )
    polish = usable_polish(payload, [r.article.article_id for r in selection.selected])
    edition = build_edition(selection, SETTINGS, WINDOW, polish=polish, now=NOW)

    assert edition.lead_story.headline == "Example Labs ships a reasoning model"


def test_an_over_long_headline_falls_back_to_the_original(selection: SelectionResult) -> None:
    payload = make_payload(
        stories=[StoryPolish(article_id="art1", headline="x" * 500, why_it_matters="Fine.")]
    )
    polish = usable_polish(payload, [r.article.article_id for r in selection.selected])
    edition = build_edition(selection, SETTINGS, WINDOW, polish=polish, now=NOW)
    assert edition.lead_story.headline == "Example Labs ships a reasoning model"


def test_a_brief_bullet_containing_a_link_is_dropped() -> None:
    payload = make_payload(executive_summary=["Fine bullet.", "Visit https://spam.example"])
    assert usable_brief(payload) == ["Fine bullet."]


def test_urls_and_dates_always_come_from_ingestion(selection: SelectionResult) -> None:
    edition = build_edition(
        selection, SETTINGS, WINDOW, polish=usable_polish(make_payload(), ["art1"]), now=NOW
    )
    lead_source = selection.selected[0]
    assert edition.lead_story.source_url == lead_source.article.canonical_url
    assert edition.lead_story.published_at == lead_source.article.published_at
    assert edition.lead_story.source_name == lead_source.source_name
    assert edition.lead_story.score == lead_source.final_score


def test_the_edition_keeps_the_selected_line_up(selection: SelectionResult, edition) -> None:
    """Same stories, no additions, no losses; the lead comes first."""
    published = [item.article_id for item in edition.all_items()]
    selected = [ranked.article.article_id for ranked in selection.selected]

    assert set(published) == set(selected)
    assert len(published) == len(selected)
    assert published[0] == selected[0]  # the lead story leads


def test_an_empty_selection_cannot_produce_an_edition() -> None:
    with pytest.raises(ValueError, match="no selected stories"):
        build_edition(SelectionResult(), SETTINGS, WINDOW, now=NOW)


# --------------------------------------------------------------------------- #
# the model-backed editor and its fallback
# --------------------------------------------------------------------------- #


def test_compose_uses_the_model_wording(selection: SelectionResult) -> None:
    client, fake, _ = make_client(FakeResponse(output_parsed=make_payload()))
    edition = NewsletterEditor(client).compose(selection, SETTINGS, WINDOW, now=NOW)

    assert edition.lead_story.headline == "Polished headline 1"
    assert fake.responses.calls[0]["instructions"] == load_editor_prompt()


def test_a_failed_editor_still_produces_a_complete_edition(selection: SelectionResult) -> None:
    """Polish is optional; the stories, links and order were fixed beforehand."""
    refusal = FakeResponse(
        output=[type("Item", (), {"content": [type("Part", (), {"refusal": "no"})()]})()]
    )
    client, _, _ = make_client(refusal, max_attempts=1)

    edition, error = NewsletterEditor(client).compose_or_fallback(
        selection, SETTINGS, WINDOW, now=NOW
    )

    assert isinstance(error, ModelRefusal)
    assert edition.lead_story.headline == "Example Labs ships a reasoning model"
    assert len(edition.all_items()) == len(selection.selected)
    assert edition.executive_summary  # deterministic brief


def test_the_deterministic_brief_is_used_when_there_is_no_editor(edition) -> None:
    assert edition.executive_summary
    assert all("http" not in bullet for bullet in edition.executive_summary)


# --------------------------------------------------------------------------- #
# generated HTML (AC4, AC12)
# --------------------------------------------------------------------------- #


def test_html_has_a_masthead_and_issue_metadata(html: str) -> None:
    assert "AI &amp; Digital Intelligence Weekly" in html
    assert "edición 2026-W34" in html
    # The window is half-open: it ends at midnight on the 18th and therefore
    # covers the 17th. Printing "18 Aug" would claim a day it does not include.
    assert "11 ago 2026" in html and "17 ago 2026" in html
    assert "18 ago 2026" not in html.split("</header>")[0]


def test_html_has_an_executive_brief_and_a_lead_story(html: str) -> None:
    assert "Lo esencial de la semana" in html
    assert 'class="lead"' in html
    assert "Nota principal" in html


def test_html_has_at_least_two_publication_sections(html: str) -> None:
    section_titles = re.findall(r'<h2 class="section-label" id="section-\d+">([^<]+)</h2>', html)
    assert len(section_titles) >= 2


def test_every_story_headline_is_a_link(html: str, edition) -> None:
    for item in edition.all_items():
        pattern = (
            rf'<a href="{re.escape(item.source_url)}" target="_blank" '
            rf'rel="noopener noreferrer">{re.escape(item.headline)}</a>'
        )
        assert re.search(pattern, html), f"headline link missing for {item.article_id}"


def test_every_story_has_a_visible_read_original_link(html: str, edition) -> None:
    read_original = re.findall(r'class="read-original" href="([^"]+)"', html)
    assert len(read_original) == len(edition.all_items())
    assert set(read_original) == {item.source_url for item in edition.all_items()}


def test_external_links_are_safe_and_absolute(html: str) -> None:
    hrefs = re.findall(r'<a [^>]*href="([^"]+)"', html)
    assert hrefs
    for href in hrefs:
        assert href.startswith("https://")
    for anchor in re.findall(r"<a [^>]*>", html):
        assert 'rel="noopener noreferrer"' in anchor
        assert 'target="_blank"' in anchor


def test_the_page_needs_no_javascript(html: str) -> None:
    assert "<script" not in html.lower()
    assert "onclick" not in html.lower()
    assert "javascript:" not in html.lower()


def test_the_page_is_self_contained(html: str) -> None:
    """No external requests: it must render identically offline, years later."""
    assert "<style>" in html
    assert not re.search(r'<link [^>]*rel="stylesheet"', html)
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "<img" not in html


def test_the_page_is_responsive(html: str) -> None:
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "@media (min-width: 46rem)" in html


def test_editorial_markup_is_rejected_before_rendering() -> None:
    payload = EditorialPayload(
        executive_summary=["Bullet with <b>markup</b>", "Clean bullet."], stories=[]
    )
    assert usable_brief(payload) == ["Clean bullet."]


def test_scraped_markup_in_a_headline_is_escaped_not_injected() -> None:
    """Titles come from scraped pages; the template must never trust them."""
    hostile = make_ranked(
        9, "<script>alert(1)</script> Model & API news", TopicCategory.AI_MODELS, 9, "Wire"
    )
    edition = build_edition(select([hostile], SETTINGS), SETTINGS, WINDOW, now=NOW)
    html = render_html(edition)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "Model &amp; API news" in html


def test_html_is_deterministic(edition) -> None:
    assert render_html(edition) == render_html(edition)


# --------------------------------------------------------------------------- #
# generated Markdown (AC5)
# --------------------------------------------------------------------------- #


def test_every_story_is_a_markdown_link(markdown: str, edition) -> None:
    for item in edition.all_items():
        assert f"### [{item.headline}]({item.source_url})" in markdown


def test_markdown_has_a_read_original_link_per_story(markdown: str, edition) -> None:
    assert markdown.count("[Ver la fuente →](") == len(edition.all_items())


def test_markdown_structure_is_valid(markdown: str) -> None:
    lines = markdown.splitlines()
    assert lines[0].startswith("# ")
    for index, line in enumerate(lines):
        if line.strip() == "---" and index > 0:
            # A rule directly after text would be a setext heading, not a rule.
            assert lines[index - 1].strip() == "", f"line {index}: rule needs a blank line above"
    assert "## Lo esencial de la semana" in markdown
    assert "## Nota principal" in markdown


def test_markdown_brief_is_a_real_list(markdown: str, edition) -> None:
    brief = markdown.split("## Lo esencial de la semana", 1)[1].split("---", 1)[0]
    bullets = [line for line in brief.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(edition.executive_summary)


def test_markdown_escaping_protects_link_syntax() -> None:
    assert markdown_escape("Title [with] brackets") == "Title \\[with\\] brackets"


def test_a_bracket_in_a_headline_cannot_break_the_link(selection: SelectionResult) -> None:
    payload = make_payload(
        stories=[
            StoryPolish(
                article_id="art1", headline="Model [v2] ships", why_it_matters="It is faster."
            )
        ]
    )
    polish = usable_polish(payload, [r.article.article_id for r in selection.selected])
    markdown = render_markdown(build_edition(selection, SETTINGS, WINDOW, polish=polish, now=NOW))
    assert "### [Model \\[v2\\] ships](https://news.example/story-1)" in markdown


# --------------------------------------------------------------------------- #
# link validation
# --------------------------------------------------------------------------- #


def test_validation_passes_for_an_edition_built_from_ingested_urls(
    edition, ranked: list[RankedArticle]
) -> None:
    validate_edition_links(edition, allowed_urls={r.article.canonical_url for r in ranked})


def test_validation_rejects_a_url_that_did_not_come_from_ingestion(edition) -> None:
    with pytest.raises(RenderError, match="did not originate from ingestion"):
        validate_edition_links(edition, allowed_urls={"https://news.example/something-else"})


def test_validation_rejects_a_url_hidden_in_prose(selection: SelectionResult) -> None:
    edition = build_edition(
        selection, SETTINGS, WINDOW, brief=["Go to https://spam.example"], now=NOW
    )
    with pytest.raises(RenderError, match="executive_summary contains a URL"):
        validate_edition_links(edition)


# --------------------------------------------------------------------------- #
# artifacts on disk (AC11)
# --------------------------------------------------------------------------- #


def test_write_edition_produces_every_artifact(
    tmp_path: Path, edition, ranked: list[RankedArticle]
) -> None:
    manifest = RunManifest(run_id="run-1", started_at=NOW, newsletter_generated=True)
    written = write_edition(edition, tmp_path, ranked=ranked, manifest=manifest)

    assert set(written) == {"html", "markdown", "json", "selected_articles", "run_manifest"}
    for path in written.values():
        assert path.is_file() and path.stat().st_size > 0

    assert (tmp_path / "newsletter.html").exists()
    assert (tmp_path / "newsletter.md").exists()
    assert (tmp_path / "newsletter.json").exists()
    assert (tmp_path / "selected_articles.json").exists()
    assert (tmp_path / "run_manifest.json").exists()


def test_the_json_edition_round_trips(edition) -> None:
    from newsletter.models import NewsletterEdition

    assert NewsletterEdition.model_validate_json(render_json(edition)) == edition


def test_selected_articles_carry_full_provenance(
    tmp_path: Path, edition, ranked: list[RankedArticle]
) -> None:
    write_edition(edition, tmp_path, ranked=ranked)
    rows = json.loads((tmp_path / "selected_articles.json").read_text(encoding="utf-8"))

    assert len(rows) == len(ranked)
    first = rows[0]
    assert first["score_breakdown"]["total"] == first["final_score"]
    assert first["canonical_url"].startswith("https://")
    assert "assessment" in first and "content_hash" in first


def test_writing_refuses_an_edition_with_a_foreign_link(tmp_path: Path, edition) -> None:
    with pytest.raises(RenderError):
        write_edition(edition, tmp_path, allowed_urls={"https://elsewhere.example/x"})
    assert not (tmp_path / "newsletter.html").exists()


# --------------------------------------------------------------------------- #
# golden fixtures — a template change must show up as a readable diff
# --------------------------------------------------------------------------- #

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures"


def test_markdown_matches_the_golden_edition(markdown: str) -> None:
    expected = (GOLDEN / "expected_newsletter.md").read_text(encoding="utf-8")
    assert markdown == expected, "run scripts/refresh_expected_edition.py if this is intended"


def test_the_json_edition_matches_the_golden_edition(edition) -> None:
    expected = (GOLDEN / "expected_newsletter.json").read_text(encoding="utf-8")
    assert render_json(edition) == expected, (
        "run scripts/refresh_expected_edition.py if this is intended"
    )


def test_the_golden_html_still_carries_its_links() -> None:
    """Guards the committed artifact itself, not just freshly rendered output."""
    html = (GOLDEN / "expected_newsletter.html").read_text(encoding="utf-8")
    assert html.count('rel="noopener noreferrer"') >= 3
    assert "<script" not in html.lower()
    assert "Lo esencial de la semana" in html
