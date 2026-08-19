# Implementation status

> Durable coordinator state. Update this at the end of every stage. After a context
> compaction or a resumed session, this file is the source of truth for where work stands.

## Current stage

**Stage 8 — automation, CI, final quality pass — COMPLETE**
**Stage 9 — reader submissions — COMPLETE** (added scope, requested during Stage 8)
**Stage 10 — Spanish edition for Leader Entertainment — COMPLETE** (added scope)

The engine has run live against real sources and a real key, and now publishes in Spanish
for a named audience (ADR-0032). What remains is editorial judgment over time, not build
work.

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
| 10 | Spanish edition, audience rubric (added scope) | complete | 2026-08-19 |

## Current objective

Steps 1-3 below are done. The edition now runs live, in Spanish, for Leader
Entertainment — a Latin American children's YouTube company moving into AI production.

1. ~~Run with a real key and read the edition end to end.~~ Done, repeatedly. Live runs
   found three defects fixtures could not: gzip despite `Accept-Encoding: identity`
   (ADR-0025), a headline taken from the account name rather than the post, and a thinness
   probe that measured the page chrome instead of the article (ADR-0031).
2. ~~Judge the prompts and bump to v2 if they need changing.~~ Done — `article_analyzer_v2`
   and `newsletter_editor_v2` write Spanish and rate for this company (ADR-0032).
3. ~~Judge the sources editorially and tune `priority`.~~ Done — priority is a trust tier
   and no source may fill an edition (ADR-0030); two kids/creator feeds were added and
   verified live, and `min_score` was recalibrated to the v2 distribution (ADR-0032).
4. Decide whether to enable the weekly cron, and where the edition should go afterwards.
5. Read two or three consecutive weekly editions before touching the rubric again — one
   edition cannot distinguish a bad threshold from a slow news week.

## Last successful validation

`2026-08-19` — full gate after the entity-fidelity guard (ADR-0033):

| Check | Result |
|-------|--------|
| `ruff check .` / `ruff format --check .` | pass (61 files formatted) |
| `python -m pytest` | **542 passed** (was 515; +27 for the guard) |
| `python scripts/validate_repo.py` | OK, 9 checks, 0 warnings |
| `python scripts/audit_acceptance.py` | 20 criteria: 18 mechanical pass, 2 need a human |
| CI workflow | valid, declares no secret, re-runs the suite with a deliberately invalid key |
| Weekly workflow | valid; uploads an artifact and publishes nothing |
| live run, real key | 11 sources, 69 discovered, 60 in window, **8 published** in Spanish |
| submitted link, end to end | fetched, enriched from its own link, assessed, recorded |

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
    dedupe.py               three-pass dedupe + event collapse (key, then text: ADR-0034)
    scoring.py              the 0-100 formula, breakdown, deterministic ranking
    selection.py            threshold, caps, max_items, lead story, sections
  persistence/
    sqlite.py               articles, assessments, editions, edition_items, run_history
  intelligence/
    editor.py               EditorialPayload, build_edition (pure), NewsletterEditor
    schemas.py              AssessmentPayload (wire) + Python-side bound enforcement
    fidelity.py             entity-fidelity guard: model prose vs trusted source (ADR-0033)
    client.py               StructuredClient: no tools, store=False, bounded retries
    analyzer.py             ArticleAnalyzer: versioned prompt, cache, failure isolation
    prompts/article_analyzer_v{1,2}.md, prompts/newsletter_editor_v{1,2}.md  (v2 live)
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

None outstanding. One shipped defect was found and closed: the W34 edition published the
corrupted brand "UTube" where the source said "YouTube". A full fabrication audit of all eight
stories found **no invented facts** — every URL, date, figure and named entity traced to the
stored source — so this was entity corruption, not hallucination. It is now caught
deterministically by `intelligence/fidelity.py` (ADR-0033). The published `output/2026-W34/`
artifacts still contain the defect; they were not regenerated, because re-running would return
the same cached assessment.

Two lesser findings from the same audit remain open and are editorial, not mechanical:

- "mil 500 millones" (W34, Roblox story) is Mexican-press numeral style rather than the
  neutral Latin American Spanish the edition commits to. A neutral-numeral instruction belongs
  in the editor prompt, which means a version bump — deliberately deferred, since the editor
  prompt version is separate from the analyzer's and should be batched with any other v3 work.
- One `why_it_matters` overreached: the source described OpenAI monitoring its **internal**
  workloads, and the Spanish prose reads as stricter monitoring of model *usage*. The guard
  cannot catch this class — it is unsupported inference, not a corrupted string.

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
- `min_score: 62` is calibrated against one week of v2 output. It is the single number
  standing between "a thin edition" and "an edition padded with filler", and one week is
  not enough evidence to defend it. Re-read it after a few editions.
- The v2 rubric has been exercised on roughly 60 articles from 11 sources. Its behaviour on
  a slow news week — whether it publishes three good stories or eight weak ones — is
  unknown.
- Source *editorial quality* remains a human call. The entrypoints are verified reachable
  and parseable (ADR-0025) and the two kids/creator feeds were added because the beat was
  missing, but nobody has yet judged several consecutive editions as a reader.

- The `scrapling_dynamic` / `scrapling_stealth` paths have no browser implementation yet,
  only the injection point and a test proving the point works. Implement when a source
  actually needs it (ADR-0012).
- Selectors for the two disabled scraping sources are placeholders, documented in
  `config/sources.yaml` but not verified against the live DOM.

## Next concrete actions

1. Read the next two editions as a reader, in Spanish, before changing any number.
2. ~~Spot-check summaries against the source articles, looking for fabrication.~~ Done for
   W34: no fabrication; one corrupted entity, now guarded (ADR-0033).
3. Add `OPENAI_API_KEY` to GitHub Secrets. **The weekly cron is already enabled in
   `weekly-newsletter.yml` (`0 6 * * 1`) and goes live the moment `main` reaches GitHub** —
   it was dormant only because the repository had never been pushed. Without the secret the
   scheduled run will fail rather than publish anything.
4. Watch the guard's false-positive rate on the next two live runs. It drops stories silently
   from the reader's point of view (the manifest records every drop, the edition does not), so
   an edition that is unexpectedly thin should be checked against
   `run_manifest.json` before `min_score` is blamed.
5. Optional: a GitHub issue form that calls `newsletter submit`, so proposing a story does
   not require a terminal.
