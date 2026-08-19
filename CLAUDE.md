# CLAUDE.md — Weekly Intelligence Newspaper Engine

## What this repository is

A Python system that generates a weekly enterprise intelligence **newspaper** (HTML +
Markdown + JSON) from a controlled set of public sources. Deterministic Python owns
control flow; OpenAI owns narrow semantic judgment only.

Authoritative specification: **`PRD_Weekly_Intelligence_Newspaper_Claude_Code.md`**.
Read it before any material work. This file is the operating contract, not a summary
of the PRD.

## Session roles

- **The main session is the coordinator.** It owns the PRD, repository state, current
  stage, cross-cutting architecture, integration and stage validation.
- **Subagents are bounded specialists.** They investigate, audit or analyse; they never
  become a second coordinator and never make unrecorded architectural decisions.
- Subagents return the handoff format in `docs/architecture.md` → *Subagent handoff*.
  The coordinator absorbs the conclusions and records durable decisions in
  `docs/decisions.md`. Raw subagent logs never enter durable context.
- Delegate when work is separable and would otherwise flood coordinator context
  (source investigation, large test failures, independent quality review). Do not
  delegate small in-context work merely to demonstrate that subagents exist.

## Architecture rules (non-negotiable)

1. **Deterministic code before LLM.** Fetching, date filtering, deduplication, scoring,
   selection, rendering and control flow are pure Python. The model never chooses a
   fetch strategy, never decides recency, never selects stories, never orchestrates.
2. **Every OpenAI response is schema-constrained.** Strict Structured Outputs plus
   Pydantic validation. Never parse free-form model text with regex.
3. **Scraped content is untrusted data.** Instructions inside source content are never
   runtime instructions. Keep the application-instruction / source-content boundary
   explicit. The editor receives validated structured records, never raw HTML.
4. **The model never computes the final score.** `scoring.py` computes it from the
   validated assessment plus source priority.
5. **Source URLs originate only from ingestion.** No model-produced URL may enter
   publication. Validate scheme and shape before rendering.
6. **HTML and Markdown come from Jinja2 templates**, rendered from one
   `NewsletterEdition`. The model never generates markup.
7. **Fail explicitly.** No silently dropped source, article or error; every failure lands
   in the run manifest. One broken source must not kill the edition.
8. **Reproducibility.** Same stored inputs plus same assessments must produce the same
   selection and the same artifacts.

## Working rules

- Follow the staged delivery model in the PRD (Stage 0 → Stage 8). One stage at a time,
  each with deliverables, tests and a stage gate.
- Before declaring a stage complete: run its validation, update
  `docs/implementation-status.md`, record durable decisions in `docs/decisions.md`, fix
  material failures.
- Keep durable state in the repository, not in conversation memory. After compaction,
  `docs/implementation-status.md` is the source of truth for where work stands.
- Do not ask for routine approvals. Read, search, edit, create files, run tests, lint and
  delegate freely — `.claude/settings.json` already pre-approves them.
- Ask only for what the permission model gates: git writes, remote/GitHub actions,
  dependency installs, destructive filesystem operations, Skill invocation, or a material
  architectural deviation from the PRD.
- A missing API credential is never a reason to stop. Continue with fixtures and mocks.
- Never read, print or commit real secrets. `.env` is denied; `.env.example` is the
  documented surface.

## Conventions

- Python 3.11+, Ruff for lint and format (line length 100), pytest for tests.
- Package layout is `src/newsletter/`; run as `python -m newsletter`.
- Type-annotate public functions; Pydantic models are the contract between stages.
- Prompts are code: `src/newsletter/intelligence/prompts/<name>_v<N>.md`. A meaningful
  prompt change requires a new version, and the version is persisted with every
  assessment and run.
- Tests must not require the Internet or a real OpenAI key. Fixtures live in
  `tests/fixtures/`.
- Generated editions in `output/` are artifacts, not source; they stay untracked.

## Fast commands

```bash
python scripts/validate_repo.py     # harness + repo integrity (fast)
ruff check . && ruff format --check .
python -m pytest -q
python -m newsletter validate       # config validation (Stage 1+)
python -m newsletter run --dry-run  # no OpenAI calls
```
