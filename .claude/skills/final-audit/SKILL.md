---
name: final-audit
description: >
  Run the final project quality checklist against the PRD acceptance criteria and
  delegate an independent review to the quality-auditor subagent. Use before declaring
  the project done or before a release.
allowed-tools: Read, Grep, Glob, Edit, Task, Bash(python -m pytest:*), Bash(ruff check:*), Bash(ruff format:*), Bash(python scripts/validate_repo.py:*), Bash(python -m newsletter validate:*), Bash(python -m newsletter run --dry-run:*), Bash(ls:*), Bash(cat:*), Bash(grep:*)
---

# Final audit

Run this only when every stage gate has passed. It verifies the *whole* system against
the PRD acceptance criteria, then buys an independent opinion.

## 1. Mechanical pass

```bash
python scripts/validate_repo.py
ruff format --check .
ruff check .
python -m pytest -q
python -m pytest tests/integration -q
```

## 2. Acceptance criteria walk-through

Verify each one against evidence in the repository, not against documentation claims:

| AC | Check |
|----|-------|
| AC1 | `python -m newsletter run` produces a full edition when inputs exist |
| AC2 | fixture-based run succeeds with no Internet and no OpenAI key |
| AC3 | every published story maps to a normalized ingestion URL |
| AC4 | rendered HTML: clickable headline + visible original-source link per story |
| AC5 | rendered Markdown: standard link per story |
| AC6 | every published article satisfies the deterministic date window |
| AC7 | every OpenAI call uses strict structured output and Pydantic validation |
| AC8 | the final score is computed only in `ranking/scoring.py` |
| AC9 | identical inputs and assessments produce an identical selection |
| AC10 | one broken source does not stop unrelated sources |
| AC11 | all five artifacts exist for a successful run |
| AC12 | the HTML reads as a newspaper, not a dashboard or a link list |
| AC13 | no model-produced URL can enter publication |
| AC14 | CI needs no live OpenAI credential |
| AC15 | at least one complete fixture pipeline test exists |
| AC16 | `CLAUDE.md`, settings, skills, subagents, hooks, status and decision log present |
| AC17 | subagent results are integrated and durable decisions recorded |
| AC18 | routine operations do not prompt the user |
| AC19 | git writes, remote actions, Skills and destructive operations stay gated |
| AC20 | no secret is committed or read |

## 3. Independent review

Delegate to the `quality-auditor` subagent. Give it: the current stage, the paths that
changed, and the acceptance criteria in scope. Ask for the standard handoff with
severity-tagged findings.

## 4. Resolve and record

- Fix every BLOCKER and CRITICAL finding, then re-verify the fix.
- Report WARNING and INFO findings once; do not silently close them.
- Update `docs/implementation-status.md`, `docs/architecture.md`, `docs/decisions.md`
  and `README.md`.

## 5. Final report

```text
Stages completed / Architecture implemented / Files and modules created /
Skills created / Subagents created / Hooks created / Permission model /
Tests executed / Test results / Generated artifact paths / Known limitations /
Exact command to run / Next logical enhancement
```
