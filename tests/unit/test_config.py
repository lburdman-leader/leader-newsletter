"""Unit tests for configuration loading.

A bad configuration must fail the run before any network call, with a message
that says which file and which field is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from newsletter.config import AppConfig, ConfigError, NewsletterSettings, load_config
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


def test_repository_configuration_is_valid() -> None:
    config = load_config(REAL_CONFIG_DIR, env={})
    assert config.sources
    assert config.enabled_sources
    assert config.newsletter.min_score == 62  # recalibrated for the v2 rubric
    assert config.newsletter.masthead == "Leader Intelligence Semanal"
    assert config.newsletter.max_items == 8


def test_repository_sources_have_unique_ids_and_known_categories() -> None:
    config = load_config(REAL_CONFIG_DIR, env={})
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
