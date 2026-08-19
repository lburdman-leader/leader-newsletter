---
name: validate-stage
description: >
  Run the complete validation gate for the current implementation stage. Use before
  declaring any stage complete, or when the user asks to validate, check the stage gate,
  or verify the repository state.
allowed-tools: Read, Grep, Glob, Edit, Bash(python -m pytest:*), Bash(pytest:*), Bash(ruff check:*), Bash(ruff format:*), Bash(python scripts/validate_repo.py:*), Bash(python -m newsletter validate:*), Bash(python -m newsletter sources:*), Bash(python -m newsletter run --dry-run:*), Bash(ls:*), Bash(cat:*), Bash(grep:*)
---

# Validate the current stage

## 1. Determine the stage

Read `docs/implementation-status.md` → **Current stage**. Then read that stage gate in
`PRD_Weekly_Intelligence_Newspaper_Claude_Code.md`. The PRD gate is authoritative; the
checks below are how it is executed.

## 2. Always run

```bash
python scripts/validate_repo.py
ruff format --check .
ruff check .
python -m pytest -q
```

Skip a command only when the stage has not created its target yet (for example, no
Python package exists during Stage 0) and say so explicitly in the report.

## 3. Stage-specific gates

| Stage | Additional checks |
|-------|-------------------|
| 0 — harness | `.claude/settings.json` parses; deny rules cover secrets; agents and skills discoverable; `scripts/validate_repo.py` passes |
| 1 — foundation | `python -m newsletter --help`; `python -m newsletter validate`; model and config unit tests |
| 2 — ingestion | fixture-based RSS and Scrapling extraction tests; a failing source is captured, not fatal |
| 3 — data layer | URL canonicalization, date-window boundary, dedupe, persistence round-trip tests; no model calls |
| 4 — intelligence | mocked OpenAI tests: valid structured output, refusal, timeout, bounded retry, cache hit, prompt-version invalidation |
| 5 — ranking | scoring formula tests; selection is identical for identical input; category limits respected |
| 6 — publication | render fixture edition, then inspect the generated HTML and Markdown, not only the templates |
| 7 — end to end | `python -m pytest tests/integration -q`; full fixture pipeline produces all five artifacts |
| 8 — automation | CI workflow runs without an OpenAI key; docs current; `quality-auditor` findings resolved |

## 4. Inspect rendered output (stages 6+)

Never approve a rendering stage from template source alone. Open the generated file and
confirm in the **output**:

- every story headline is wrapped in an `<a href>` to its source URL;
- a visible `Read original` link exists per story with `rel="noopener noreferrer"`;
- Markdown uses standard `[headline](url)` links;
- every URL is `http`/`https` and originated from ingestion, not from a model;
- masthead, issue metadata, executive brief, lead story and at least two sections exist;
- the page reads without JavaScript.

## 5. Report and record

Report per check: PASS / FAIL / SKIPPED (with reason). Then:

- fix material failures before continuing;
- update `docs/implementation-status.md` (current stage, last successful validation,
  known failures, pending debt, next actions);
- record durable decisions in `docs/decisions.md`.

Do not declare a stage complete while any gate item is failing or unrun.
