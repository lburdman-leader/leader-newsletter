"""Unit tests for configuration loading.

A bad configuration must fail the run before any network call, with a message
that says which file and which field is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsletter.config import (
    AppConfig,
    ConfigError,
    NewsletterSettings,
    SubmissionSettings,
    load_config,
)
from newsletter.models import TopicCategory, WindowMode

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_DIR = REPO_ROOT / "config"

MINIMAL_SOURCES = """
sources:
  - id: alpha
    name: Alpha
    category_hint: ai_models
    entrypoint: "https://alpha.example/feed"
    strategy: rss
    priority: 9
    enabled: true
  - id: beta
    name: Beta
    category_hint: ai_video
    entrypoint: "https://beta.example/feed"
    strategy: scrapling_static
    priority: 4
    enabled: false
"""

MINIMAL_NEWSLETTER = """
newsletter:
  masthead: "Test Weekly"
  timezone: "UTC"
  window_days: 7
  max_items: 8
  min_score: 70
  section_limits:
    ai_models: 3
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "sources.yaml").write_text(MINIMAL_SOURCES, encoding="utf-8")
    (tmp_path / "newsletter.yaml").write_text(MINIMAL_NEWSLETTER, encoding="utf-8")
    return tmp_path


def write_sources(directory: Path, body: str) -> None:
    (directory / "sources.yaml").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# the configuration shipped in the repository
# --------------------------------------------------------------------------- #


def test_the_shipped_configuration_is_the_one_the_newsletter_is_tuned_for() -> None:
    """Every editorial number the repository ships, in one place.

    A retuning that silently changes what the edition prints has to change this
    test, and a source list with a duplicate id or an invented category fails it.
    """
    config = load_config(REAL_CONFIG_DIR, env={})
    settings = config.newsletter

    assert config.sources
    assert config.enabled_sources
    assert settings.min_score == 62  # recalibrated for the v2 rubric
    assert settings.masthead == "Leader Intelligence Semanal"
    assert settings.max_items == 10  # a bank of links, submissions first
    assert settings.max_per_subject == 2
    # Half the edition. Unset would mean "every pending submission up to
    # max_items", and the intake form authenticates nobody, so one submitter
    # could take all ten slots and the rubric would print nothing.
    assert config.submissions.reserved_slots == 5
    assert settings.suppress_already_published is True
    assert settings.collapse_similar_events is True
    assert settings.similar_event_threshold == 0.21

    ids = [s.id for s in config.sources]
    assert len(ids) == len(set(ids))
    for source in config.sources:
        assert isinstance(source.category_hint, TopicCategory)


# --------------------------------------------------------------------------- #
# loading and ordering
# --------------------------------------------------------------------------- #


def test_load_config_reads_both_files(config_dir: Path) -> None:
    config = load_config(config_dir, env={})
    assert isinstance(config, AppConfig)
    assert [s.id for s in config.sources] == ["alpha", "beta"]
    assert config.newsletter.masthead == "Test Weekly"
    assert config.newsletter.window_mode is WindowMode.ROLLING


def test_enabled_sources_are_ordered_deterministically(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - {id: zulu, name: Zulu, entrypoint: "https://z.example/f", strategy: rss, priority: 5}
  - {id: alpha, name: Alpha, entrypoint: "https://a.example/f", strategy: rss, priority: 5}
  - {id: mike, name: Mike, entrypoint: "https://m.example/f", strategy: rss, priority: 9}
""",
    )
    config = load_config(config_dir, env={})
    assert [s.id for s in config.enabled_sources] == ["mike", "alpha", "zulu"]


def test_the_entity_fidelity_guard_is_on_unless_it_is_switched_off(config_dir: Path) -> None:
    """A corrupted brand name is an error in print, so the guard is opt-out."""
    assert load_config(config_dir, env={}).newsletter.check_entity_fidelity is True
    assert load_config(REAL_CONFIG_DIR, env={}).newsletter.check_entity_fidelity is True

    (config_dir / "newsletter.yaml").write_text(
        f"{MINIMAL_NEWSLETTER}  check_entity_fidelity: false\n", encoding="utf-8"
    )
    assert load_config(config_dir, env={}).newsletter.check_entity_fidelity is False


def test_a_non_boolean_entity_fidelity_setting_is_reported(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text(
        f"{MINIMAL_NEWSLETTER}  check_entity_fidelity: sometimes\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="check_entity_fidelity"):
        load_config(config_dir, env={})


def test_source_by_id(config_dir: Path) -> None:
    config = load_config(config_dir, env={})
    assert config.source_by_id("alpha").name == "Alpha"
    with pytest.raises(KeyError):
        config.source_by_id("missing")


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #


def test_missing_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing configuration file"):
        load_config(tmp_path / "nope", env={})


def test_invalid_yaml_is_reported(config_dir: Path) -> None:
    write_sources(config_dir, "sources: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(config_dir, env={})


def test_sources_must_be_a_list(config_dir: Path) -> None:
    write_sources(config_dir, "sources:\n  alpha: yes\n")
    with pytest.raises(ConfigError, match="must contain a 'sources' list"):
        load_config(config_dir, env={})


def test_duplicate_source_id_is_rejected(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - {id: alpha, name: A, entrypoint: "https://a.example/f", strategy: rss, priority: 5}
  - {id: alpha, name: B, entrypoint: "https://b.example/f", strategy: rss, priority: 5}
""",
    )
    with pytest.raises(ConfigError, match="duplicate source id"):
        load_config(config_dir, env={})


def test_unknown_category_names_the_offending_source(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - id: alpha
    name: A
    entrypoint: "https://a.example/f"
    strategy: rss
    priority: 5
    category_hint: crypto
""",
    )
    with pytest.raises(ConfigError, match="source alpha"):
        load_config(config_dir, env={})


def test_embedded_date_key_defaults_to_unset(config_dir: Path) -> None:
    """Reading a date out of an embedded payload is opt-in, never the default."""
    write_sources(
        config_dir,
        """
sources:
  - {id: alpha, name: A, entrypoint: "https://a.example/f", strategy: rss, priority: 5}
""",
    )
    assert load_config(config_dir, env={}).sources[0].embedded_date_key is None


def test_embedded_date_key_accepts_a_json_identifier(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - id: alpha
    name: A
    entrypoint: "https://a.example/f"
    strategy: scrapling_static
    priority: 5
    embedded_date_key: publishedOn
""",
    )
    assert load_config(config_dir, env={}).sources[0].embedded_date_key == "publishedOn"


@pytest.mark.parametrize("bad", ['"pub lished"', '"9lives"', '""', '"a[href]"'])
def test_embedded_date_key_rejects_a_non_identifier(config_dir: Path, bad: str) -> None:
    """It names a JSON key, not a selector or a phrase. Bad input fails the load."""
    write_sources(
        config_dir,
        f"""
sources:
  - id: alpha
    name: A
    entrypoint: "https://a.example/f"
    strategy: scrapling_static
    priority: 5
    embedded_date_key: {bad}
""",
    )
    with pytest.raises(ConfigError, match="embedded_date_key"):
        load_config(config_dir, env={})


def test_unknown_strategy_is_rejected(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - {id: alpha, name: A, entrypoint: "https://a.example/f", strategy: telepathy, priority: 5}
""",
    )
    with pytest.raises(ConfigError, match="strategy"):
        load_config(config_dir, env={})


def test_non_http_entrypoint_is_rejected(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - {id: alpha, name: A, entrypoint: "file:///etc/passwd", strategy: rss, priority: 5}
""",
    )
    with pytest.raises(ConfigError, match="entrypoint"):
        load_config(config_dir, env={})


def test_all_sources_disabled_is_rejected(config_dir: Path) -> None:
    write_sources(
        config_dir,
        """
sources:
  - {id: alpha, name: A, entrypoint: "https://a.example/f", strategy: rss, priority: 5, enabled: false}
""",
    )
    with pytest.raises(ConfigError, match="every configured source is disabled"):
        load_config(config_dir, env={})


def test_empty_source_list_is_rejected(config_dir: Path) -> None:
    write_sources(config_dir, "sources: []\n")
    with pytest.raises(ConfigError, match="no sources configured"):
        load_config(config_dir, env={})


def test_unknown_timezone_is_rejected(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text(
        'newsletter:\n  timezone: "Mars/Olympus"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="timezone"):
        load_config(config_dir, env={})


def test_out_of_range_threshold_is_rejected(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text("newsletter:\n  min_score: 500\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="min_score"):
        load_config(config_dir, env={})


def test_the_caps_the_guard_and_the_collapse_have_defaults(config_dir: Path) -> None:
    """Absent from YAML, the settings still hold their documented defaults.

    The similarity threshold is editorial policy, so it is configuration with a
    default rather than a constant.
    """
    settings = load_config(config_dir, env={}).newsletter

    assert settings.max_per_subject == 2
    assert settings.suppress_already_published is True
    assert settings.collapse_similar_events is True
    assert settings.similar_event_threshold == 0.21


def test_a_zero_subject_cap_is_rejected(config_dir: Path) -> None:
    """A cap of zero would publish nothing; removing the cap is ``null``."""
    (config_dir / "newsletter.yaml").write_text(
        f"{MINIMAL_NEWSLETTER}  max_per_subject: 0\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="max_per_subject"):
        load_config(config_dir, env={})


def test_a_non_boolean_reprint_guard_is_reported(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text(
        f"{MINIMAL_NEWSLETTER}  suppress_already_published: occasionally\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="suppress_already_published"):
        load_config(config_dir, env={})


def test_an_unusable_similarity_threshold_is_rejected(config_dir: Path) -> None:
    """Cosine similarity cannot exceed 1, 0 would fold the whole edition, and a
    phrase is not a number. All four fail the load rather than the edition."""
    for value in ("0", "1.5", "-0.2", "quite high"):
        (config_dir / "newsletter.yaml").write_text(
            f"{MINIMAL_NEWSLETTER}  similar_event_threshold: {value}\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="similar_event_threshold"):
            load_config(config_dir, env={})


# --------------------------------------------------------------------------- #
# environment overrides
# --------------------------------------------------------------------------- #


def test_environment_overrides_runtime_settings(config_dir: Path) -> None:
    config = load_config(
        config_dir,
        env={
            "NEWSLETTER_OUTPUT_DIR": "/tmp/editions",
            "NEWSLETTER_DB_PATH": "/tmp/news.sqlite",
            "OPENAI_ANALYZER_MODEL": "gpt-analyzer",
            "OPENAI_EDITOR_MODEL": "gpt-editor",
            "LOG_LEVEL": "debug",
        },
    )
    assert config.runtime.output_dir == Path("/tmp/editions")
    assert config.runtime.db_path == Path("/tmp/news.sqlite")
    assert config.runtime.analyzer_model == "gpt-analyzer"
    assert config.runtime.editor_model == "gpt-editor"
    assert config.runtime.log_level == "DEBUG"


def test_blank_environment_values_do_not_override_defaults(config_dir: Path) -> None:
    config = load_config(config_dir, env={"OPENAI_ANALYZER_MODEL": "   ", "LOG_LEVEL": ""})
    assert config.runtime.analyzer_model == "gpt-4.1-mini"
    assert config.runtime.log_level == "INFO"


def test_yaml_runtime_defaults_are_used_when_env_is_empty(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text(
        MINIMAL_NEWSLETTER + '\nruntime:\n  analyzer_model: "from-yaml"\n', encoding="utf-8"
    )
    config = load_config(config_dir, env={})
    assert config.runtime.analyzer_model == "from-yaml"


def test_environment_wins_over_yaml(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text(
        MINIMAL_NEWSLETTER + '\nruntime:\n  analyzer_model: "from-yaml"\n', encoding="utf-8"
    )
    config = load_config(config_dir, env={"OPENAI_ANALYZER_MODEL": "from-env"})
    assert config.runtime.analyzer_model == "from-env"


# --------------------------------------------------------------------------- #
# concurrency limits
# --------------------------------------------------------------------------- #


def test_concurrency_limits_have_working_defaults(config_dir: Path) -> None:
    runtime = load_config(config_dir, env={}).runtime
    assert runtime.analysis_concurrency == 8
    assert runtime.fetch_concurrency == 6


def test_concurrency_limits_can_be_set_from_yaml_or_the_environment(config_dir: Path) -> None:
    (config_dir / "newsletter.yaml").write_text(
        MINIMAL_NEWSLETTER + "\nruntime:\n  analysis_concurrency: 4\n  fetch_concurrency: 2\n",
        encoding="utf-8",
    )
    assert load_config(config_dir, env={}).runtime.analysis_concurrency == 4
    assert load_config(config_dir, env={}).runtime.fetch_concurrency == 2

    overridden = load_config(config_dir, env={"NEWSLETTER_ANALYSIS_CONCURRENCY": "1"})
    assert overridden.runtime.analysis_concurrency == 1  # the sequential escape hatch
    assert overridden.runtime.fetch_concurrency == 2


@pytest.mark.parametrize("bad", ["0", "-3", "not a number", "8.5"])
def test_an_unusable_concurrency_is_refused_before_the_run_starts(
    config_dir: Path, bad: str
) -> None:
    with pytest.raises(ConfigError, match=r"(?i)concurrency"):
        load_config(config_dir, env={"NEWSLETTER_ANALYSIS_CONCURRENCY": bad})


def test_a_concurrency_beyond_the_ceiling_is_refused(config_dir: Path) -> None:
    """A limit is a limit: 500 threads is a way to get banned, not a way to go fast."""
    with pytest.raises(ConfigError, match="fetch_concurrency"):
        load_config(config_dir, env={"NEWSLETTER_FETCH_CONCURRENCY": "500"})


def test_invalid_log_level_is_rejected(config_dir: Path) -> None:
    with pytest.raises(ConfigError, match="log_level"):
        load_config(config_dir, env={"LOG_LEVEL": "LOUD"})


def test_api_key_is_detected_but_never_exposed(config_dir: Path) -> None:
    config = load_config(config_dir, env={"OPENAI_API_KEY": "sk-super-secret"})
    assert config.runtime.has_openai_key is True
    assert "sk-super-secret" not in repr(config.runtime)
    assert "sk-super-secret" not in str(config.runtime.model_dump(mode="json"))
    assert config.runtime.openai_api_key is not None
    assert config.runtime.openai_api_key.get_secret_value() == "sk-super-secret"


def test_missing_api_key_is_not_an_error(config_dir: Path) -> None:
    config = load_config(config_dir, env={})
    assert config.runtime.has_openai_key is False


# --------------------------------------------------------------------------- #
# editorial policy helpers
# --------------------------------------------------------------------------- #


def test_limit_for_falls_back_to_max_items() -> None:
    settings = NewsletterSettings(max_items=8, section_limits={TopicCategory.AI_MODELS: 3})
    assert settings.limit_for(TopicCategory.AI_MODELS) == 3
    assert settings.limit_for(TopicCategory.AI_VIDEO) == 8


def test_ordered_categories_respects_order_and_exclusions() -> None:
    settings = NewsletterSettings(
        section_order=[TopicCategory.AI_MODELS, TopicCategory.YOUTUBE_PLATFORM],
        excluded_categories=[TopicCategory.OTHER, TopicCategory.AI_BUSINESS],
    )
    ordered = settings.ordered_categories()
    assert ordered[:2] == [TopicCategory.AI_MODELS, TopicCategory.YOUTUBE_PLATFORM]
    assert TopicCategory.OTHER not in ordered
    assert TopicCategory.AI_BUSINESS not in ordered
    assert set(ordered) == {
        TopicCategory.AI_MODELS,
        TopicCategory.YOUTUBE_PLATFORM,
        TopicCategory.YOUTUBE_MONETIZATION,
        TopicCategory.KIDS_CONTENT,
        TopicCategory.AI_VIDEO,
    }


def test_section_titles_have_defaults_for_every_category() -> None:
    settings = NewsletterSettings()
    for category in TopicCategory:
        assert settings.title_for(category)


def test_negative_section_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="section_limits"):
        NewsletterSettings(section_limits={TopicCategory.AI_MODELS: -1})


# --------------------------------------------------------------------------- #
# the reader submission form
# --------------------------------------------------------------------------- #


def test_no_submission_form_is_configured_by_default() -> None:
    """Without an address, the edition prints no call to action at all."""
    assert SubmissionSettings().form_url is None
    assert SubmissionSettings(form_url="   ").form_url is None


@pytest.mark.parametrize(
    "bad", ["javascript:alert(1)", "/submit", "submissions.example/submit", "mailto:a@b.example"]
)
def test_a_submission_form_url_that_cannot_be_published_is_rejected(
    config_dir: Path, bad: str
) -> None:
    """It is printed as a link in the edition, so it is validated at load time."""
    (config_dir / "newsletter.yaml").write_text(
        MINIMAL_NEWSLETTER + f'\nsubmissions:\n  form_url: "{bad}"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="form_url"):
        load_config(config_dir, env={})


@pytest.mark.parametrize("bad", ["-1", "not-a-number", "999"])
def test_a_reserved_slot_count_that_makes_no_sense_is_refused(config_dir: Path, bad: str) -> None:
    """It decides how much of the edition is given away, so it fails at load time."""
    (config_dir / "newsletter.yaml").write_text(
        MINIMAL_NEWSLETTER + f"\nsubmissions:\n  reserved_slots: {bad}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="reserved_slots"):
        load_config(config_dir, env={})


# --------------------------------------------------------------------------- #
# coverage floors and the analysis pool
# --------------------------------------------------------------------------- #

OWN_BEAT = [TopicCategory.YOUTUBE_PLATFORM, TopicCategory.KIDS_CONTENT]


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param(
            {"coverage_floors": {"own_beat": {"categories": [], "minimum": 1}}},
            "categories",
            id="a floor with no categories can never be satisfied",
        ),
        pytest.param(
            {"coverage_floors": {"own_beat": {"categories": [TopicCategory.OTHER], "minimum": 1}}},
            "excluded_categories",
            id="a floor on a category the edition never prints",
        ),
        pytest.param(
            {
                "max_items": 3,
                "coverage_floors": {"own_beat": {"categories": OWN_BEAT, "minimum": 4}},
            },
            "exceeds max_items",
            id="a floor larger than the edition",
        ),
        pytest.param(
            {"analysis_pool_min": 60, "analysis_pool_max": 50},
            "analysis_pool_max",
            id="a ceiling below its own first batch",
        ),
    ],
)
def test_an_unsatisfiable_policy_is_refused_before_the_run_starts(
    settings: dict[str, object], message: str
) -> None:
    """Each of these would fail silently at run time -- a floor permanently unmet,
    or a batch loop that can never take its first batch."""
    with pytest.raises(ValueError, match=message):
        NewsletterSettings(**settings)


@pytest.mark.parametrize(("configured", "expected"), [(0, None), (None, None), (50, 50)])
def test_zero_and_empty_both_mean_no_analysis_cap(
    configured: int | None, expected: int | None
) -> None:
    """YAML has no tidy way to say "off" for a number, so both spellings work."""
    assert NewsletterSettings(analysis_pool_max=configured).analysis_pool_cap == expected


def test_the_shipped_configuration_carries_the_owners_own_beat_floor() -> None:
    """The floor is editorial policy, so the real config is what states it."""
    floors = load_config(REAL_CONFIG_DIR, env={}).newsletter.coverage_floors

    assert floors["own_beat"].minimum == 4
    assert set(floors["own_beat"].categories) == {
        TopicCategory.YOUTUBE_PLATFORM,
        TopicCategory.YOUTUBE_MONETIZATION,
        TopicCategory.KIDS_CONTENT,
    }


# --------------------------------------------------------------------------- #
# the minimum edition, and the caps it is allowed to relax
# --------------------------------------------------------------------------- #


def test_an_explicit_minimum_above_the_edition_is_refused() -> None:
    """Otherwise every edition is permanently, inexplicably short."""
    with pytest.raises(ValueError, match="min_items 9 exceeds max_items 8"):
        NewsletterSettings(max_items=8, min_items=9)


def test_the_default_minimum_shrinks_to_fit_a_smaller_newspaper() -> None:
    """A three-story edition is not misconfigured; it simply cannot carry six."""
    assert NewsletterSettings(max_items=3).min_items == 3
    assert NewsletterSettings().min_items == 6


def test_relaxing_moves_the_rationing_caps_and_nothing_else() -> None:
    """The safety property the whole feature rests on, pinned field by field."""
    settings = NewsletterSettings(
        max_items=10,
        min_score=62,
        max_per_source=2,
        max_per_subject=2,
        section_limits={TopicCategory.AI_MODELS: 3},
        excluded_categories=[TopicCategory.OTHER],
    )

    relaxed = settings.relaxed(2)

    assert relaxed.section_limits == {TopicCategory.AI_MODELS: 5}
    assert relaxed.max_per_source == 4
    assert relaxed.max_per_subject == 4
    assert relaxed.model_dump(
        exclude={"section_limits", "max_per_source", "max_per_subject"}
    ) == settings.model_dump(exclude={"section_limits", "max_per_source", "max_per_subject"})


def test_relaxing_reaches_a_fixed_point_so_the_loop_terminates() -> None:
    """No cap rises above ``max_items``, which is where it stops constraining."""
    settings = NewsletterSettings(max_items=10, max_per_source=2, max_per_subject=None)

    assert settings.relaxed(50) == settings.relaxed(9)
    assert settings.relaxed(50).max_per_source == 10
    assert settings.relaxed(50).max_per_subject is None  # an absent cap stays absent
    assert settings.relaxed(0) is settings


def test_the_shipped_configuration_rations_a_ten_story_edition() -> None:
    """The caps were calibrated for eight slots; these are the ten-slot ones."""
    newsletter = load_config(REAL_CONFIG_DIR, env={}).newsletter

    assert newsletter.max_items == 10
    assert newsletter.min_items == 6
    assert newsletter.section_limits == {
        TopicCategory.YOUTUBE_PLATFORM: 4,
        TopicCategory.YOUTUBE_MONETIZATION: 4,
        TopicCategory.KIDS_CONTENT: 4,
        TopicCategory.AI_VIDEO: 3,
        TopicCategory.AI_MODELS: 3,
        TopicCategory.AI_BUSINESS: 3,
    }
    # No single topic may hold half the paper, and no two may fill it.
    assert max(newsletter.section_limits.values()) * 2 <= newsletter.max_items
