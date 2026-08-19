---
name: add-source
description: >
  Add or modify a newsletter source safely. Use when the user asks to add, change,
  disable or debug a source in config/sources.yaml. Investigates the source, picks the
  cheapest working fetch strategy, updates config, captures a fixture and adds an
  extraction test.
allowed-tools: Read, Grep, Glob, Edit, Write, WebFetch, Task, Bash(python -m newsletter sources:*), Bash(python -m pytest:*), Bash(ruff check:*), Bash(python scripts/validate_repo.py:*)
---

# Add or modify a newsletter source

A source is only "added" when it is configured, covered by an offline fixture test, and
validated. Configuration alone is not enough.

## 1. Investigate

Delegate to the `source-researcher` subagent with the entrypoint URL. Web exploration
and DOM inspection must stay out of the coordinator context.

Absorb its handoff: feed availability, date semantics, canonical URL source, selectors,
recommended strategy, risks.

## 2. Choose the strategy

Take the cheapest option that actually works, in this order:

```text
rss  →  scrapling_static  →  scrapling_dynamic  →  scrapling_stealth
```

Anything beyond `scrapling_static` needs an observed justification (JS-rendered content,
bot wall, 403) recorded in `docs/decisions.md`. Never let a model choose the strategy at
runtime — it is static configuration.

## 3. Update configuration

Edit `config/sources.yaml`:

```yaml
- id: kebab-case-stable-id       # never reused for a different source
  name: Human Readable Name
  category_hint: ai_models       # closed taxonomy only
  entrypoint: "https://..."
  strategy: rss
  priority: 8                    # 0-10, feeds the deterministic score
  enabled: true
  selectors: {}                  # only for scrapling_* strategies
```

`category_hint` must be one of `youtube_platform`, `youtube_monetization`, `ai_models`,
`ai_video`, `ai_business`, `other`. It is a hint for the analyzer, not a final category.

## 4. Capture a fixture

Save a real, small, representative response under `tests/fixtures/sources/<source-id>/`
(feed XML, index HTML, one article HTML). Strip nothing that the parser depends on;
truncate aggressively otherwise. Fixtures make CI offline and reproducible.

## 5. Add an extraction test

In `tests/unit/test_ingestion_<source_id>.py`, assert against the fixture:

- discovery returns the expected number of articles and their URLs;
- titles are extracted;
- publication dates parse to timezone-aware datetimes with the expected values;
- canonical URL is the canonicalized original source URL;
- a malformed or missing date is handled explicitly, never invented.

## 6. Validate

```bash
python -m newsletter sources
python -m pytest tests/unit -q
ruff check .
python scripts/validate_repo.py
```

## 7. Report

State: strategy chosen and why, date semantics, fixture path, test added, and the
limitations (paywall, JS dependence, rate limits, robots restrictions, fragile
selectors). If the source cannot be supported reliably, say so and leave it
`enabled: false` rather than shipping a flaky adapter.
