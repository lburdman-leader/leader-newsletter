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
