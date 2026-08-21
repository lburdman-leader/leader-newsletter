# Weekly Intelligence Newspaper Engine

Generates a weekly intelligence **newspaper** — HTML, Markdown and JSON — from a
controlled set of public sources covering YouTube platform changes, YouTube monetization,
children's and family content, AI video and creative AI, new AI models and APIs, and AI
developments with concrete business impact.

The edition is written **in Spanish**, for the team at Leader Entertainment: a Latin
American company that makes children's content on YouTube and is becoming an AI content
company. Audience is not a rendering concern here — it is in the rubric the model rates
against and in the brief the editor writes (see
[ADR-0032](docs/decisions.md)).

The design principle is deliberate: **AI for judgment, software for rules and
guarantees.** Discovery, date filtering, deduplication, scoring, selection and rendering
are deterministic Python. OpenAI is used only for narrow, schema-constrained semantic
judgment, and never decides what gets published.

Full specification: [`PRD_Weekly_Intelligence_Newspaper_Claude_Code.md`](PRD_Weekly_Intelligence_Newspaper_Claude_Code.md).
Current state: [`docs/implementation-status.md`](docs/implementation-status.md).

## Quick start

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
cp .env.example .env          # add OPENAI_API_KEY for live runs

python -m newsletter validate         # check configuration
python -m newsletter run --dry-run    # fetch sources, no OpenAI call, no files written
python -m newsletter run              # generate this week's edition
```

Open `output/<issue>/newsletter.html` in a browser. For an example that needs neither a
key nor a network, run `python scripts/refresh_fixture_edition.py` and open
`output/fixture-edition/2026-W34/newsletter.html`.

## How it works

```text
LOAD CONFIG → DISCOVER → FETCH → NORMALIZE → HARD FILTER → DEDUPLICATE
→ ANALYZE → SCORE → SELECT → EDITORIAL SYNTHESIS → VALIDATE → RENDER → PERSIST RUN REPORT
```

The model reads one article at a time and returns a strict structured assessment:
a category from a closed list, four 0-5 ratings, a factual summary, an interpretation, and
an event fingerprint. It never sees a URL it could echo back, never returns a score, and
never chooses what gets published.

Python computes the score:

```text
topic_relevance ×6 + business_impact ×5 + novelty ×4 + actionability ×3 + source_priority
= 0-100        (default publication threshold: 70)
```

Then Python selects: threshold, per-category caps so one topic cannot take over the
edition, a deterministic tie-break, and the top story as the lead. The editorial model is
consulted *after* that, and may only reword headlines and write the executive brief — its
response schema has no field for a URL, a source, a date, an order or a score.

## Commands

```bash
python -m newsletter run                                  # this week's edition
python -m newsletter run --from 2026-08-11 --to 2026-08-17
python -m newsletter run --dry-run                        # no OpenAI calls, nothing written
python -m newsletter validate                             # validate configuration
python -m newsletter sources                              # list configured sources
python -m newsletter submit <url> --by NAME --note TEXT   # propose a story
python -m newsletter submissions [--status pending]       # what happened to each
python -m newsletter serve [--host ADDR --port N]         # the edition and the reader form

python -m pytest                                          # 485 tests, no network, no key
ruff check . && ruff format --check .
python scripts/validate_repo.py                           # repository + harness integrity
python scripts/refresh_expected_edition.py --sample       # regenerate golden edition
python scripts/audit_acceptance.py                        # PRD acceptance criteria
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | configuration or runtime error |
| 2 | usage error |
| 4 | nothing to publish — no story cleared the threshold. A quiet week, not a failure. |

A scheduled job should treat 4 as normal and 1 as an alert.

## Output

```text
output/2026-W34/
  newsletter.html          standalone newspaper edition (the primary artifact)
  newsletter.md            the same edition as Markdown
  newsletter.json          the structured edition
  selected_articles.json   every published story with its score breakdown and assessment
  run_manifest.json        counts per stage, every error, model and prompt versions, paths
```

Every story carries a clickable headline and a visible `Read original →` link. URLs come
only from ingestion — a model cannot introduce one, because its schema has nowhere to put
one. The HTML is self-contained: inline CSS, no JavaScript, no external requests, and it
prints sensibly.

Generated editions are artifacts, not source, and are not committed.

## Configuration

Behaviour lives in YAML. Only secrets, paths and model names come from the environment.

**`config/newsletter.yaml`** — masthead, timezone, window length and mode, `min_score`,
`max_items`, per-category limits, section order, excluded categories, event collapse.

**`config/sources.yaml`** — one entry per source:

```yaml
- id: openai-news
  name: OpenAI News
  category_hint: ai_models
  entrypoint: "https://openai.com/news/rss.xml"
  strategy: rss          # rss | scrapling_static | scrapling_dynamic | scrapling_stealth
  priority: 10           # 0-10, added directly to the score
  enabled: true
```

Prefer the cheapest strategy that works: `rss → scrapling_static → scrapling_dynamic →
scrapling_stealth`. Scraping strategies take a `selectors` block; the keys are documented
at the top of `config/sources.yaml`. Browser strategies additionally need
`pip install 'scrapling[fetchers]' && scrapling install`, and should be justified by
observed source behaviour rather than convenience.

Inside Claude Code, `/add-source` runs the whole procedure: investigate the source, choose
a strategy, configure it, capture a fixture, add an extraction test, validate.

### Environment variables (`.env`)

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | required for live runs; omit it and use `--dry-run` |
| `OPENAI_ANALYZER_MODEL` / `OPENAI_EDITOR_MODEL` | override the configured models |
| `NEWSLETTER_DB_PATH` / `NEWSLETTER_OUTPUT_DIR` | override paths |
| `LOG_LEVEL` | `DEBUG` … `CRITICAL` |
| `NEWSLETTER_ANALYSIS_CONCURRENCY` / `NEWSLETTER_FETCH_CONCURRENCY` | how many model calls / article fetches run at once (8 and 6; `1` is one at a time) |

`.env` is never committed, and the development harness blocks tooling from reading it.

## Anyone can suggest a story

```bash
python -m newsletter submit https://example.com/article --by "Ana" --note "why it matters"
python -m newsletter submissions             # what happened to each one
python -m newsletter submissions --status rejected
```

A submitted link joins the next run as an ordinary candidate: fetched, normalized,
deduplicated, assessed and scored by exactly the same code as everything else. It then
**takes one of the edition's ten slots by right**: reader submissions are seated first and
the rubric fills whatever is left, so three submissions mean three reserved stories and
seven earned ones. A reserved slot skips the score floor and the per-source, per-subject
and per-section caps — the rules that ration slots between competing stories — and skips
nothing that protects the edition: duplicates, stories an earlier issue printed, corrupted
prose and excluded categories are refused whoever proposed them, and every refusal is named
in `run_manifest.json`. Set `submissions.reserved_slots` to `0` to switch the guarantee off
and make submissions compete on score again.

Every submission ends up with a reason you can read:

| Status | Meaning |
|--------|---------|
| `published` | it ran in that issue |
| `approved` | good enough, but it did not fit this edition; it will be considered again |
| `rejected` | outside the window, a duplicate, the page could not be read — or, with `reserved_slots: 0`, below the threshold |
| `pending` | not reached yet — the per-run cap applies |

The `--note` is for humans only and is **never** shown to the model: otherwise submitting a
link would be a way to write the analyst's prompt. Submitted URLs must be `https`, are
checked against a host blocklist, and are refused if they resolve into private or loopback
address space. Tune it under `submissions:` in `config/newsletter.yaml`, or set
`enabled: false` to close the door.

### From the newsletter itself, without a terminal

```bash
python -m newsletter serve            # http://127.0.0.1:8765/         the latest edition
                                      # http://127.0.0.1:8765/submit   the form
```

`/` serves the most recently **generated** edition — the one the database recorded last, not
the newest file on disk — read from `<output_dir>/<issue_label>/newsletter.html`. The issue
label comes from the database and never from the request, so there is nothing in the URL to
point at another file; anything else is a 404. Before the first run there is nothing to
serve, and the page says so and names the command that fixes it.

The form asks for three things — **full name**, **link**, and an optional **description** —
and writes them straight into the `submissions` table of the configured database
(`NEWSLETTER_DATABASE_URL`, SQLite or PostgreSQL). They land as `pending`, and the next
`newsletter run` picks up every pending one, subject to `max_per_run`. Resubmitting the
same link updates that submission instead of creating a second one: the id is a hash of
the canonical URL. The link goes through the same gate as `newsletter submit` — https,
blocklist, and a refusal to resolve into private address space.

Set `submissions.form_url` in `config/newsletter.yaml` to the address readers can actually
reach, and every rendered edition prints a call to action linking to it, opening in a new
window: a button on the masthead, where nobody has to scroll to find it, and a second
invitation at the foot of the HTML edition. Leave it empty and the edition prints nothing at
all, in either place — an intake nobody can reach is worse than no invitation.

`serve` binds to `127.0.0.1` on purpose: **the form has no authentication**, so whoever can
reach it can queue links. Exposing it is a deployment decision. The application is a plain
WSGI callable, so a real deployment runs it behind a real server — `gunicorn
newsletter.web.app:application` — with TLS, logging and whatever rate limiting the exposure
deserves, rather than pointing `--host 0.0.0.0` at the world.

## Automation

- **`.github/workflows/ci.yml`** — push and pull request, Python 3.11–3.14: formatting,
  lint, repository integrity, unit tests, the integration fixture pipeline, and a render
  check that fails if the golden edition changes. **No secret is required**; the suite is
  offline by construction.
- **`.github/workflows/weekly-newsletter.yml`** — `workflow_dispatch` plus a weekly
  schedule. Reads `OPENAI_API_KEY` from GitHub Secrets, caches the assessment database
  between runs, and uploads the edition as a workflow artifact. It does not email, deploy,
  publish, commit or push anything.

## Development with Claude Code

| Path | Purpose |
|------|---------|
| `CLAUDE.md` | coordinator contract and the non-negotiable architecture rules |
| `.claude/settings.json` | permissions — routine work frictionless, consequential actions gated — plus hooks |
| `.claude/agents/` | `source-researcher`, `quality-auditor` — read-only specialists |
| `.claude/skills/` | `/add-source`, `/validate-stage`, `/final-audit` |
| `scripts/claude_guard.py` | pre-execution guard against unsafe or secret-exposing shell commands |
| `scripts/validate_repo.py` | fast repository integrity validator (also a `Stop` hook) |
| `docs/architecture.md` | the architecture as built |
| `docs/decisions.md` | ADR-style log of every durable decision and why |

## Limitations

- **Prompt quality is unproven.** No live run has reached the model yet; every artifact so
  far was produced with a mocked SDK. The first real run is the first test of the prompts,
  the cost, and whether these sources make a good newspaper.
- **Scraping depends on other people's markup.** Sources break; the design expects it and
  records each failure in the run manifest rather than losing the edition.
- **Source quality is unjudged.** The nine enabled sources are verified as reachable and
  parseable, not as editorially good.
- **No delivery.** Email, subscribers, a web frontend and authentication are explicit
  non-goals for this MVP. The artifact is ready to send; sending it is not built.
