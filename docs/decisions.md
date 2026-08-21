# Decision log

ADR-style, newest last. Record only durable decisions the coordinator must remember —
not routine implementation choices.

Format: **Date · Decision · Reason · Alternatives considered · Consequences**

---

## 2026-08-18 · ADR-0001 · Permission rule syntax adapted to Claude Code 2.1.x

**Decision.** The PRD's proposed `.claude/settings.json` was implemented with current
matcher syntax rather than verbatim: Bash rules use `Bash(cmd:*)` (prefix matching)
instead of `Bash(cmd *)`, and file rules use gitignore-style paths — `Read(./.env)`,
`Read(./**/.env.*)`, `Read(//~/.ssh/**)` — instead of `Read(.env)` / `Read(~/.ssh/**)`.

**Reason.** Installed version is Claude Code 2.1.234. `:*` is the documented prefix
form for Bash rules, and file-path rules resolve relative to the settings file unless
anchored. The PRD explicitly permits syntax adjustment as long as intent is preserved.

**Alternatives.** Copy the PRD JSON verbatim (rules would silently fail to match, which
is worse than a visible deviation).

**Consequences.** Intent preserved exactly: routine work is frictionless, git writes and
Skills are gated, secrets are denied. Matchers are verified by parse and by
`scripts/validate_repo.py`, but only live sessions confirm each matcher fires as
intended — tighten on the first unexpected prompt.

---

## 2026-08-18 · ADR-0002 · Subagent frontmatter drops `maxTurns`

**Decision.** `source-researcher` and `quality-auditor` use `name`, `description`,
`model` and `tools` only. The PRD's suggested `maxTurns: 20 / 25` was omitted.

**Reason.** `maxTurns` is not a recognised agent frontmatter key in this Claude Code
version; unknown keys risk parse warnings and give false assurance of a bound.

**Alternatives.** Keep the key as documentation (misleading); enforce a turn budget in
the agent prompt (kept — both agents are scoped to a narrow, terminating procedure).

**Consequences.** Turn bounding is behavioural, not enforced. Both agents are read-only,
so an over-long run costs tokens, never repository state.

---

## 2026-08-18 · ADR-0003 · Guard hook on `PreToolUse`, validator on `Stop`

**Decision.** `scripts/claude_guard.py` runs as a `PreToolUse` hook on `Bash` only.
`scripts/validate_repo.py --quiet` runs on `Stop`, not on `PostToolUse`.

**Reason.** The guard must block before execution; the validator only needs to catch a
broken repository once per turn. Running it after every edit would tax every file write
for no additional safety, and the PRD explicitly warns against slow hooks.

**Alternatives.** `PostToolUse` on `Edit|Write` (noisier, slower); no hooks (loses the
shell-level bypass defence, since `permissions.deny` on `Read(.env)` does not stop
`cat .env`).

**Consequences.** A syntax error introduced mid-turn is caught at the end of the turn,
not immediately — acceptable, since Ruff and pytest run at stage gates. The guard is
narrow by design: 9 rules, verified by an 18-case allow/block matrix, tuned to avoid
false positives on `.env.example`, `conda env list`, `python -m venv` and
`rm -rf output/...`.

---

## 2026-08-18 · ADR-0004 · Secret defence is layered, not single-point

**Decision.** Three independent layers: `permissions.deny` for tool-level file access,
`claude_guard.py` for shell-level bypasses (`cat .env`, `printenv`, `echo $OPENAI_API_KEY`,
`git add .env`), and `.gitignore` plus a `validate_repo.py` check for commit-level leaks.

**Reason.** `permissions.deny` governs the Read/Edit tools only; a shell command is the
obvious bypass. Defence in depth is cheap here.

**Alternatives.** Rely on `permissions.deny` alone (leaves the shell path open).

**Consequences.** Reading real credentials requires deliberately defeating three layers.
`.env.example` remains freely readable and is the documented configuration surface.

---

## 2026-08-18 · ADR-0005 · PRD file keeps its original name

**Decision.** The PRD stays at `PRD_Weekly_Intelligence_Newspaper_Claude_Code.md`;
`CLAUDE.md` and the skills reference that path instead of the PRD's suggested `PRD.md`.

**Reason.** Renaming a user-provided file is an unrequested change with no functional
benefit.

**Alternatives.** Rename to `PRD.md`; add a stub `PRD.md` pointing at it (duplicate
source of truth).

**Consequences.** One extra path to remember; no ambiguity about which document is
authoritative.

---

## 2026-08-18 · ADR-0006 · Ingestion hidden behind a `SourceAdapter` protocol

**Decision.** Scrapling objects never leave `src/newsletter/ingestion/`. All ingestion is
consumed through `SourceAdapter.discover()` / `.fetch()` returning `DiscoveredArticle` and
`RawArticle`.

**Reason.** The PRD requires normalization at the ingestion boundary, and the local
interpreter is Python 3.14.4 where Scrapling support is unconfirmed. The protocol keeps a
swap to `httpx` + `lxml`/`selectolax` a one-module change rather than an architectural
rewrite.

**Alternatives.** Use Scrapling types throughout (fast now, expensive to reverse).

**Consequences.** A thin mapping layer per adapter, offset by fixture-based offline tests
and a genuinely replaceable fetch layer. Stage 2 must confirm Scrapling installs on the
target interpreter before depending on it.

---

## 2026-08-18 · ADR-0007 · Date windows are half-open and rolling by default

**Decision.** `DateWindow` is `[start, end)`. The default derivation is
`WindowMode.ROLLING` — `[now - window_days, now)` — with `WindowMode.COMPLETED_DAYS`
available as configuration. `--from A --to B` treats both dates as inclusive calendar
days, so the half-open end becomes midnight after B.

**Reason.** The PRD phrase "last 7 completed days up to execution time" admits two
readings. Half-open bounds make consecutive windows partition time with no overlap and
no gap, which is what keeps an article from being published twice. Rolling matches "up to
execution time"; completed-days is the more reproducible reading, so it stays available
rather than being argued about.

**Alternatives.** Hard-code one reading (guarantees the wrong one for somebody); infer
intent at runtime (nondeterministic).

**Consequences.** `contains()` rejects naive datetimes outright, so a timezone bug fails
loudly instead of silently shifting the window. Window mode is one config key, covered by
boundary tests on both modes.

---

## 2026-08-18 · ADR-0008 · Stable CLI exit codes, including 3 for not-yet-implemented

**Decision.** 0 success, 1 configuration or runtime error, 2 usage error (argparse),
3 requested behaviour not implemented in the current stage. `run` without `--dry-run`
returns 3 until Stage 7 wires the pipeline.

**Reason.** CI and the weekly workflow branch on exit codes. A stage that is not built yet
must be distinguishable from a real failure, and must never look like success.

**Alternatives.** Return 1 (indistinguishable from a config error); return 0 with a
warning (a scheduled run would silently report success while producing nothing).

**Consequences.** The Stage 7 change is deleting one branch. `--dry-run` is genuinely
useful from Stage 1: it resolves configuration, computes the window and prints the plan.

---

## 2026-08-18 · ADR-0009 · The initial source list is provisional and partly disabled

**Decision.** `config/sources.yaml` ships 11 sources: 9 RSS entries enabled, 2
`scrapling_static` entries disabled. A header comment states plainly that the entrypoints
are unverified.

**Reason.** Stage 1 forbids network access, so no URL in that file has been confirmed.
Shipping unverified URLs as enabled would make a live run look broken for reasons that are
really just configuration. Scrapling is also not installed yet.

**Alternatives.** Ship an empty list (config tests and dry-run would have nothing to
exercise); enable everything (a live run would fail on unverified feeds).

**Consequences.** Stage 2 must verify each source via `/add-source` and the
`source-researcher` subagent before any source is trusted, and must enable the two
scraping sources only after confirming their DOM and date semantics.

---

## 2026-08-18 · ADR-0010 · Secrets are `SecretStr`, and tzdata is a Windows dependency

**Decision.** `RuntimeSettings.openai_api_key` is a Pydantic `SecretStr` populated only
from the environment. Configuration objects are safe to log or dump. `tzdata` is declared
as a dependency under `platform_system == "Windows"`.

**Reason.** The run manifest and debug logging will serialize configuration; a plain `str`
key would eventually be written to an artifact. On Windows there is no system tz database,
so `ZoneInfo("UTC")` raises without `tzdata` — and the date window depends on it.

**Alternatives.** Read the key straight from `os.environ` at call sites (untestable,
scattered); pin UTC only and skip `zoneinfo` (breaks the configurable timezone).

**Consequences.** Reaching the key requires an explicit `get_secret_value()`, which is
greppable in review. A unit test asserts the key never appears in `repr()` or in a JSON
dump.

---

## 2026-08-18 · ADR-0011 · Runtime dependencies installed on Python 3.14; Scrapling risk closed

**Decision.** Dependencies were installed into the user-level Python 3.14.4 interpreter
(no virtualenv), plus `pip install -e . --no-deps` for the package itself. Version pins
were then tightened to the versions actually exercised: `openai>=3.0,<4` and
`scrapling>=0.4,<0.5`.

Installed and import-verified: scrapling 0.4.14, feedparser 6.0.14, openai 3.2.0,
lxml 6.1.1, jinja2 3.1.6, pydantic 2.13.4, pyyaml 6.0.3, tzdata.

**Reason.** The open risk from ADR-0006 was whether Scrapling works on Python 3.14.
It does: PyPI ships a cp314 wheel for 0.4.14 and its lxml dependency, and the module
imports cleanly. Scrapling 0.4 also exposes exactly the surface the four configured
strategies need — `Fetcher` (static), `DynamicFetcher`, `StealthyFetcher`, `Selector` —
whereas the previously pinned 0.2 line has a materially different API, so the lower bound
had to move. `openai` resolved to 3.2.0, whose `client.responses.parse` is the structured
output entry point Stage 4 will use; pinning `>=1.40` would have allowed an SDK that the
Stage 4 code is not written against.

**Alternatives.** Create a virtualenv (cleaner isolation, but the user asked for a plain
install and every documented command would then need the venv interpreter); keep the loose
`>=` pins (a fresh environment could resolve to an SDK with a different API and fail only
at runtime).

**Consequences.** `python -m newsletter` and the `newsletter` console script now work
without `PYTHONPATH`. The full gate passes on the installed package: ruff clean,
93 tests, dry run, repo validation. `SourceAdapter` (ADR-0006) stays in place — it is
still the right boundary — but its fallback is now insurance rather than a live plan.

**Open item.** Scrapling browser backends are not installed. `Fetcher` (static) works
today; `DynamicFetcher` and `StealthyFetcher` additionally need browser binaries via
`scrapling install`. That is only required if Stage 2 finds a source that genuinely needs
a browser, and it must be justified by observed source behaviour.

---

## 2026-08-18 · ADR-0012 · Scrapling is the parser; the transport is the standard library

**Decision.** Ingestion splits transport from parsing. `scrapling.Selector` is the
parsing engine for non-feed sources, but pages are retrieved through a small
`HttpClient` protocol whose default implementation is `urllib.request`. The
`scrapling_static` strategy is fully supported today. `scrapling_dynamic` and
`scrapling_stealth` are explicit extension points: constructing one without a browser
`page_loader` raises `UnsupportedStrategyError` naming the exact fix.

**Reason.** Scrapling 0.4 ships its fetchers behind the `scrapling[fetchers]` extra —
`Fetcher` alone needs `curl_cffi`, and the extra also pulls playwright, patchright and
browserforge. The base install imports `Selector` fine, which is the part that earns its
place: robust CSS/XPath selection and adaptive-selector recovery over untrusted markup.
Feeds and static pages do not need a browser, and PRD section 12 says to prefer the
standard library where it is sufficient. Injecting the transport is also what makes every
extraction test offline and deterministic.

**Alternatives.** Install `scrapling[fetchers]` now (hundreds of MB of browser tooling for
zero currently-configured sources, contradicting "do not add until a concrete requirement
emerges"); use urllib everywhere and drop Scrapling (loses the adaptive parsing the PRD
asks for).

**Consequences.** `HttpClient` is a documented seam: a `ScraplingHttpClient` or a
playwright loader drops in without touching adapter logic. The transport also became the
last line of defence on URL schemes — it re-validates the target and every redirect, so a
`file://` link inside a feed can never reach the network layer. When a source genuinely
needs a browser: `pip install 'scrapling[fetchers]' && scrapling install`, then inject a
`page_loader`; justify it with observed behaviour and record it here.

---

## 2026-08-18 · ADR-0013 · A candidate with no readable date is kept, never dated

**Decision.** During discovery, a candidate whose publication date is *known* and outside
the window is dropped. A candidate whose date cannot be read is **kept** and passed on
with `published_at_hint = None`. `parse_datetime` returns `None` rather than guessing, and
a parsed timestamp with no timezone is normalized to UTC as an explicit, tested assumption.

**Reason.** The two failure modes are not symmetric. Inventing a date corrupts the
deterministic window and can publish something from last year in this week's edition;
carrying an unknown date forward merely defers the decision to Stage 3, which has the
article body and can read a canonical date from the page. Dropping undated candidates
outright would silently lose real stories, since many index pages omit dates entirely.

**Alternatives.** Assume "now" for undated items (fabricates recency — the single worst
outcome); drop them (silent data loss, invisible in the manifest).

**Consequences.** Stage 3 must resolve or reject undated articles explicitly, and the
hard filter there — not discovery — is the authoritative time gate. Discovery-level
filtering is an optimisation that avoids fetching articles the pipeline would discard.

---

## 2026-08-18 · ADR-0014 · Two URL forms: publishable canonical and comparison key

**Decision.** `canonicalize_url` produces what gets **published** and only removes what
cannot change the destination: fragments, analytics parameters, default ports, scheme and
host case. `dedupe_key` produces a key used only for **comparison** and is aggressive —
it also folds `www.`, trailing slashes and `index.html`. Bare `ref` and `source` are
treated as content parameters and kept; only unambiguous analytics keys are stripped.

**Reason.** These two jobs have opposite failure costs. A published link that 404s is a
visible product defect; an over-eager comparison key only risks collapsing two stories,
which the source-priority rule resolves sensibly. Folding them into one function forces a
single compromise that is wrong for one of the two jobs.

**Alternatives.** One aggressive canonical form (risks broken published links); one
conservative form (misses `www` and trailing-slash duplicates, which are extremely common
across a feed and its site).

**Consequences.** `article_id` derives from `dedupe_key`, so the same story reached by any
URL variant keeps one stable identity across runs and across the cache. Two functions to
keep in sync, covered by a parametrized equivalence test.

---

## 2026-08-18 · ADR-0015 · A page cannot claim a canonical URL on another site

**Decision.** `<link rel="canonical">` and `og:url` are honoured only when they point at
the same site the page was fetched from. Otherwise the fetched final URL wins and the
attempt is logged.

**Reason.** Scraped markup is untrusted input, and the canonical tag is the one piece of
untrusted data that would otherwise decide where a published link points and which source
gets credited. A content farm that copies an article and declares a canonical on its own
domain could otherwise redirect attribution — and the reader — away from the real source.
This is the traceability half of the prompt-injection boundary: AC3 and AC13 say every
published URL originates from ingestion.

**Alternatives.** Trust the tag (standard SEO behaviour, wrong for untrusted input);
ignore it entirely (loses real deduplication value when a publisher canonicalizes its own
tracking or AMP variants).

**Consequences.** Cross-domain canonical chains are not followed. Syndicated copies are
still collapsed, but by content hash rather than by a claim in the markup.

---

## 2026-08-18 · ADR-0016 · Persistence stores indexed columns plus the full JSON payload

**Decision.** Every table keeps queryable columns *and* the complete validated model as a
JSON payload column. Reads reconstruct the Pydantic model from the payload
(`model_validate_json`); the columns exist for indexing, joins and manual inspection.
Plain `sqlite3`, no ORM.

**Reason.** The payload guarantees an exact round-trip — including timezone-aware
datetimes, enums and nested structures — without a migration for every model field. The
columns keep the database greppable and joinable, which is what makes a failed run
inspectable. The PRD explicitly warns against a heavy ORM for this scale.

**Alternatives.** Fully normalized columns (a migration for every schema change, and
lossy round-trips for nested models); pure JSON blobs (no indexes, no joins, no
traceability query).

**Consequences.** A field lives in two places, so column and payload can drift; the
payload is authoritative on read, which makes drift harmless for correctness. Schema
version is recorded in a `meta` table for future migrations. Traceability (AC3) is a
join: `edition_items -> articles -> sources`, tested end to end.

---

## 2026-08-18 · ADR-0017 · A separate wire schema, because strict mode rejects bounds

**Decision.** The analyzer requests `AssessmentPayload` (in `intelligence/schemas.py`),
not the domain `ArticleAssessment`. The payload carries no numeric constraints; ranges are
*described* in field descriptions and in the prompt. `to_assessment()` then enforces every
bound in Python and returns the domain model. A rating outside 0-5 is **rejected**, not
clamped; over-long `key_facts` are trimmed, which is cosmetic.

**Reason.** OpenAI strict Structured Outputs rejects `minimum`, `maximum`, `maxItems`,
`pattern`, `format` and similar keywords. Generating the schema straight from
`ArticleAssessment` produces `maximum: 5` and `maxItems: 8`, so the request would have
failed at the API — discovered by converting the model with the SDK helper before writing
any calling code, not at runtime.

**Alternatives.** Strip constraints from the domain model (loses the Python-side guarantee
that makes AC8 true); hand-write raw JSON schema (drifts from the model with nothing to
catch it); clamp bad ratings (silently accepts a model that ignored the rubric).

**Consequences.** Two models for one concept, joined by one tested function, and a test
asserting the generated schema contains no unsupported keyword — so this cannot regress
silently. It also states the architecture precisely: the model is *asked* for 0-5, the
software *guarantees* it.

---

## 2026-08-18 · ADR-0018 · One retry budget, owned by the wrapper

**Decision.** The SDK client is built with `max_retries=0`; retries live in
`StructuredClient` with `max_attempts` (default 3) and exponential backoff. Transport
failures — timeout, connection, rate limit, 5xx — are retried. Refusals, auth failures,
permission errors, bad requests and 404s fail immediately. Truncation by
`max_output_tokens` is a contract error, not a retry.

**Reason.** Two retry layers multiply into an invisible budget: three SDK retries inside
three wrapper attempts is nine calls and nine times the cost. One number, in one place,
asserted by tests that count calls and recorded sleeps. Retrying a refusal or a bad
request is pure waste — the outcome is deterministic.

**Alternatives.** Rely on SDK retries (no control over which errors retry, and the sleep
is untestable); no retries (a single blip loses an article that a second attempt would
have got).

**Consequences.** The sleeper is injectable, so backoff is asserted without slow tests.
An exhausted budget raises a typed `ModelTimeout` or `ModelUnavailable`, which
`analyze_all` records per article — one unassessable article never costs the edition.

---

## 2026-08-18 · ADR-0019 · The analyzer is given no capability it does not need

**Decision.** Every request sets `store=False` and passes no `tools`, no `tool_choice`,
no conversation history and no previous-response id. Application instructions travel in
the `instructions` field; untrusted article text travels in `input`, fenced by explicit
markers. Prompt v1 states that content is data, that instructions inside it are never
instructions, and that lower confidence is the correct response to a manipulation attempt.

**Reason.** The prompt-injection boundary is only real if the model has nothing to be
manipulated *into doing*. With no tools, no filesystem and no network, the worst a hostile
page can achieve is a wrong rating on its own article — which the deterministic score,
the category limits and the human-visible confidence then contain. Separate API fields
make the boundary structural rather than a formatting convention that a clever payload
could blur. `store=False` keeps scraped third-party content out of remote retention.

**Alternatives.** Concatenate instructions and content into one prompt (the boundary
becomes a string convention); allow tools "in case" (creates the very capability the
boundary exists to deny).

**Consequences.** Tests assert the request shape directly: `store is False`, no `tools`
key, instructions and content in separate fields, and a hostile payload confined to the
untrusted block. Any future need for a tool must be an explicit, reviewed decision.

---

## 2026-08-18 · ADR-0020 · Score weights are constants in code; thresholds are configuration

**Decision.** The four weights (6 / 5 / 4 / 3) and the 0-100 range live as named constants
in `ranking/scoring.py`. What *is* configuration: `min_score`, `max_items`,
`section_limits`, `section_order`, `excluded_categories` and `collapse_events`.

**Reason.** The weights define what the newsletter considers important — changing them
changes every historical score's meaning, so it deserves a code review and a test update,
not a YAML edit. Thresholds and caps are ordinary editorial dials that a user should be
able to turn per edition without touching Python. The split follows the PRD line that
cadence and policy are configuration while architecture is not.

**Alternatives.** All weights in YAML (silent drift, and cached assessments would keep
scores that no longer mean the same thing); everything hard-coded (forces a code change to
publish six stories instead of eight).

**Consequences.** A parametrized test pins every component contribution and both extremes
(0 and 100), so a weight change is a deliberate, visible edit. `score_breakdown()` itemises
the arithmetic for the manifest, and a test asserts it always agrees with `compute_score`.

---

## 2026-08-18 · ADR-0021 · Selection owns the line-up; the editor only rewords it

**Decision.** `ranking/selection.py` decides which stories run, in what order, which one
leads, and which section each belongs to. The lead is simply the highest-ranked selected
story, and it is excluded from its own section so it is not printed twice. Every rejection
is recorded with a reason: `below_threshold`, `category_excluded`, `category_limit`,
`max_items` or `duplicate_event`.

**Reason.** AC9 requires that identical inputs and configuration produce an identical
selection. That only holds if selection is a pure function — sorted by
`(-score, published_at, article_id)` so every tie is broken by data rather than by input
order. Leaving lead choice to the editorial model would put a non-deterministic step in
the middle of a deterministic contract; the model still adds value by rewording the
headline and writing the brief, which changes presentation and not composition.

**Alternatives.** Let the editor pick the lead (breaks reproducibility for a marginal
editorial gain); silently discard rejected articles (a thin edition becomes unexplainable).

**Consequences.** A thin or empty edition is fully accounted for from the rejection
reasons in the run report. Event collapse runs first, using the analyzer fingerprint, and
is switchable via `collapse_events` — when it is on, two outlets covering one announcement
yield the higher-scoring story only.

---

## 2026-08-18 · ADR-0022 · The editor can only reword; the edition is assembled in Python

**Decision.** `NewsletterEditor` asks the model for exactly three things per story —
`article_id`, `headline`, `why_it_matters` — plus the brief bullets. The
`NewsletterEdition` is then assembled by `build_edition()` from the selected
`RankedArticle` objects. Every URL, date, source name and score is copied from ingestion
and scoring, never from the response. Polish that names an unselected id, repeats an id,
exceeds a length cap, or contains a URL, markdown link or HTML tag is discarded and the
deterministic original is used.

**Reason.** AC13 says no model-created link may enter publication. Instructing a model not
to invent URLs is a request; giving its schema no field for one is a guarantee. The same
reasoning applies to ordering and story membership: the wire model literally cannot express
them. The editorial content the model *is* allowed to touch — wording — cannot break the
artifact, so bad polish degrades to the original rather than failing the run.

**Alternatives.** Have the model return the whole edition (one hallucinated URL and the
newspaper publishes a dead or hostile link); trust the prompt alone (unverifiable).

**Consequences.** `build_edition` is a pure function that needs no model at all, which is
what makes the offline fixture pipeline and `--dry-run` possible.
`compose_or_fallback()` returns the deterministic edition plus the error when the model
fails, so an editorial outage costs polish rather than the week's edition. Tests feed
hostile polish — a link, a script tag, an unknown id — and assert the original survives.

---

## 2026-08-18 · ADR-0023 · Rendering is verified against generated output, with golden files

**Decision.** Every rendering test reads the **rendered artifact**, not the template.
`tests/fixtures/expected_newsletter.{md,json,html}` are committed golden files, refreshed
deliberately by `scripts/refresh_expected_edition.py`. HTML autoescape is on with no
`| safe` anywhere; Markdown text passes through an escape filter.

**Reason.** The PRD gate says to inspect the generated HTML, and it was right to: the first
render passed template review but produced a broken Markdown list and a `---` directly
after text, which CommonMark reads as a setext heading rather than a rule. No amount of
reading the template would have shown that. Golden files turn a future template edit into a
readable diff instead of a silent visual regression.

**Alternatives.** Assert on template source (would have missed both defects); no golden
files (regressions only visible by opening the file and remembering what it used to look
like).

**Consequences.** A deliberate design change means running the refresh script and reviewing
the diff — the diff *is* the review. Structural HTML assertions (link count, `rel`
attributes, absence of `<script>`, viewport, media queries) run on freshly rendered output
so they cannot go stale, and a scraped `<script>` tag in a headline is proven to render
escaped.

---

## 2026-08-18 · ADR-0024 · A quiet week is not a failure: exit code 4

**Decision.** The pipeline raises `NothingToPublish` when no story clears the threshold or
no article falls inside the window, and the CLI maps it to exit code **4**. Exit code 3
(not implemented) is retired now that the pipeline exists. Fatal conditions — no usable
article data, persistence that will not initialise, output that cannot be written, an
edition that fails link validation — remain exit code 1. The run manifest is persisted in
both cases.

**Reason.** A scheduled weekly job needs to distinguish "the sources were quiet" from
"the system is broken". Reporting both as 1 trains whoever watches the job to ignore
failures; reporting a quiet week as 0 with no artifacts is worse, because the job then
looks successful while producing nothing.

**Alternatives.** Publish an empty edition (a newspaper with no stories is not a
deliverable); return 0 with a warning (silent failure by another name).

**Consequences.** One more exit code to document, and `docs/architecture.md` plus the
README carry the table. The rejection reasons recorded by selection explain *why* the week
was quiet, so exit 4 is always accompanied by an answer.

---

## 2026-08-18 · ADR-0025 · Verify against the live web before trusting the transport

**Decision.** `UrllibHttpClient` now decompresses `gzip` and `deflate` responses even
though it requests `Accept-Encoding: identity`, refuses unknown encodings, and applies the
size cap *after* decompression. The provisional source list from ADR-0009 was exercised by
a real `--dry-run`.

**Reason.** A live dry run against the configured sources fetched 68 articles and reported
one source as a malformed feed. It was not malformed: the server returned
`Content-Encoding: gzip` regardless of the request, the body decoded into replacement
characters, and feedparser reported the only symptom it could see. A transport bug had
disguised itself as a content bug one layer up — exactly the class of defect that fixtures
cannot find, because fixtures are already decoded. After the fix, all nine enabled sources
succeed and the same run fetches 80 articles.

**Alternatives.** Send `Accept-Encoding: gzip` and always decompress (fine too, but it
hides the surprise rather than handling it); switch to `httpx`/`requests` for transparent
decompression (a dependency for one behaviour the standard library can cover in fifteen
lines).

**Consequences.** Six unit tests cover the encoding paths, including a decompression-bomb
guard, since the cap now has to apply to the expanded size. ADR-0009's warning is
downgraded: the entrypoints are verified as reachable and parseable, though the *editorial
quality* of each source is still unproven until a live analysed run.

---

## 2026-08-18 · ADR-0026 · CI proves the suite needs no credential; the weekly job only uploads

**Decision.** `ci.yml` declares no secret at all and runs on Python 3.11–3.14. Beyond lint
and tests it does two things worth naming: it regenerates the golden edition and fails on
any diff, and it re-runs the suite with `OPENAI_API_KEY` deliberately set to an invalid
value. `weekly-newsletter.yml` runs on `workflow_dispatch` plus a weekly cron, takes the
real key from GitHub Secrets, caches the assessment database between runs, treats exit
code 4 as a notice rather than a failure, and uploads the edition as a workflow artifact.

**Reason.** AC14 says normal CI must not need a live credential. Declaring no secret proves
half of it; running with a deliberately broken key proves the other half, because a test
that quietly reached for the network would now fail loudly. The render check exists because
a template edit can change every future edition while every unit test still passes — the
golden diff is the only thing that notices. On the weekly side, the PRD is explicit that
nothing is emailed, deployed, committed or pushed: an artifact is collected by a human.

**Alternatives.** Rely on a mocked SDK alone (does not prove the *absence* of a network
dependency); commit generated editions (they are artifacts, and it would put third-party
content into the repository's history).

**Consequences.** A fork's pull request gets the same CI signal as a maintainer's branch.
The cache key means a re-run costs nothing in tokens for articles already assessed. Any
change to the templates now requires running `scripts/refresh_expected_edition.py` and
reviewing the diff, which is the intended friction.

---

## 2026-08-18 · ADR-0027 · The acceptance audit is a script, and it introspects rather than greps

**Decision.** `scripts/audit_acceptance.py` checks all twenty PRD acceptance criteria
against the repository and the generated fixture edition, marking AC1 (a live run) and
AC12 (does it read as a newspaper) as MANUAL rather than passing them.

**Reason.** An acceptance list that lives only in a document drifts. Two of the checks were
initially written as string searches over source files, and both produced false failures by
matching the *prose* that promised the opposite — a docstring saying "contains no score",
and a comment saying "the absence of URLs". Rewriting them to import the Pydantic models
and inspect `model_fields` made them both correct and stronger: AC13 now asserts that no
model schema anywhere can express a URL, source, date, score or ordering.

**Alternatives.** Audit by hand each release (drifts, and the reviewer sees what they
expect); assert only in tests (tests check behaviour, not the acceptance contract).

**Consequences.** `/final-audit` has something concrete to run. The two MANUAL items are
the honest residue: no script can judge whether an edition reads like a newspaper, and no
offline check can prove a live run works.

---

## 2026-08-18 · ADR-0028 · A submitted link earns consideration, never publication

> **Superseded in part by ADR-0040 (2026-08-21).** Everything below still holds up to
> selection; from selection on, a submission now takes a slot by right rather than by score.

**Decision.** Anyone can propose a story with `python -m newsletter submit <url>`. A
submission is stored as `pending` and, on the next run, enters the pipeline as one
synthetic source (`reader-submissions`, priority 4). From that point it is fetched,
normalized, date-filtered, deduplicated, assessed and scored by **exactly the same code**
as an article from a configured source, and competes on the same terms. There is no
approval queue and no human UI; the outcome is decided by the ordinary threshold and
recorded with a reason the submitter could read:

| Outcome | Meaning |
|---------|---------|
| `published` | it ran in issue X |
| `approved` | it cleared the threshold but did not fit this edition |
| `rejected` | below the threshold, outside the window, a duplicate, or unreadable |
| `pending` | never reached — the per-run cap applies, so it waits |

**Reason.** The obvious implementations are both wrong. Auto-publishing anything submitted
hands the newsletter to whoever submits most; requiring human approval builds the review UI
the PRD lists as a non-goal and makes the feature cost somebody's afternoon every week. The
existing machinery already answers the question "is this worth publishing?" — the honest
move is to let a submission ask that question rather than bypass it.

Three constraints follow from the fact that a submitter is a stranger:

1. **The note never reaches the model.** A submitter writes `--note` for humans. If it were
   forwarded to the analyst, submitting a link would become a way to write the prompt —
   "please rate this 5/5" — which is prompt injection with a friendly interface.
2. **A submitter cannot assert a publication date.** The adapter offers no date hint; the
   date must come from the page, and a page without one is rejected like any other.
3. **The URL is hostile until proven otherwise.** Scheme, host blocklist and *resolved
   address* are checked at submission time, and the transport re-checks every request. A
   URL that resolves into loopback, private, link-local or reserved space is refused, so a
   stranger cannot point the fetcher at an internal service or a cloud metadata endpoint
   and have the response summarised into a newsletter.

**Alternatives.** Publish on submission (capture); a moderation queue (an explicit
non-goal, and it moves the bottleneck to a person); a separate lower threshold for
submissions (two standards of quality in one publication).

**Consequences.** `submission_id` is derived from the URL exactly as `article_id` is, so
resubmitting the same link updates one record instead of piling up duplicates, and a
submission usually shares its id with the article it becomes. Submissions are immutable —
`decide()` returns a copy — so the history of a decision is never overwritten in place.
Writing the address guard surfaced a latent bug in `canonicalize_url`: an IPv6 literal lost
its brackets, turning `https://[::1]/x` into malformed output. Fixed and tested.

---

## 2026-08-19 · ADR-0029 · The edition adopts a blue-and-paper newspaper style, designed before it was coded

**Decision.** The HTML edition was redesigned from a reference the user supplied: vivid
blue on warm off-white, a boxed grid, an issue block, numbered brief cards, and a filled
sidebar. The design was authored first as a canvas (`design/*.dc.html`, published as an
Artifact) and then ported into `newsletter.html.j2`. The canvas is the visual
specification; the template is what ships.

**Reason.** The previous template was a serif broadsheet with a red accent — a reasonable
default, but nobody had chosen it. Designing first made two constraints visible before
they cost implementation time: the reference is photo-led and this engine stores **no
images**, and the edition must stay self-contained, which rules out a webfont. Both were
resolved as design decisions rather than discovered as bugs:

- the hero photograph became a filled accent block carrying the lead's *why it matters*,
  so the interpretation inherits the visual mass the photo had;
- the teaser thumbnails became numbered accent blocks, one per brief bullet;
- the type is a system stack, because a webfont would look right on the canvas and wrong
  in the product.

**Alternatives.** Restyle the template directly (the photo and font problems would have
surfaced halfway through, as rework); keep the serif broadsheet (nobody had chosen it).

**Consequences.** The class vocabulary was preserved deliberately — `.lead`,
`.read-original`, `.section-label`, `.story` — so every existing render assertion keeps
testing the same guarantees through the redesign, and the whole suite passed the port
without a single test change. Two template mechanics are worth knowing: the wordmark
accents the **last word** of whatever masthead is configured, so the two-tone survives a
renamed publication, and the sidebar always holds `sections[0]` with the rest below, which
keeps the choice deterministic. The lead's category label comes from the edition's own
section titles, falling back to the configured defaults.

---

## 2026-08-19 · ADR-0030 · Priority is a trust tier, not a ranking; no source may fill an edition

**Decision.** Two changes, after the first live run published four of seven stories from one
publisher. Source priorities were flattened from a 5-10 spread to three coarse trust tiers
— 7 for an organisation announcing its own news, 6 for specialist trade press, 5 for
general tech press — and `newsletter.max_per_source` (2) caps how many stories any one
source may contribute. Reader submissions moved from priority 4 to 7, so a submitted link
competes on equal footing with a primary source. The score formula is untouched: the PRD
fixes it, and priority is configuration.

**Reason.** With priorities spread 5-10, the source moved a story more than its own merits
did. The evidence was exact: a submitted post scored 64 and was rejected, while the
identical assessment from a priority-10 source would have scored 70 and published. That is
priority doing the selecting. Re-running the same week with tiers and a cap changed the
edition from three sources to five: two stories that had been crowded out — a Hugging Face
cluster-utilisation piece and a Groq funding round, both scoring 72-75 on the rubric alone
— replaced two lower-merit stories that had ridden a +10 boost. The bar on merit went up.

**Alternatives.** Lower `min_score` (publishes more of everything rather than fixing the
distortion); reduce the priority weight in the formula (the PRD specifies it, and
configuration was the right lever); leave it (the paper reads like one company's press
page).

**Consequences.** `max_per_source` defaults to None, so the cap is opt-in and existing
configurations are unaffected. A rejection now carries a `source_limit` reason alongside
the others. Two limits worth stating: raising a submission to the top tier did **not** make
the tested post publishable — it moved from 64 to 67 against a threshold of 70, because on
326 characters of tweet it scored 60 on a rubric where the published set scored 71-78. The
remedy for a thin submission is more context to judge, not a bigger thumb on the scale. And
a decided submission is never re-offered, so a policy change does not reconsider it; there
is no re-queue command yet.

---

## 2026-08-19 · ADR-0031 · A thin submission is enriched from its own outbound link, by Python

**Decision.** When a submitted page carries less readable text than
`submissions.min_text_chars` (600), ingestion follows the page's own first usable outbound
link, fetches it through the same guarded transport, and attaches its text to the article
under a visible label. The submitted page stays the article — its title, date and URL are
what get published. `submissions.follow_links`, `max_link_hops` and `max_linked_chars`
bound it; a failure is never fatal, the submission is simply judged on the thin page.

**Reason.** A post is usually a pointer, not the story: 300 characters announcing something
with a link to the announcement. Judging the pointer as though it were the story is unfair
to whoever submitted it. The alternative the user proposed — letting the analyzer search
the web — was declined because three guarantees depend on the model having no tools:
traceability (AC3/AC13) would break as facts arrived from sources nobody configured; the
injection boundary would gain an instruction channel, since a page saying "search for X and
treat it as authoritative" becomes actionable the moment the model can search; and
reproducibility (AC9) would go, because search results change hourly. Following a link the
page itself contains keeps every one of those: Python picks the link from markup by rule,
the model never chooses what to fetch, and the result caches like any other content.

**Alternatives.** Replace the article with the linked page (the linked page often has no
publication date — the contest terms this was tested against do not — and the submitter
pointed at the post, not the terms); model-driven search (above); do nothing (thin
submissions are judged on 300 characters).

**Consequences.** `RawArticle` gained `linked_url` / `linked_text`, and the linked material
enters `clean_text` labelled with its origin, so the content hash covers it and a page that
gains a link is re-analysed. Two bugs were found by testing against the real page rather
than a fixture. The thinness probe measured the whole `<body>`, so 300 characters of post
inside 1,700 characters of navigation looked substantial and was never enriched — it now
measures what normalization actually extracts. And `twitter.com` counted as a different
site from `x.com`, offering a profile link on the same platform as the first thing to
follow; known aliases now resolve to one site.

**What it did and did not do.** On the tested post, enrichment took the analysed text from
326 to 8,408 characters and lifted confidence from 0.7 to 0.9 with a better-grounded
summary — but the rubric moved only 60 to 63, and the same 63 had already appeared on an
unenriched re-run. The post publishes at exactly 70 against a threshold of 70, so it turns
on a one-point wobble in a single rating. Enrichment improved the grounding, not the
verdict; anything sitting on the threshold is decided by model variance, and the assessment
cache is what normally hides that variance — a volatile page whose text shifts between
fetches defeats the cache and gets re-judged.

## 2026-08-19 · ADR-0032 · The newspaper is written in Spanish, for Leader Entertainment

**Decision.** The edition addresses one named audience — the people who plan, produce and
publish Leader Entertainment's children's video, a Latin American company moving into AI
production — and everything a reader sees is written in neutral Latin American Spanish.
That covers the model's prose (`summary`, `why_it_matters`, `key_facts`, the executive
brief, polished headlines), the templates and their chrome, the section titles and the
dates. The rubric was rewritten to rate articles for that company rather than for a
general enterprise reader, and `kids_content` joined the closed category taxonomy.

**Reason.** Tone is not a rendering concern. Which stories are worth publishing, what
counts as "actionable", and what a summary chooses to mention all follow from who is
reading — so audience and language belong in the analyzer's rubric and the editor's brief,
not in a post-processing translation pass. Translating after the fact would also have put a
second model call between the assessment and the page, with nothing constraining it to keep
the facts.

**Prompts are code, so this is a version bump, not an edit.** `article_analyzer_v2.md` and
`newsletter_editor_v2.md` are new files; `ASSESSMENT_SCHEMA_VERSION` and
`EDITOR_SCHEMA_VERSION` moved to `"2"`. Cache identity is
`content_hash:prompt_version:schema_version:model`, so every stored English assessment is
now a miss and no v1 prose can leak into a v2 edition. That was the point of versioning the
prompt; this is the first time it has been spent.

**What stayed English.** The four event-fingerprint fields (`event_subject`,
`event_action`, `event_object`, `event_date`). They are dedup keys compared across
articles, never printed. Translating them would make two reports of the same event stop
matching whenever the model chose different Spanish wording.

**Dates without a locale.** `issue_date` formats Spanish months from a tuple in
`renderer.py` rather than calling `setlocale`. A locale is process-global, mutable by any
library in the process, and not guaranteed to be installed on the host — none of which is
acceptable for output that has to be byte-identical across runs (AC9).

**The threshold had to move, and the source list had to change first.** The v2 rubric is
strictly harder: it asks what an article means for a kids-YouTube company, so an
interesting model-infrastructure story that would have scored well for an enterprise reader
now scores low on relevance and actionability. On the first v2 run nothing cleared 70 — the
best rubric total was 61 where v1 had produced 78. Two things were wrong, and both were
fixed rather than only the visible one:

- *The sources were an AI-industry reading list.* 23 of 43 articles landed in
  `ai_business`. Two feeds were added and verified live — Cartoon Brew (`kids_content`) and
  Social Media Today (`youtube_platform`) — so the pipeline can actually see the beat the
  rubric now rewards.
- *The threshold was calibrated against a distribution that no longer exists.* `min_score`
  moved 70 → 62, which is where the v2 score distribution separates.

**Consequences.** The next run discovered 69 articles, 60 in window, and published 8
between 62 and 74 across four sections, led by the U.S. Senate's investigation into Roblox
over child safety — a story the old source list could not have surfaced and the old rubric
would have ranked below a model-pricing announcement. `min_score` is now tied to the prompt
version: a future `v3` rubric will need recalibrating the same way, and the number is
meaningless without knowing which rubric produced it. The audit's AC5 check and the
renderer tests assert Spanish strings, so an accidental revert to English chrome fails the
gate rather than shipping.

---

## 2026-08-19 · ADR-0033 · A story whose prose names an entity the source never mentions is dropped, not published

**Decision.** `intelligence/fidelity.py` compares brand-shaped tokens in reader-visible model
prose against the article's trusted text (`source_name` + `title` + `clean_text`, all of it
ingested, none of it model-authored). A token is checked only if it carries an uppercase
letter at a non-initial position and is not entirely uppercase, or if it mixes letters and
digits. It is a violation when no word of the trusted text vouches for it, compared without
regard to case or punctuation and always anchored at a left word boundary. The guard runs in
two phases inside
`run_pipeline`: before editorial synthesis it checks analyzer prose and drops the offending
article from the line-up; after synthesis it checks editor prose and, on any violation,
discards the polish and rebuilds the deterministic edition. Every drop lands in the run
manifest as a `VALIDATE` error. `check_entity_fidelity` defaults to true.

**Reason.** The first live Spanish edition published *"Esta política, que **UTube** aplicó
anteriormente solo en YouTube Shorts…"*. A fabrication audit of all eight stories found no
invented facts — every figure and named entity traced to the source — so the failure mode is
not hallucinated claims but corrupted entity strings, and it appeared in the flagship
YouTube-facing story. Nothing in the schema could catch it: `summary` is a free string, and
strict Structured Outputs constrains shape, not content. This is exactly the class the
architecture reserves for deterministic Python (rule 1), and it needs no model call.

**Why the story is dropped rather than the run failed.** Assessments are cached under
`content_hash:prompt_version:schema_version:model`. A fatal guard would therefore be
unrecoverable: the re-run returns the identical cached prose and the edition stays blocked
until someone manually invalidates the cache. Dropping one story is proportionate and matches
the failure-isolation rule — a broken part must not kill the edition — while the manifest
entry keeps it from being silent (rule 7). If the filter empties the line-up, the existing
`NothingToPublish` path reports a quiet week rather than a crash.

**Why the match is anchored on the left only.** The obvious rule — case-insensitive substring
— cannot catch this defect at all, because `utube` *is* a substring of `youtube`, so any
article mentioning YouTube would vouch for the corruption. A left word boundary fixes that,
and it is the one part of the rule that cannot be relaxed. Everything to the right of the
anchor is deliberately loose: false negatives are tolerable, false positives delete a real
story from the newspaper.

**Why all-caps tokens are exempt.** The prose is Spanish and the sources are English, so any
rule that survives translation must only test tokens translation does not touch. Brand names
qualify; acronyms do not — `IA` is Spanish for `AI`, and `CEO`/`API` appear in Spanish prose
whose English source may use different wording. Testing them would fire constantly on correct
output.

**Alternatives.** A prompt fix was rejected: the corruption sits in a story `summary`, which
is analyzer output, so it would mean `article_analyzer_v3` — invalidating the entire v2
assessment cache and forcing another `min_score` recalibration (ADR-0032), which would also
destroy the two-edition baseline needed before the rubric is touched again. Letting the model
self-check was rejected under ADR-0031's reasoning: the guarantee comes from the model having
no tools. Recording the violation without acting on it was rejected because the defect would
still reach the reader.

**Consequences.** The edition can now be thinner than the score threshold alone would predict,
so `selection.py` gained `REASON_UNSUPPORTED_ENTITY` and `articles_selected` is corrected to
the surviving count. No prompt, schema version, score formula or threshold changed, so the v2
cache is intact.

**Two false positives found in reliability review, and how matching answers them.** The first
literal implementation dropped a faithful story over punctuation — scraped English writes
`GPT4` and `H100` where the model prints `GPT-4` and `H-100` — and over Spanish brand
morphology, flagging `YouTubers` against a source that says `YouTube`. Both delete a true
story from a published newspaper, which is strictly worse than the corruption the guard
prevents, so both were fixed. Comparison now runs on a normalized form: each side keeps only
alphanumerics, case folded, so a whitespace-delimited source word becomes one unit (`GPT-4`
collapses to `gpt4`, never to a loose `gpt`), and consecutive source words are merged on
demand so `YouTube Shorts` also supports prose writing `YouTube-Shorts`. A match then counts
in either length direction: the source may be longer than the token, which is how `YouTube`
vouches for `YouTubers`, or shorter, which is how a source writing `GPT` vouches for prose
writing `GPT-4`. The shorter-source direction has a floor of three characters — the length of
`GPT`, the shortest stem that has to keep working — because a bidirectional prefix rule with
a two-character floor lets `AI`, `EE` or `Op` vouch for every brand starting those letters and
the guard would quietly stop guarding. Collapsing punctuation never collapses the left
anchor, so `UTube` is still caught against an article that only ever writes `YouTube`, and the
existing rule that a source `GPT-4` does not vouch for prose `GPT-5` still holds.

**What is still a false positive.** Any alphanumeric identifier that reaches reader-visible
prose and is absent from the article body — short codes such as `Q3` or `T4` that the source
spells out in words, and the 16-character hex `article_id` that the integration harness prints
into fake headlines — is checkable by shape and unsupported by text, so it still drops a
story. A thin or truncated `clean_text` widens the same hole: the fewer words ingestion
collected, the more of the model's correct names have nothing to vouch for them. And the left
anchor means a brand the prose *opens* differently from the source is unreachable by any
loosening, because that anchor is exactly what makes `UTube` catchable. These are accepted:
each one costs at most a story, and the manifest records every drop.

---

## 2026-08-19 · ADR-0034 · One event is collapsed by content similarity, and the title never suppresses across editions

**Decision.** Three changes to how an edition decides what not to print.

1. `PublishedKeys` carries `article_id` and `content_hash` only. The normalized title is no
   longer a cross-edition suppression key, and `published_identity_keys` no longer reads the
   column. The title pass is unchanged inside one run (`deduplicate`).
2. `RunManifest` gained `withheld: list[WithheldStory]` (article id, url, title, reason,
   detail) and `record_withheld`. `select` writes every `already_published`, `similar_event`
   and `subject_limit` rejection into it, carrying the detail text.
3. A second collapse pass, `collapse_similar_events`, runs after the exact-key collapse and
   inside one run only. Unigram TF-IDF over `title + clean_text`, sublinear term frequency,
   document frequency taken over the run's own candidates, L2-normalized cosine, greedy in
   `ranking_key` order so the survivor is the highest-scoring report. Gated by
   `collapse_similar_events` (default true) and `similar_event_threshold` (default 0.21).
   It folds only candidates whose `final_score` reaches `min_score`; see *Scope* below. The
   exact-key pass keeps its full scope — it is a key, not a judgement, and costs nothing.

**Reason.** (1) A headline repeats on a recurring beat — "YouTube changes its monetization
rules" is plausible in March and again in September — and inside one run that repetition is
cheap to be wrong about while across editions it is permanent and invisible. An identical
article id or content hash is not a guess; a shared headline is. (2) CLAUDE.md rule 7: the
console is not an audit surface, and a story that disappears between the candidate pool and
the printed page must be explainable from the artifact. (3) The owner's actual complaint was
three of eight stories on one ChatGPT-for-Teens launch. Exact-key matching provably cannot
catch it: each article is assessed by an independent model call with no cross-article
consistency mechanism, so the free-text `event_subject`/`event_object` pairs disagree
("openai / chatgpt for teens" beside "chatgpt / teen accounts"). The articles themselves do
not disagree — they share their rare words.

**Why TF-IDF cosine over words.** Three outlets on one launch share vocabulary and proper
names and almost no verbatim phrasing, so 5-word shingles under-match them; raw word overlap
over-matches everything about the same industry. Inverse document frequency is the
discriminator: `the`, `ai`, `company` cost nothing, `teens`, `parental`, `safeguards` carry
the signal. Bigrams were measured and rejected — they diluted the true trio to 0.13–0.21 while
leaving unrelated pairs at 0.36. One extra rule earns its place: a term printed on at least
half of one source's articles in the run is site furniture (masthead, byline template,
"latest stories" rail) and is dropped per source. Without it the two Cartoon Brew stories in
2026-W34 scored 0.44 against each other, higher than the real trio.

**The threshold, measured on the real 2026-W34 data** (58 candidates, the eight published
stories, read-only from `newsletter.sqlite`). True trio, pairwise: 0.3779, 0.3373, 0.2774.
Nearest similarity between any *other* published story and any other candidate in the run:
0.1419 (Roblox Senate investigation vs YouTube Shorts monetization). 0.21 sits between them
with ≈0.07 of margin on each side. The next-nearest published pairs are 0.1215, 0.1104, 0.0899
— nowhere near. These are the numbers over the full candidate pool; *Re-measured* below gives
them again for the pool the pass now actually folds over, and is the authority for the scope
that shipped.

**Alternatives.** Keying the collapse on the analyzer fields was already in place and is what
failed. A prompt change to force consistent event naming was rejected: it means
`article_analyzer_v3`, which invalidates the whole v2 cache and forces another `min_score`
recalibration (ADR-0032). Asking a model to judge similarity was rejected under ADR-0031.
Running the pass across editions was rejected for the same reason the title key was: inside a
week an over-collapse costs one story, across weeks it buries every follow-up forever.

**Scope: only a publishable candidate is ever folded.** The pass first shipped over every
candidate, and the false positives below were tolerated on the grounds that they happened to
land under `min_score` and so cost the edition nothing. That was luck, not design, and the
paragraph that argued it is superseded. The fold is now restricted to candidates whose
`final_score` reaches `min_score`. The argument is simple: a candidate under the floor cannot
reach the page whatever this pass decides, so collapsing it changes no edition and can only be
wrong. Every false positive measured on 2026-W34 lived there — the distinct TechCrunch pair at
0.4637 (SpaceX/Cursor vs Stripe/OpenRouter, both carrying a rotating "latest stories" rail),
the two OpenAI posts about young people at 0.3605, the Hugging Face reports at 0.3572 — all of
them scoring 42–58 against a floor of 62. They are out of scope by construction now, not by
accident.

**What is scoped is the fold, not the evidence.** Restricting the *term statistics* to the
publishable subset as well was measured and is wrong. Inverse document frequency and the
per-source chrome rule are properties of the run's whole corpus and are only observable there:
with 13 publishable candidates The Verge contributes two, which is under
`MIN_SOURCE_ARTICLES_FOR_CHROME`, so its chrome becomes unmeasurable and its rail stays in the
vectors. "Anthropic explains Claude's watermarks" then scores 0.4487 against "ChatGPT is
getting a dedicated mode for teens" — higher than anything in the true trio — and the greedy
pass loses "Pacing model development" (64) and the YouTube first-frame story (63), two of the
eight stories that actually published. So `collapse_similar_events` takes the whole candidate
list and is told the floor; it profiles everything and folds only what could print.

**Re-measured on the real 2026-W34 data**, read-only from `newsletter.sqlite` (59 candidates
reconstructed from stored articles and v2 assessments — one more than the 58 the original run
recorded, since the file has accumulated across runs; the publishable subset is exactly the 13
the run manifest reports). True trio, pairwise: 0.3621, 0.3081, 0.2892. Nearest pair between
any two *distinct* publishable candidates: 0.1461 (Roblox Senate investigation vs YouTube
Shorts monetization, both Cartoon Brew). 0.21 sits 0.079 under the trio's weakest pair and
0.064 over the nearest false one. The pass folds exactly two candidates out of 59, both of them
the trio's junior reports into "Introducing ChatGPT for Teens" (68); the other five published
stories — Roblox, YouTube Shorts monetization, connected TV, first-frame views, Pacing model
development — all survive, and the result is identical for the input reversed and shuffled
(AC9).

**An above-threshold false collapse is still possible, and is recorded.** Nothing here proves
two publishable stories can never be pushed together by shared furniture; it proves it did not
happen in the one edition we can measure, with roughly 0.06 of margin. When it does happen the
run manifest carries the loss as a `WithheldStory` with reason `similar_event` and a detail
naming the story it was folded into, so a thin or surprising edition is explainable from the
artifact rather than from the console (CLAUDE.md rule 7). That is the guarantee on offer: not
that the pass is never wrong, but that it is never silently wrong.

**The root cause the measurement exposed, and where it belongs.** Extraction is putting page
chrome into `clean_text` — TechCrunch's "latest stories" rail is the clearest case, and its
headlines are themselves other candidates in the same run. That is what inflates similarity
between unrelated articles, and the per-source chrome heuristic is a patch over it that only
works when a source contributes enough articles to make the repetition visible. The larger
problem is that `clean_text` is also what the analyzer reads, so navigation text is being fed
to the model as article content and is scored as if it were the story. The real fix is
narrowing extraction to the article body, not moving the threshold. Deferred; not done here.

---

## 2026-08-20 · ADR-0035 · A date may be read from an embedded script payload, when the source opts in by naming the key

**Decision.** New optional field on `SourceConfig`: `embedded_date_key` (a JSON identifier,
pattern-validated, default `None`). When set, `extract_published_at` scans the page's
`<script>` elements for that key and validates the value through `ingestion/dates.py`. It runs
*after* the standard routes (`article:published_time`, JSON-LD, `<time datetime>`) and before
the discovery hint, so it can only add a date, never override a declared one. Unset — the
default everywhere else — means the scan does not happen at all.

`anthropic-news` is enabled on this mechanism at priority 7 (ADR-0030: an organisation
announcing its own news), with `embedded_date_key: publishedOn`.

**Reason.** Anthropic publishes no feed (`/rss.xml`, `/feed.xml`, `/news/rss.xml`, `/news/feed`
all 404) and states no date in any standard form: no `article:published_time`, no JSON-LD, no
`<time>` element at all. The rendered text is `Aug 14, 2026`, which is neither ISO 8601 nor RFC
2822, so `parse_datetime` correctly refuses it. The only machine-readable timestamp is
`publishedOn` inside the escaped Next.js RSC payload. The owner wants the source; the date was
the sole blocker; the date is in fact present and unambiguous.

**Why a config field and not an Anthropic adapter.** The pattern — a framework that renders
from an embedded data blob and ships no date in the markup — is not Anthropic-specific; it is
what every Next.js/Nuxt/Remix content site does. Naming the key in configuration keeps `src/`
free of any vendor branch and lets the next such source be a one-line YAML change. The key name
varies by CMS (`publishedOn`, `datePublished`, `firstPublishedAt`), which is exactly why it is
data and not a constant.

**Article pages only; discovery deliberately does not use it.** First match wins, and that is
safe only where the page's own record leads the payload. On an article page it does: the entry
opens `{"post":{...,"publishedOn":X,"relatedPosts":[...]}}`, verified on 7 live articles
spanning 2026-06-30 to 2026-08-14, with the value matching the human-visible date on every one.
The *index* page carries one record per listed item — 271 occurrences of the key, no privileged
first position — so a first-match read there would stamp all 14 items with the first item's
date. Index items therefore stay undated and are filtered by window after normalization, which
costs one fetch per article and is the price of the source.

**Treated as hostile input (rule 3).** The payload is never evaluated and never deserialized.
It is scanned under a 1,000,000-character budget with a bounded, linear pattern; the value
group is capped at 64 characters and excludes quotes and backslashes. The pattern also matches
an *unquoted* value (`null`, a bare number) purely so a key that stopped holding a string is
seen and refused there, rather than the scan sliding on to the next occurrence and returning a
related post's date — a plausible-looking wrong answer is worse than a visible break.

**Failure is explicit (rule 7).** No date means `NormalizationError`, recorded in the run
manifest by `normalize_all`, with a message naming the configured key. Nothing is guessed.

**Known fragility, accepted knowingly.** Every other source rests on a feed or a published
metadata contract. This one rests on the shape of Anthropic's rendering internals, which they
may change without notice and owe nobody. It fails safe rather than silently — a break yields
an empty `anthropic-news` and a manifest full of the named-key error, not a wrong date — and
`tests/unit/test_ingestion_anthropic_news.py` pins the extraction against captured markup as
the tripwire. The manifest, not the edition, is where an operator sees it first.

**Not changed.** No prompt, no schema version, no `min_score`, no score formula; the v2
assessment cache stays valid.

## 2026-08-20 · ADR-0036 · The edition invites submissions, and the intake is a WSGI app with no framework

**Decision.** The whole submission loop is reachable from the newspaper. A new
`submissions.form_url` (default `None`, validated as a public http/https URL at config load)
is threaded to both renderers — `render_html(..., submit_url=)` and
`render_markdown(..., submit_url=)`, the same channel `tagline` already uses — and prints a
call to action in the colophon area, `target="_blank" rel="noopener noreferrer"`. Unset, no
call to action is rendered at all. `newsletter serve` answers that address: `GET /submit`
returns a self-contained form with three fields (full name, link, optional description),
`POST /submit` builds the submission through the existing `create_submission` gate and saves
it through the `Storage` protocol as `pending`, which the next run reads.

**Why WSGI, not a framework.** `newsletter/web/app.py` is a plain callable.
`wsgiref.simple_server` runs it locally with **zero new dependencies**, and
`gunicorn newsletter.web.app:application` runs the identical object on a real server without
a rewrite. Storage is resolved through `create_storage(config.runtime.database_url)`, never a
concrete backend, so the form writes to whichever database the pipeline reads.

**One connection per request.** SQLite connections belong to the thread that opened them and
every real WSGI server is threaded, so a process-lifetime connection would be a latent
cross-thread bug; a dropped server connection would also outlive its request. A form that
sees a submission a day cannot notice the cost.

**Every field is hostile (rule 3).** The URL passes through `check_submitted_url` — https,
host blocklist, and the SSRF address guard — unchanged, because that gate is why it exists.
The name and description are echoed back on the result page, so all text reaching a page goes
through `html.escape(..., quote=True)` in one place, and the model's own limits (80, 500) are
enforced by `create_submission` rather than the form's `maxlength`. The body size is checked
against `Content-Length` *before* `wsgi.input` is read at all: an oversized request costs a
header parse, not 8 MB of buffer. Responses carry a `default-src 'none'` CSP and `nosniff`;
an unexpected exception is logged in full and answered with a generic 500, because a
traceback can carry a path or a DSN.

**Localhost by default, and that is the security model.** The form authenticates nobody:
whoever reaches it can queue a link — which is the point, since a submission buys
consideration and not publication — but it also means exposure is an explicit deployment
decision, taken behind a real server. `--host` says so in its help text and a non-loopback
binding prints a warning at startup.

**Not changed.** No prompt, no schema version, no `min_score`, no score formula, no new
runtime dependency; the v2 assessment cache stays valid. The golden edition changed by
exactly the call to action.

## 2026-08-20 · ADR-0037 · The two stages that only wait now wait in parallel, and the order is restored before anything can see it

**The measurement, not a hunch.** A real edition took **8:51**. Analysis was **5:58** of
it — 62 OpenAI calls issued strictly one after another, median 6.0s each, mean 6.1s, max
15s — and ingestion **2:24** over roughly 145 sequential fetches. Everything deterministic
(filter, dedupe, score, select, render) came to about 30s. Both slow stages were waiting on
a network, not computing anything, and nothing about either requires a queue of one.

**Decision.** `ArticleAnalyzer.analyze_all` and `ingest_source` hand their independent work
to a `concurrent.futures.ThreadPoolExecutor` bounded by configuration:
`runtime.analysis_concurrency` (default **8**) and `runtime.fetch_concurrency` (default
**6** — lower, because fetch parallelism is *within* one source and every one of those
requests lands on the same origin). Both accept `1`, which runs the work inline in a plain
loop with no pool and no thread: the exact sequential behaviour they replaced, kept as the
escape hatch and used by the tests as the baseline to compare against. Both are rejected
with `ConfigError` outside 1-32 and 1-16, whether they come from YAML or from
`NEWSLETTER_ANALYSIS_CONCURRENCY` / `NEWSLETTER_FETCH_CONCURRENCY`.

**The thread boundary sits in front of the database.** A SQLite connection belongs to the
thread that opened it, and the assessment cache *is* the database, so a worker does the
model call and nothing else. `analyze_all` runs in three phases: cache reads on the calling
thread, splitting the batch into hits and misses; the misses' model calls in the pool;
then, back on the calling thread and walking the input from front to back, the manifest
errors, the cache writes, the call counts and the returned pairs. The one deliberate
reordering is that all cache reads now precede all cache writes; the cache key contains the
content hash and deduplication has already made those unique within a run, so no read can
be answered by a write that used to precede it.

**Order is restored, never observed** (AC9). `newsletter/concurrency.py` is the single
place that knows how: work is submitted with its position and read back by position. There
is no `as_completed`, no set iteration, no dependence on dict insertion order. Manifest
errors are the same story — a worker never touches the manifest; failures travel back as
values in their slots and are recorded by the calling thread in input order, so a run's
error list is a walk over the input rather than a race log. An integration test runs the
whole fixture pipeline at 1 and at 8 and compares the three artifacts byte for byte, and
the golden edition test never changed.

**Failure isolation is unchanged** (rule 7). Only exception types the caller names are
turned into values — `ModelError` for analysis, `AdapterError` / `HttpError` for fetching.
Anything else propagates once the pool has drained, from the earliest failing position, so
a bug is still a loud failure and never a quietly skipped article.

**What this asks of an adapter.** `fetch` must be callable from several threads at once.
Every adapter shipped today qualifies: state is built in `__init__` and `discover`, and
`fetch` only reads it, while the transport builds a fresh request per call. The one place
this could stop being true is the browser `page_loader` injection point in
`scrapling.py` (ADR-0012), which has no implementation yet — whoever writes it either makes
it thread-safe or sets `fetch_concurrency: 1`.

**Rate limits, because eight callers hit one now** (extends ADR-0018). The retry budget is
untouched. What changed is the wait before a retry, and only for a rate limit: the server's
`Retry-After` is honoured when it asks for longer than our own backoff, capped at 30s
because a header is a hint and a run must not be parked for an hour, and a random share of
up to half the delay is added on top so a batch throttled together does not march back in
step and spend the whole budget colliding. Timing only: what is sent, what comes back and
in which order are all unaffected.

**Measured, with a fake client sleeping the latency a real run showed** — 6.0s per model
call over 120 articles, 1.0s per fetch over 8 sources x 18 articles
(`scripts/benchmark_concurrency.py`):

| Stage | Sequential | Concurrent | Speedup |
|-------|-----------|------------|---------|
| analysis, 120 calls | 720.1s (12m00s) at 1 | 90.0s (1m30s) at 8 | **8.00x** |
| fetch, 144 articles | 144.1s (2m24s) at 1 | 24.1s (0m24s) at 6 | **5.99x** |

Applied to the run that was actually observed: analysis 5:58 → about 48s, ingestion 2:24 →
about 40s (less than 6x, because discovery stays sequential and sources hold uneven numbers
of articles), the deterministic ~30s unchanged. **8:51 → roughly two minutes**, at which
point the deterministic stages are the largest share of a run and the next optimisation
would be a different one.

**Not changed.** No prompt, no schema version, no `min_score`, no score formula, no new
runtime dependency (`concurrent.futures` is the standard library); the v2 assessment cache
stays valid, and the same stored inputs still produce the same artifacts, byte for byte.

---

## 2026-08-20 · ADR-0038 · The server serves the newspaper at `/`, and the call to action rides the masthead

**Decision.** `newsletter serve` is what a reader opens, not only what a submitter opens.
`GET /` now serves the latest edition's `newsletter.html`; the form keeps `/submit`. The
reader call to action moved to the top of the edition: a button-styled anchor in the
`masthead-foot` strip (HTML) and the line directly under the masthead block (Markdown). The
HTML keeps the colophon banner as well — the two are visually different objects, a button in
the furniture and a banner at the foot — while the Markdown carries the line once, because in
plain text the same sentence twice is noise. Unset `submissions.form_url` still renders
nothing, in either position.

**Which edition is "latest" — `generated_at`, from the database.** A new
`Storage.latest_issue_label()` (implemented in both backends, ordered by `generated_at DESC,
edition_id DESC`) answers it. That is the instant the run recorded when it wrote the
artifacts. File mtime was rejected: a checkout, a copy or a restore rewrites it, and
`output/` also holds `sample-edition` and `fixture-edition` directories that no run generated
and that an mtime scan would happily publish. The tie-break on `edition_id` keeps the answer
single-valued.

**Turning a request into a file read, safely.** `/` takes no parameter at all: the issue
label comes from the database, is matched whole against `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`
(leading alphanumeric rules out `..` and dotfiles; the class contains no separator, so a
label can only name one directory directly under `output_dir`), and only then joins the
configured output directory with the fixed filename `newsletter.html`. The joined path is
resolved and required to stay inside the resolved output directory, so a symlinked edition
directory cannot reach out either. Every other path — `/../`, its encoded spellings, an
explicit `/2026-W34/newsletter.html` — is the pre-existing 404, which names no path but the
form's. There is no static-file route and no per-label route: the edition is self-contained
(inline CSS, no script, no external asset), so one route serves it whole.

**No edition yet answers 200, not 404.** The address is right and the server is healthy; what
is missing is a run. The page says so and names `python -m newsletter run`. A 404 would tell
an operator's uptime check that a fresh install is misconfigured. The same page answers when
the database names an edition whose file was deleted or whose output directory moved — a
missing newspaper, never a 500.

**Not changed.** No prompt, no schema version, no `min_score`, no score formula, no new
runtime dependency (`wsgiref` and the standard library); the v2 assessment cache stays valid,
and the edition is still byte-identical for identical inputs. The golden edition changed by
exactly the moved call to action and its CSS; `expected_newsletter.json` is untouched.

---

## 2026-08-21 · ADR-0039 · `clean_text` is the article body, not the page around it

**Decision.** Normalization now extracts the article body rather than the whole document.
It picks a container — a source selector, then `article`, `main`, `[role="main"]`,
`[itemprop="articleBody"]`, `.article-body`, `.post-content`, then `body` — drops
non-content elements wherever they appear (`script`, `style`, `nav`, `aside`, `footer`,
`header`, `form`, `iframe`, `noscript`, `svg`, `template`, `button`, `select`, `dialog`),
and then discards blocks whose text is more than 60% link characters, exempting prose tags
and anything under 60 characters. Removal is non-mutating: the tree is left as fetched, so
every later extractor still sees the page the fetch returned.

**Reason.** ADR-0034 recorded this as the deferred root cause and it was doing more damage
than the deduplication bug that exposed it. Two unrelated TechCrunch articles scored 0.464
against each other purely because both carried the site's rotating story rail — higher than
three genuine reports of one event. The analyzer was rating relevance partly on other
stories' headlines. And a rail that rotates between fetches changes `content_hash`, which is
why a cold run saw four cache hits in ninety-eight. One bug, three symptoms.

**Measured on the real corpus, not on fixtures.** 166 of the 179 URLs in the production
database were re-fetched and both extractions run over the same HTML. Coverage of each
page's own JSON-LD `articleBody` went 551 → 552 sentences out of 707, with **zero pages
covered less** — that is the evidence against over-stripping, which was the failure to fear,
since losing real body would have degraded every assessment silently. Text fell 14.9% overall,
median page ratio 0.83. Same-source similarity collapsed as intended: Cartoon Brew 0.4756 →
0.1413, TechCrunch's worst pair 0.4547 → 0.2081, The Verge 0.4771 → 0.2273.

**Two rules that only real pages could have taught us.** The first matching container wins
unless it is under half the largest match — because Hugging Face's first `<article>` is a
127-character teaser card while the body is a later one, and taking the longest match instead
silently swapped an x.com post for a longer *reply*. And the link-density numerator and
denominator must obey the same exclusion rules; counting anchors inside an excluded `<header>`
against body text that already excluded it drove a plain Cartoon Brew article to density 1.00
and deleted its entire body. Both were caught by measurement, not by review.

**A latent crash was found on the way.** The first implementation recursed over the DOM and
raised `RecursionError` on a 600-deep page the old code survived. That would have killed a
run rather than an article, which rule 7 forbids. Body collection is now depth-capped and
link counting is iterative.

**Consequences, all measured rather than assumed.** The assessment cache is fully invalidated
— `content_hash` is a hash of `clean_text` — so the next run re-analyses roughly 150 articles.
That is a one-time reset, not a recurring cost, and the rail rotation this removes is what was
destroying hash stability in the first place. The entity-fidelity guard did **not** tighten in
practice: replaying it over 152 stored assessments gave seven violations before and seven
after, with no newly unsupported token, so the chrome was vouching for nothing. The similarity
threshold stays at 0.21 — on the publishable pool the true pairs rose and the nearest false
positive fell, widening the separating gap from 0.064 to 0.094. `min_score` was not touched.
The per-source site-furniture rule keeps a smaller place: it still removes template text link
density cannot see, such as "Subscribe for daily Tubefilter Top Stories".

**What to watch.** Link roundups are the shape that loses most — Cartoon Brew's weekly digest
went 6790 → 2053 characters, since its teaser blocks are exactly what link density removes. It
stays publishable on its own headline, but such posts now carry less. And four Ars Technica
pages extract nothing at all, because what they served was a "JavaScript is disabled" bot wall;
they will now fail normalization with a recorded error instead of being analysed as though the
wall were the article. That is an improvement that will look like four new manifest errors.

---

## 2026-08-21 · ADR-0040 · A submitted link takes a slot, the rubric fills the rest

**Decision.** The edition is a bank of ten links (`max_items: 10`) in which reader
submissions are seated **first and guaranteed**. `submissions.reserved_slots` bounds how
many — unset means "one per submission, up to `max_items`", `0` switches the guarantee off
and restores ADR-0028 exactly. Three submissions therefore produce three reserved stories
and seven earned ones. No model call was added, no prompt or schema changed, and no score
was touched: `min_score` is still 62, and a reserved slot bypasses it rather than moving it.

**What a reserved slot bypasses, and why only those.** The four rules a reserved story
skips — `min_score`, `max_per_source`, `max_per_subject`, `section_limits` — exist to
*ration* scarce slots between stories competing for them, and a submission is not competing.
`max_per_source` is the one that made this non-trivial: submissions arrive as a single
synthetic source, so a cap of 2 would have held any edition to two submitted links and made
the owner's workflow impossible to build at all.

Everything else still applies, because it is correctness rather than rationing: the three
deterministic deduplication passes, both collapse passes, cross-edition suppression ("printed
once" is a promise to the reader, not a cap on the submitter), the entity-fidelity guard
(corrupted prose is a defect whoever proposed the link), `excluded_categories`, and
`max_items` — reserved slots come out of the ten, never on top of them.

**A collision keeps the reader's copy.** When a submitted link turns out to be a page a
configured source also carries, `deduplicate` and both collapse passes now prefer the
submission, and the similarity pass treats a reserved submission as publishable whatever it
scored. Both changes are gated on reservation being on. Keeping the outlet's copy instead
would have put the story back into competition, where the score it happens to carry could
lose it — the guarantee would then quietly depend on which page a reader linked to. The
false-positive cost moves with the preference: an over-eager fold now costs a source story
rather than the reader's, which is the trade the owner asked for.

**The lead is still the best story, not merely a submitted one.** `selected` is
reserved-first, so `selected[0]` would have made every submission the lead by fiat. `lead`
is now the minimum of `ranking_key` over the line-up: identical to `selected[0]` when
nothing is reserved, and a submission leads only when it genuinely out-scores the field.

**Ordering is total and data-driven** (AC9). Submissions take slots in the ordinary ranking
order — score descending, then earliest publication, then article id — never insertion order
and never set iteration. `RankedArticle` carries no `submitted_at`, and passing one in would
have added an input to a pure function to reproduce an order those three keys already fix.

**Visibility** (rule 7). `run_manifest.json` gains `articles_reserved`, so
`articles_selected - articles_reserved` is what the rubric earned. While slots are reserved,
*every* rejected submission reaches `withheld` — not only the three reasons ordinarily
recorded there — with its detail prefixed `reader submission:`, because a slot a reader was
promised and did not get is precisely the omission that is invisible from the edition. The
entity guard now records the story it dropped as well as the error that caused it.

**Consequences.** A thin week can now print stories nothing scored for, which is the point.
Because reserved stories are seated first, they also lead their section, so a low-scoring
submitted link can appear above a high-scoring earned one inside the same section — the
owner's "submissions come first" policy, made visible. And a submission still counts against
the caps for everything below it, so seven earned slots stay as diverse as ten used to be.

