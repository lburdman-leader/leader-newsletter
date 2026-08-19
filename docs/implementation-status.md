# Implementation status

> Durable coordinator state. Update this at the end of every stage. After a context
> compaction or a resumed session, this file is the source of truth for where work stands.

## Current stage

**Stage 8 — automation, CI, final quality pass — COMPLETE**
**Stage 9 — reader submissions — COMPLETE** (added scope, requested during Stage 8)

Next: a live run with a real `OPENAI_API_KEY`. Everything else is built.

## Completed stages

| Stage | Name | Status | Date |
|-------|------|--------|------|
| 0 | Claude development harness | complete | 2026-08-18 |
| 1 | Foundation, config, models, CLI | complete | 2026-08-18 |
| 2 | Ingestion (RSS + Scrapling) | complete | 2026-08-18 |
| 3 | Normalization, filtering, dedupe, SQLite | complete | 2026-08-18 |
| 4 | OpenAI structured intelligence | complete | 2026-08-18 |
| 5 | Scoring and selection | complete | 2026-08-18 |
| 6 | Newspaper editor and rendering | complete | 2026-08-18 |
| 7 | End-to-end vertical slice | complete | 2026-08-18 |
| 8 | Automation, CI, final quality pass | complete | 2026-08-18 |
| 9 | Reader submissions (added scope) | complete | 2026-08-18 |

## Current objective

The build is complete. What remains is judgment that only a live run can inform:

1. Run `python -m newsletter run` with a real key and read the edition end to end.
2. Judge the prompts against real output — the rubric wording, the summary/interpretation
   split, the executive brief — and bump `article_analyzer_v1` / `newsletter_editor_v1` to
   v2 if they need changing.
3. Judge the nine sources editorially, and tune `priority` per source.
4. Decide whether to enable the weekly cron, and where the edition should go afterwards.

## Last successful validation

`2026-08-18` — full gate across Stages 8 and 9:

| Check | Result |
|-------|--------|
| `ruff check .` / `ruff format --check .` | pass |
| `python -m pytest` | **485 passed** in 3.3s |
| `python scripts/validate_repo.py` | OK, 9 checks, 0 warnings |
| `python scripts/audit_acceptance.py` | 20 criteria: 18 mechanical pass, 2 need a human |
| CI workflow | valid, declares no secret, re-runs the suite with a deliberately invalid key |
| Weekly workflow | valid; uploads an artifact and publishes nothing |
| live `run --dry-run` | 9/9 sources, 80 articles discovered, 66 after deduplication |
| submitted link, end to end | fetched, assessed, published, status recorded |

## What exists now

```text
pyproject.toml              package metadata, ruff (line length 100), pytest (pythonpath=src)
config/sources.yaml         11 sources (9 RSS enabled, 2 scrapling disabled) — PROVISIONAL
config/newsletter.yaml      editorial policy + runtime defaults
src/newsletter/
  __init__.py               __version__, SCHEMA_VERSION
  __main__.py               python -m newsletter
  models.py                 enums + 16 Pydantic models; URL and timezone invariants
  config.py                 strict YAML loading, env overrides, ConfigError
  context.py                RunContext (run id, window, manifest, edition dir)
  logging_setup.py          stderr logging + stdout run narrative
  cli.py                    run / validate / sources, stable exit codes
  ingestion/
    base.py                 SourceAdapter protocol, errors, factory, failure isolation
    http.py                 HttpClient protocol + UrllibHttpClient (URL-scheme boundary)
    dates.py                date parsing that returns None instead of guessing
    rss.py                  RssAdapter (feedparser)
    scrapling.py            ScraplingAdapter (Selector; static now, browser gated)
  normalization/
    urls.py                 canonicalize_url (publishable) + dedupe_key (comparison)
    article.py              untrusted HTML -> NormalizedArticle, hashing, identity
    filtering.py            authoritative date-window gate (AC6)
  ranking/
    dedupe.py               three-pass dedupe + post-analysis event collapse
    scoring.py              the 0-100 formula, breakdown, deterministic ranking
    selection.py            threshold, caps, max_items, lead story, sections
  persistence/
    sqlite.py               articles, assessments, editions, edition_items, run_history
  intelligence/
    editor.py               EditorialPayload, build_edition (pure), NewsletterEditor
    schemas.py              AssessmentPayload (wire) + Python-side bound enforcement
    client.py               StructuredClient: no tools, store=False, bounded retries
    analyzer.py             ArticleAnalyzer: versioned prompt, cache, failure isolation
    prompts/article_analyzer_v1.md, prompts/newsletter_editor_v1.md
  pipeline.py               the state machine: one run, injectable collaborators
  ingestion/submissions.py  reader-submission gate, adapter and identity
  rendering/
    renderer.py             link validation, Jinja env, artifact writing
    templates/newsletter.html.j2, templates/newsletter.md.j2
tests/conftest.py           FakeHttpClient, fixtures, autouse no-network guard
tests/fixtures/sources/     example-feed (RSS), example-site (index + article HTML)
tests/fixtures/             expected_newsletter.{md,json,html} golden edition
tests/fixtures/integration/ alpha (RSS), beta (scraped, syndicated copy), gamma (down)
tests/integration/          21 end-to-end tests, including submitted links
.github/workflows/          ci.yml (no secrets), weekly-newsletter.yml (artifact only)
scripts/audit_acceptance.py the 20 PRD acceptance criteria, checked mechanically
scripts/refresh_expected_edition.py  regenerate goldens (+ --sample to browse one)
tests/unit/                 456 tests
```

Key invariants now enforced in code, not just documented:

- `validate_public_url` rejects `javascript:`, `data:`, `file:`, `ftp:`, relative and
  host-less URLs — the single choke point protecting AC4/AC13;
- every timestamp field is `AwareDatetime`; `DateWindow.contains` refuses naive input;
- `ArticleAssessment` has no score field at all, so the model cannot emit one (AC8);
- value models are frozen and `extra="forbid"`, so schema drift fails loudly;
- `RuntimeSettings.openai_api_key` is a `SecretStr`, asserted absent from repr and dumps.

## Known failures

None.

## Environment (installed 2026-08-18)

Dependencies are installed into the user-level Python 3.14.4 interpreter (no virtualenv),
and the package itself via `pip install -e . --no-deps`, so `python -m newsletter` and the
`newsletter` console script work without `PYTHONPATH`.

| Package | Version | Note |
|---------|---------|------|
| scrapling | 0.4.14 | cp314 wheel; exposes `Fetcher`, `DynamicFetcher`, `StealthyFetcher`, `Selector` |
| feedparser | 6.0.14 | RSS/Atom adapter (Stage 2) |
| openai | 3.2.0 | `client.responses.parse` is the Stage 4 entry point |
| lxml | 6.1.1 | scrapling dependency |
| pydantic / pyyaml / jinja2 | 2.13.4 / 6.0.3 / 3.1.6 | |
| pytest / ruff | 8.4.2 / 0.15.15 | |

Pins tightened to what was exercised: `openai>=3.0,<4`, `scrapling>=0.4,<0.5` (ADR-0011).

## Pending technical debt

- **Scrapling browser backends are not installed.** Static fetching works now; dynamic and
  stealth strategies additionally need browser binaries via `scrapling install`. Only do
  this if Stage 2 finds a source that genuinely requires a browser.
- **No virtualenv.** Packages went into the user-level interpreter, as requested. If this
  machine later needs isolation, a venv plus the same `pip install -e ".[dev]"` reproduces
  the environment.
- **Source entrypoints are unverified** (ADR-0009). Stage 2 verifies each via
  `/add-source` and the `source-researcher` subagent.
- **Permission rule syntax is unverified at runtime** — tighten on the first false prompt.
- Live runs have never reached the model: every artifact so far used a fake SDK. The
  first real `python -m newsletter run` needs `OPENAI_API_KEY` and will be the first test
  of prompt quality, cost and the editorial output.
- Event-fingerprint collapse is implemented but has never seen real analyzer output;
  its behaviour on live data is unproven until a run with a key.
- No live OpenAI call has ever been made. The request shape is asserted against the SDK
  signature and mocks, but a real call is only exercised by the optional smoke test in
  Stage 7.
- Source *editorial quality* is still unproven: the entrypoints are verified as reachable
  and parseable (ADR-0025), but no human has judged whether these nine sources produce a
  good newspaper.

- The `scrapling_dynamic` / `scrapling_stealth` paths have no browser implementation yet,
  only the injection point and a test proving the point works. Implement when a source
  actually needs it (ADR-0012).
- Selectors for the two disabled scraping sources are placeholders, documented in
  `config/sources.yaml` but not verified against the live DOM.

## Next concrete actions

1. `python -m newsletter run` with a real key; read the whole edition before anything else.
2. Compare the model's summaries against the source articles, looking for fabrication.
3. Tune source `priority` values and `min_score` against real output.
4. Add `OPENAI_API_KEY` to GitHub Secrets and enable the weekly cron once satisfied.
5. Optional: a GitHub issue form that calls `newsletter submit`, so proposing a story does
   not require a terminal.
