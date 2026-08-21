# Architecture

> This file describes the architecture **as implemented**, not the target architecture.
> The target lives in `PRD_Weekly_Intelligence_Newspaper_Claude_Code.md`. Sections marked
> *(planned)* are not built yet and must be updated as each stage lands.

## Two separate architectures

| | Development time | Runtime |
|---|---|---|
| Orchestrator | Claude Code (coordinator session) | plain Python |
| Specialists | Claude Code subagents | none |
| Intelligence | — | OpenAI Responses API, strict schemas |
| Dependency | Claude Code | **must not depend on Claude Code** |

The production newsletter runs as ordinary Python. Nothing in `src/` may import or
require Claude Code.

## Development harness (implemented — Stage 0)

```text
.claude/
  settings.json          permissions (allow / ask / deny) + hooks
  agents/
    source-researcher.md read-only source investigation (haiku, no repo writes)
    quality-auditor.md   read-only independent stage audit (inherit model)
  skills/
    add-source/          add or modify a source, end to end
    validate-stage/      run the current stage gate
    final-audit/         acceptance criteria + independent review
scripts/
  claude_guard.py        PreToolUse hook: blocks unsafe / secret-exposing Bash
  validate_repo.py       Stop hook + manual: fast repo integrity validator
docs/
  implementation-status.md   durable stage state (survives compaction)
  architecture.md            this file
  decisions.md               ADR-style decision log
CLAUDE.md                coordinator contract and non-negotiable rules
```

### Permission model

- `defaultMode: acceptEdits` — routine reads, searches, edits, file creation, tests,
  lint and subagent delegation never prompt.
- `allow` — read-only shell, read-only git, pytest, ruff, the project CLI in safe modes.
- `ask` — git writes, remote/GitHub actions, dependency installs, deletions, network
  downloads, and every `Skill` invocation.
- `deny` — dotenv files, private keys, `~/.ssh`, `~/.aws`, gcloud config, force push,
  piping downloads into a shell.
- `Bash` is never allowed globally; each command family is enumerated.

### Hooks

| Hook | Matcher | Command | Effect |
|------|---------|---------|--------|
| `PreToolUse` | `Bash` | `python scripts/claude_guard.py` | exit 2 blocks the command and returns the reason to Claude |
| `Stop` | — | `python scripts/validate_repo.py --quiet` | fast integrity check; non-blocking on failure |

Hooks stay fast and deterministic. Test suites and pipeline runs belong to
`/validate-stage` and CI, never to a hook.

### Subagent handoff

Every delegated task returns exactly this structure; the coordinator absorbs the
conclusions and never pastes raw subagent logs into durable context:

```text
TASK / RESULT / FILES INSPECTED / FILES CHANGED /
DECISIONS - ASSUMPTIONS / VALIDATION / RISKS - OPEN ITEMS / RECOMMENDED NEXT ACTION
```

## Application skeleton (implemented — Stage 1)

```text
src/newsletter/
  __init__.py         __version__, SCHEMA_VERSION (part of the assessment cache key)
  __main__.py         python -m newsletter
  models.py           enums + domain models; owns the URL and timezone invariants
  config.py           strict YAML loading, environment overrides, ConfigError
  context.py          RunContext: run id, window, manifest, edition directory
  concurrency.py      bounded thread pool that returns results in input order
  logging_setup.py    stderr diagnostics + stdout run narrative
  cli.py              run / validate / sources
```

**Contracts.** Value objects (`SourceConfig`, `DateWindow`, `NormalizedArticle`,
`ArticleAssessment`, `NewsletterItem`, `NewsletterEdition`, ...) inherit `ValueModel`:
frozen and `extra="forbid"`, so schema drift fails at the boundary instead of leaking.
`RunManifest` inherits `MutableModel` because it accumulates during a run.

**Two invariants live in the models rather than in their callers**, because both protect
published output:

| Invariant | Enforcement |
|-----------|-------------|
| every published URL is `http`/`https` | `PublicUrl` -> `validate_public_url` |
| every timestamp is timezone-aware | `AwareDatetime` + `DateWindow.contains` |

`ArticleAssessment` deliberately has **no score field**, so the analyzer cannot emit one.

**Configuration.** Behaviour is YAML (`config/newsletter.yaml`, `config/sources.yaml`).
Only secrets, paths, model names and log level come from the environment, and the
environment wins over YAML. The API key is a `SecretStr`, never a plain string.

**Date window.** `[start, end)`, half-open. Default `rolling` = `[now - N days, now)`;
`completed_days` uses whole local days. `--from/--to` are inclusive calendar dates. The
window is computed once in the CLI and carried in `RunContext` — no stage consults the
clock (ADR-0007).

**CLI exit codes.** 0 success · 1 configuration or runtime error · 2 usage · 3 not
implemented in this stage (ADR-0008).

## Ingestion (implemented — Stage 2)

```text
src/newsletter/ingestion/
  base.py        SourceAdapter protocol, error hierarchy, factory, failure isolation
  http.py        HttpClient protocol + UrllibHttpClient (also the URL-scheme boundary)
  dates.py       date parsing that returns None instead of guessing
  rss.py         RssAdapter    (feedparser)
  scrapling.py   ScraplingAdapter (Scrapling Selector; static now, browser gated)
```

**One interface for every source.** `discover(window) -> list[DiscoveredArticle]` and
`fetch(article) -> RawArticle`. feedparser structures and Scrapling selectors stop at
this boundary — a test asserts that only `newsletter.*` types come out.

**Transport is injected.** Adapters take an `HttpClient`; production uses
`UrllibHttpClient`, tests use a fake driven by `tests/fixtures/sources/`. Scrapling
provides parsing, not transport, because its fetchers live behind the
`scrapling[fetchers]` extra (ADR-0012). The transport re-validates the URL scheme on the
request *and* on the redirect target, so a `file://` link inside an untrusted feed never
reaches the network layer.

**Strategy support.** `rss` and `scrapling_static` work today. `scrapling_dynamic` and
`scrapling_stealth` raise `UnsupportedStrategyError` with install instructions unless a
browser `page_loader` is injected — an extension point, not a silent failure.

**Failure isolation (AC10).** `ingest_all` walks sources in the caller's deterministic
order:

| Failure | Effect |
|---------|--------|
| adapter cannot be built | source skipped, error recorded at `LOAD_CONFIG`, run continues |
| discovery fails | source marked failed, error recorded at `DISCOVER`, other sources continue |
| one article fails to fetch | article skipped, error recorded at `FETCH`, source still succeeds |

Every one of those paths writes a `RunError` into the `RunManifest`. Nothing is dropped
silently.

**Concurrency (ADR-0037).** `ingest_source` fetches up to `runtime.fetch_concurrency`
articles of one source at once (default 6, the per-host budget a browser allows itself);
sources themselves stay sequential, so `sources_attempted` / `sources_succeeded` /
`sources_failed` remain the product of a loop rather than of a race. Results are read back
in discovery order and every failure is recorded from the calling thread, so the fetched
sequence and the manifest are the same as at one request at a time. `1` is that.

**Dates.** A known date outside the window drops the candidate; an unreadable date keeps
it with `published_at_hint = None` for Stage 3 to resolve (ADR-0013).

## Deterministic data layer (implemented — Stage 3)

```text
src/newsletter/normalization/
  urls.py        canonicalize_url (publishable) + dedupe_key (comparison) + same_site
  article.py     untrusted HTML -> NormalizedArticle; hashing and article identity
  filtering.py   the authoritative date-window gate (AC6)
src/newsletter/ranking/
  dedupe.py      three-pass deterministic deduplication
src/newsletter/persistence/
  sqlite.py      sources, articles, assessments, editions, edition_items, run_history
```

**Two URL forms, on purpose** (ADR-0014). The published canonical only strips what cannot
change the destination; the comparison key additionally folds `www.`, trailing slashes and
`index.html`. `article_id = sha256(dedupe_key)[:16]`, so one story keeps one identity
across URL variants, runs and cache lookups.

**Normalization invariants.** No date, no article — an undated page is rejected with a
recorded error rather than dated "now". A page-declared canonical URL is honoured only
within the same site, so untrusted markup cannot hijack attribution (ADR-0015). Extracted
text is data; nothing in it is ever an instruction.

**Extraction order** (first hit wins, all deterministic):

| Field | Sources tried |
|-------|---------------|
| title | `og:title` → `twitter:title` → JSON-LD `headline` → `h1` → `<title>` → discovery hint |
| date | `article:published_time` → `datePublished` meta → JSON-LD → `<time datetime>` → discovery hint |
| author | `author` meta → JSON-LD `author.name` → `[rel=author]` / `.byline` |
| text | configured `selectors.content` → `article` → `main` → `[itemprop=articleBody]` → `body` |

**Date filter (AC6).** `filter_by_window` partitions into inside/outside against the
half-open window. Discovery-level filtering is only an optimisation; this is the gate.

**Deduplication.** Runs before any model call — canonical URL, then content hash, then
normalized title (skipped for titles under 15 characters, which are not evidence). The
survivor is chosen by rule: highest source priority, then earliest publication, then
lowest article id, so the result does not depend on input order. Semantic collapse of
*different* stories about one event needs the analyzer event fingerprint and belongs after
analysis.

**Persistence.** Indexed columns plus the full validated JSON payload (ADR-0016), so
records round-trip exactly while staying queryable. Assessment cache identity is
`content_hash:prompt_version:schema_version:model` — changing any component is a miss, so
a prompt edit can never reuse a stale judgment. Traceability is a join:
`edition_items → articles → sources`.

## Semantic layer (implemented — Stage 4)

```text
src/newsletter/intelligence/
  schemas.py     AssessmentPayload (wire) + bound enforcement into ArticleAssessment
  client.py      StructuredClient: one request, one schema, bounded retries
  analyzer.py    ArticleAnalyzer: prompt + cache + per-article failure isolation
  prompts/article_analyzer_v1.md, article_analyzer_v2.md   (v2 is live; see ADR-0032)
```

**Two models for one concept** (ADR-0017). Strict Structured Outputs rejects `minimum`,
`maximum` and `maxItems`, so the wire model carries no constraints and the bounds are
enforced in Python afterwards. A rating outside 0-5 is rejected, never clamped. A test
asserts the generated schema contains no unsupported keyword.

**What the model is not given** (ADR-0019):

| Capability | Status |
|------------|--------|
| tools / function calling | never passed |
| filesystem, shell, network | none |
| remote retention | `store=False` |
| conversation history | none — each article is independent |
| control over the pipeline | none: no score, no selection, no ordering |

**Trust boundary.** Application instructions go in the `instructions` field; untrusted
article text goes in `input`, inside `<<<BEGIN UNTRUSTED ARTICLE>>>` markers. The boundary
is structural, not a formatting convention, and a test feeds a hostile payload to prove it
stays inside the block.

**Retries** (ADR-0018). The SDK is built with `max_retries=0`; the wrapper owns one
budget. Timeout / connection / rate-limit / 5xx retry with exponential backoff; refusal,
auth, permission and bad-request fail immediately. Exhaustion raises a typed
`ModelTimeout` or `ModelUnavailable`, which `analyze_all` records per article. A rate limit
additionally honours the server's `Retry-After` (capped at 30s) and jitters the wait, so a
batch of concurrent calls throttled together does not come back in lockstep (ADR-0037).

**Cache.** Identity is `content_hash:prompt_version:schema_version:model`. A cache hit
skips the call entirely and increments `llm_cache_hits`; a prompt edit invalidates every
judgment it produced, while the old records stay in the database for audit.

**Concurrency (ADR-0037).** `analyze_all` runs up to `runtime.analysis_concurrency` model
calls at once (default 8) in three phases: cache reads on the calling thread, the model
call and nothing else in the workers, then manifest, cache writes and results back on the
calling thread in input order. The thread boundary sits *before* the database on purpose —
a SQLite connection belongs to the thread that opened it — and the ordering rules live in
`newsletter/concurrency.py`, so completion order never reaches an artifact.

## Scoring and selection (implemented — Stage 5)

```text
src/newsletter/ranking/
  scoring.py     the score formula, ScoreBreakdown, rank_all, ranking_key
  selection.py   threshold, category caps, max_items, lead story, sections
  dedupe.py      + collapse_duplicate_events (post-analysis, event fingerprint)
```

**The formula, in one place** (AC8):

```text
topic_relevance x6 (0-30) + business_impact x5 (0-25) + novelty x4 (0-20)
+ actionability x3 (0-15) + source_priority (0-10)  =  0-100
```

Weights are constants in code; thresholds and caps are configuration (ADR-0020).
`ArticleAssessment` has no score field, so the model cannot express one even if asked.

**Selection rules**, applied to articles sorted `(-score, published_at, article_id)`:

| Rule | Rejection reason |
|------|------------------|
| excluded category (`other` by default) | `category_excluded` |
| score below `min_score` | `below_threshold` |
| per-category cap reached | `category_limit` |
| `max_items` reached | `max_items` |
| same event as a higher-scoring story | `duplicate_event` |

Every tie is broken by data, so the result is independent of input order — asserted by
running the same set forwards and reversed (AC9). Every rejection is recorded, so a thin
edition is explainable from the run report rather than mysterious.

**Reserved slots** (ADR-0040). Reader submissions are seated before anything is earned:
three submissions mean three reserved stories and seven earned ones out of `max_items`.
A reserved slot bypasses only what *rations* slots between competing stories —
`min_score`, `max_per_source`, `max_per_subject`, `section_limits` — and nothing that
protects correctness: the three deduplication passes, both collapse passes, cross-edition
suppression, the entity-fidelity guard, `excluded_categories` and `max_items` all still
apply. A reserved story counts against the caps for everything below it, so the earned
slots stay diverse. `submissions.reserved_slots: 0` restores the earlier behaviour exactly.

**Lead story and sections** (ADR-0021). The lead is the best selected story by
`ranking_key` — *not* the first in the printed order, since reserved slots are seated
first and being submitted is not an argument for leading the edition. It is excluded from
its own section so it is not printed twice; a section that would hold only the lead
disappears. Sections follow `section_order`. The editorial model may reword a headline,
never change the line-up.

**Event collapse.** `collapse_duplicate_events` uses the analyzer's
`subject|action|object|date` fingerprint to merge two outlets covering one announcement,
keeping the higher-scoring story. An incomplete fingerprint is never treated as a
duplicate. Switchable via `collapse_events`.

## Editorial synthesis and publication (implemented — Stage 6)

```text
src/newsletter/intelligence/
  editor.py      EditorialPayload wire schema, build_edition (pure), NewsletterEditor
  prompts/newsletter_editor_v1.md, newsletter_editor_v2.md  (v2 is live)
src/newsletter/rendering/
  renderer.py    link validation, Jinja environment, artifact writing
  templates/newsletter.html.j2   the newspaper
  templates/newsletter.md.j2     the same edition as Markdown
```

**The editor cannot change the edition** (ADR-0022). Its schema has three fields per story
— `article_id`, `headline`, `why_it_matters` — plus brief bullets. There is no field for a
URL, a source, a date, a score or an ordering, so it cannot express one.
`build_edition()` copies every link and date from the selected `RankedArticle` objects.

| Editorial output | Treatment |
|------------------|-----------|
| polish for an unselected or repeated `article_id` | discarded, warning logged |
| headline over 140 chars | discarded, original title used |
| any text containing a URL, markdown link or HTML tag | discarded, original used |
| model call fails entirely | deterministic edition, error recorded |

`build_edition` is pure and model-free, which is what makes the offline fixture pipeline
possible.

**Rendering.** One `NewsletterEdition` produces HTML, Markdown and JSON, so the three
artifacts cannot disagree. `validate_edition_links` runs before anything is written: every
URL is re-validated as `http(s)`, checked against the ingested set when supplied, and any
URL hidden in prose fails the render (AC13). HTML autoescape is on with no `| safe`
anywhere — a `<script>` tag in a scraped headline renders as text.

**The artifact.** Blue-and-paper newspaper styling (ADR-0029), designed on a canvas in
`design/` before being ported: an issue block and two-tone wordmark, the executive brief as
numbered cards, the lead story beside a filled sidebar holding `sections[0]`, then the
remaining sections as a card grid. Every headline is a link; every story carries a visible
`Read original →`; all external links use `target="_blank" rel="noopener noreferrer"`.
Self-contained: inline CSS, a system font stack, no JavaScript, no external assets, plus a
print stylesheet.

The engine has no images, so the weight a photograph would carry goes to the lead's *why it
matters* block and to the accent fills. The wordmark accents the last word of whatever
masthead is configured, so the two-tone survives a rename.

**Verification** (ADR-0023). Tests read the *generated* output, and
`tests/fixtures/expected_newsletter.{md,json,html}` are golden files refreshed by
`scripts/refresh_expected_edition.py`.

## The pipeline (implemented — Stage 7)

`src/newsletter/pipeline.py` walks the state machine once, in plain Python, with every
collaborator injectable so the whole run is testable offline:

```python
run_pipeline(context, *, analyzer=None, editor=None, database=None, adapter_factory=None, now=None)
```

Each stage writes its counts into the `RunManifest` and prints one console line
(PRD §33). What ends the run, and nothing else:

| Condition | Outcome |
|-----------|---------|
| configuration invalid | exit 1, before any network call |
| persistence will not initialise | exit 1 |
| no source returned a usable article | exit 1 |
| no article could be normalized | exit 1 |
| edition fails link validation, or output cannot be written | exit 1 |
| nothing cleared the threshold / the window | **exit 4** — a quiet week, not a defect (ADR-0024) |
| a broken source, an unreadable page, an unassessable article, a failed editor | recorded, run continues |

`--dry-run` stops after deduplication: sources are fetched and every deterministic stage
runs, but no OpenAI call is made, no database is opened and no file is written.

**Verification.** `tests/integration/test_full_pipeline.py` runs three fake sources —
alpha (RSS, one story out of window), beta (scraped index, republishing alpha's article
under a different headline) and gamma (down) — through the real ingestion, normalization,
dedupe, scoring, selection, editing and rendering code with a fake HTTP client and a fake
SDK. It asserts all five artifacts, AC3 traceability, AC6 windowing, AC10 partial failure,
and byte-identical output across two runs. `output/fixture-edition/2026-W34/` holds a
generated example.

## Reader submissions (implemented — Stage 9)

```text
src/newsletter/ingestion/submissions.py   the gate, the adapter, submission identity
src/newsletter/pipeline.py                decide_submissions() -> a reason per submission
src/newsletter/ranking/selection.py       reserve() -> the slots a submission holds by right
```

Anyone can propose a link:

```bash
python -m newsletter submit https://example.com/story --by "Ana" --note "why it matters"
python -m newsletter submissions --status rejected
```

The submission is stored `pending` and joins the next run as one synthetic source
(`reader-submissions`). Ingestion, normalization, deduplication, assessment and scoring are
the *same* code as for any other article — no shortcut. Selection is where it differs: while
`submissions.reserved_slots` is on, submissions take the edition's slots first and the rubric
fills the rest (ADR-0040, superseding ADR-0028).

**Three defences, because a submitter is a stranger:**

| Risk | Defence |
|------|---------|
| prompt injection via the submission form | `note` is stored for humans and never enters a prompt |
| faked recency | no date hint is offered; the date must come from the page |
| SSRF into internal services | scheme, host blocklist and *resolved address* checked at submit time, and re-checked by the transport on every request |

The address guard refuses loopback, private, link-local, reserved, multicast and
unspecified addresses, including a hostname that resolves into them — which covers a cloud
metadata endpoint and split-horizon rebinding.

**Outcomes** are recorded with a reason a submitter could read: `published` (with the issue
label), `approved` (did not fit this edition), `rejected` (outside the window, duplicate,
unreadable — or below the threshold, when reservation is off), `pending` (not reached — the
per-run cap).

## Runtime pipeline reference

```text
LOAD CONFIG → DISCOVER → FETCH → NORMALIZE → HARD FILTER → DEDUPLICATE
→ ANALYZE → SCORE → SELECT → EDITORIAL SYNTHESIS → VALIDATE → RENDER → PERSIST RUN REPORT
```

Each stage has typed inputs and outputs, is independently testable, reports metrics into
the run manifest, and fails explicitly.

Where intelligence is allowed:

| Concern | Owner |
|---------|-------|
| fetch strategy | static YAML config |
| date window | Python |
| deduplication | Python (URL, content hash, title), then structured event fingerprint |
| classification, summary, ratings | OpenAI, strict schema (`ArticleAssessment`) |
| final score | Python (`ranking/scoring.py`) |
| story selection | Python (`ranking/selection.py`) |
| editorial synthesis | OpenAI, strict schema (`NewsletterEdition`), selection already fixed |
| HTML / Markdown | Jinja2 templates |

Trust boundary: scraped content is untrusted data. The analyzer has no tools, no
filesystem and no web access; the editor receives validated structured records only.

## Repository layout *(target)*

```text
config/     sources.yaml, newsletter.yaml
src/newsletter/
  cli.py config.py models.py pipeline.py
  ingestion/     base.py rss.py scrapling.py
  normalization/ article.py urls.py
  intelligence/  analyzer.py editor.py schemas.py prompts/
  ranking/       scoring.py dedupe.py selection.py
  persistence/   sqlite.py
  rendering/     renderer.py templates/
tests/      unit/ integration/ fixtures/
output/     <edition>/newsletter.{html,md,json}, selected_articles.json, run_manifest.json
```
