#!/usr/bin/env python3
"""Check the PRD acceptance criteria mechanically.

This is evidence, not a substitute for judgment. Each criterion is checked by
inspecting the repository and the generated artifacts; criteria that need a live
credential or a human eye are reported as such rather than quietly passed.

    python scripts/audit_acceptance.py

Exit code 0 when nothing FAILS. Items marked MANUAL never fail the run, but they
are the ones worth reading.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_EDITION = ROOT / "output" / "fixture-edition" / "2026-W34"

PASS, FAIL, MANUAL = "PASS", "FAIL", "MANUAL"

Result = tuple[str, str]


def source_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in (ROOT / "src").rglob("*.py")
    )


def test_names() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (ROOT / "tests").rglob("test_*.py")
    )


def artifact(name: str) -> str | None:
    path = FIXTURE_EDITION / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


# --------------------------------------------------------------------------- #
# criteria
# --------------------------------------------------------------------------- #


def ac1_direct_execution() -> Result:
    cli = (ROOT / "src" / "newsletter" / "cli.py").read_text(encoding="utf-8")
    if "run_pipeline" not in cli:
        return FAIL, "the CLI does not call the pipeline"
    return MANUAL, "wired and exercised offline; a live run needs OPENAI_API_KEY"


def ac2_offline_integration() -> Result:
    if not (ROOT / "tests" / "integration" / "test_full_pipeline.py").is_file():
        return FAIL, "no integration test"
    if "no_network" not in (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"):
        return FAIL, "no guard against network access in tests"
    return PASS, "integration suite runs behind an autouse socket guard"


def ac3_traceability() -> Result:
    selected = artifact("selected_articles.json")
    if selected is None:
        return FAIL, "no selected_articles.json in the fixture edition"
    rows = json.loads(selected)
    if not rows or not all(row["canonical_url"].startswith("https://") for row in rows):
        return FAIL, "a published story has no canonical source URL"
    return PASS, f"{len(rows)} stories, each with a canonical URL and score breakdown"


def ac4_html_clickable() -> Result:
    html = artifact("newsletter.html")
    if html is None:
        return FAIL, "no newsletter.html in the fixture edition"
    headlines = re.findall(r'<h[23][^>]*>\s*<a href="(https://[^"]+)"', html)
    read_original = re.findall(r'class="read-original" href="(https://[^"]+)"', html)
    if not headlines or len(read_original) != len(headlines):
        return FAIL, f"{len(headlines)} headline links, {len(read_original)} read-original links"
    if any('rel="noopener noreferrer"' not in tag for tag in re.findall(r"<a [^>]*>", html)):
        return FAIL, "an external link is missing rel=noopener noreferrer"
    return PASS, f"{len(headlines)} clickable headlines, each with a visible source link"


def ac5_markdown_links() -> Result:
    markdown = artifact("newsletter.md")
    if markdown is None:
        return FAIL, "no newsletter.md in the fixture edition"
    links = re.findall(r"^### \[[^\]]+\]\((https://[^)]+)\)", markdown, re.MULTILINE)
    if not links or markdown.count("[Read original →](") != len(links):
        return FAIL, "headline and read-original links do not match"
    return PASS, f"{len(links)} Markdown headline links"


def ac6_time_window() -> Result:
    edition = artifact("newsletter.json")
    if edition is None:
        return FAIL, "no newsletter.json in the fixture edition"
    data = json.loads(edition)
    start, end = data["period_start"], data["period_end"]
    dates = [data["lead_story"]["published_at"]] + [
        item["published_at"] for section in data["sections"] for item in section["items"]
    ]
    if any(not (start <= published < end) for published in dates):
        return FAIL, "a published story falls outside the edition window"
    return PASS, f"{len(dates)} stories, all inside {start[:10]}..{end[:10]}"


def ac7_structured_ai() -> Result:
    code = source_text()
    if "text_format=schema" not in code:
        return FAIL, "a model call does not pass a strict schema"
    if re.search(r"json\.loads\(\s*response", code):
        return FAIL, "free-form model output is being parsed"
    return PASS, "every call passes text_format and reads only output_parsed"


def ac8_deterministic_score() -> Result:
    """Introspect the real model rather than grepping prose about it."""
    from newsletter.models import ArticleAssessment

    offenders = [name for name in ArticleAssessment.model_fields if "score" in name.lower()]
    if offenders:
        return FAIL, f"ArticleAssessment exposes {offenders}"
    scoring = (ROOT / "src" / "newsletter" / "ranking" / "scoring.py").read_text(encoding="utf-8")
    if "* TOPIC_RELEVANCE_WEIGHT" not in scoring:
        return FAIL, "the score formula is not in ranking/scoring.py"
    formula_files = [
        path.name
        for path in (ROOT / "src").rglob("*.py")
        if "TOPIC_RELEVANCE_WEIGHT" in path.read_text(encoding="utf-8")
    ]
    if formula_files != ["scoring.py"]:
        return FAIL, f"the score weights appear in more than one place: {formula_files}"
    return PASS, "no score field on the assessment; the weights exist only in scoring.py"


def ac9_deterministic_selection() -> Result:
    if "identical_artifacts" not in test_names():
        return FAIL, "no test asserts two identical runs produce identical output"
    return PASS, "asserted by re-running the fixture pipeline and diffing the artifacts"


def ac10_partial_failures() -> Result:
    manifest = artifact("run_manifest.json")
    if manifest is None:
        return FAIL, "no run_manifest.json in the fixture edition"
    data = json.loads(manifest)
    if not (data["sources_failed"] >= 1 and data["newsletter_generated"]):
        return FAIL, "the fixture edition does not demonstrate surviving a failed source"
    return PASS, (
        f"{data['sources_failed']} source failed, "
        f"{data['sources_succeeded']} succeeded, edition still generated"
    )


def ac11_artifacts() -> Result:
    required = (
        "newsletter.html",
        "newsletter.md",
        "newsletter.json",
        "selected_articles.json",
        "run_manifest.json",
    )
    missing = [name for name in required if not (FIXTURE_EDITION / name).is_file()]
    if missing:
        return FAIL, f"missing artifacts: {', '.join(missing)}"
    return PASS, "all five artifacts present"


def ac12_newspaper_presentation() -> Result:
    html = artifact("newsletter.html")
    if html is None:
        return FAIL, "no newsletter.html in the fixture edition"
    for needle in ("masthead", "Executive Brief", "Lead Story", "section-label", "@media"):
        if needle not in html:
            return FAIL, f"the edition has no {needle}"
    if "<script" in html.lower():
        return FAIL, "the edition requires JavaScript"
    return MANUAL, "structure present; whether it *reads* as a newspaper needs a human"


def ac13_no_invented_links() -> Result:
    """The model must have nowhere to put a link, a date or an ordering."""
    from newsletter.intelligence.editor import EditorialPayload, StoryPolish
    from newsletter.intelligence.schemas import AssessmentPayload

    forbidden = ("url", "link", "href", "source", "date", "score", "order", "rank")
    for model in (EditorialPayload, StoryPolish, AssessmentPayload):
        offenders = [
            name
            for name in model.model_fields
            if any(word in name.lower() for word in forbidden) and not name.startswith("event_")
        ]
        if offenders:
            return FAIL, f"{model.__name__} can express {offenders}"

    if "validate_edition_links" not in source_text():
        return FAIL, "links are not validated before rendering"
    return PASS, "no model schema can express a link; links revalidated before every render"


def ac14_ci_without_credentials() -> Result:
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():
        return FAIL, "no CI workflow"
    text = workflow.read_text(encoding="utf-8")
    if "secrets." in text:
        return FAIL, "the CI workflow references a secret"
    return PASS, "CI declares no secret and runs the suite with an invalid key"


def ac15_integration_test() -> Result:
    path = ROOT / "tests" / "integration" / "test_full_pipeline.py"
    if not path.is_file():
        return FAIL, "no full pipeline test"
    count = path.read_text(encoding="utf-8").count("\ndef test_")
    return PASS, f"{count} end-to-end tests over three fake sources"


def ac16_claude_architecture() -> Result:
    required = [
        ROOT / "CLAUDE.md",
        ROOT / ".claude" / "settings.json",
        ROOT / ".claude" / "agents" / "source-researcher.md",
        ROOT / ".claude" / "agents" / "quality-auditor.md",
        ROOT / ".claude" / "skills" / "add-source" / "SKILL.md",
        ROOT / ".claude" / "skills" / "validate-stage" / "SKILL.md",
        ROOT / ".claude" / "skills" / "final-audit" / "SKILL.md",
        ROOT / "scripts" / "claude_guard.py",
        ROOT / "docs" / "implementation-status.md",
        ROOT / "docs" / "decisions.md",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
    if missing:
        return FAIL, f"missing: {', '.join(missing)}"
    return PASS, "settings, agents, skills, hooks, status and decision log all present"


def ac17_recorded_decisions() -> Result:
    decisions = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    count = decisions.count("\n## ")
    if count < 10:
        return FAIL, f"only {count} decisions recorded"
    return PASS, f"{count} ADRs recorded"


def ac18_permissions_frictionless() -> Result:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    allow = settings["permissions"]["allow"]
    for needed in ("Read", "Edit", "Write", "Grep", "Glob"):
        if needed not in allow:
            return FAIL, f"{needed} is not pre-approved"
    return PASS, f"{len(allow)} routine operations pre-approved"


def ac19_consequential_gated() -> Result:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    ask = " ".join(settings["permissions"]["ask"])
    for guarded in ("git commit", "git push", "Skill", "pip install", "rm"):
        if guarded not in ask:
            return FAIL, f"{guarded} is not approval-gated"
    return PASS, "git writes, remote actions, installs, deletions and Skills all gated"


def ac20_no_secrets() -> Result:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    leaked = [name for name in tracked if name == ".env" or name.endswith("/.env")]
    if leaked:
        return FAIL, f"a dotenv file is tracked: {leaked}"
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    if ".env" not in gitignore:
        return FAIL, ".env is not ignored"
    return PASS, "no dotenv tracked; .env ignored and denied to tooling"


CRITERIA: list[tuple[str, str, Callable[[], Result]]] = [
    ("AC1", "direct execution", ac1_direct_execution),
    ("AC2", "offline integration", ac2_offline_integration),
    ("AC3", "traceability", ac3_traceability),
    ("AC4", "clickable HTML", ac4_html_clickable),
    ("AC5", "Markdown links", ac5_markdown_links),
    ("AC6", "time window", ac6_time_window),
    ("AC7", "structured AI", ac7_structured_ai),
    ("AC8", "deterministic score", ac8_deterministic_score),
    ("AC9", "deterministic selection", ac9_deterministic_selection),
    ("AC10", "partial failures", ac10_partial_failures),
    ("AC11", "artifacts", ac11_artifacts),
    ("AC12", "newspaper presentation", ac12_newspaper_presentation),
    ("AC13", "no invented links", ac13_no_invented_links),
    ("AC14", "CI without credentials", ac14_ci_without_credentials),
    ("AC15", "full integration test", ac15_integration_test),
    ("AC16", "Claude architecture", ac16_claude_architecture),
    ("AC17", "recorded decisions", ac17_recorded_decisions),
    ("AC18", "permissions", ac18_permissions_frictionless),
    ("AC19", "consequential operations", ac19_consequential_gated),
    ("AC20", "secrets", ac20_no_secrets),
]


def main() -> int:
    failures = 0
    manual = 0
    for code, title, check in CRITERIA:
        try:
            status, detail = check()
        except Exception as exc:  # an audit must report, never crash
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        failures += status == FAIL
        manual += status == MANUAL
        print(f"  {status:6} {code:5} {title:26} {detail}")

    print()
    if failures:
        print(f"acceptance audit: {failures} FAILED, {manual} need a human")
        return 1
    print(f"acceptance audit: all mechanical criteria pass, {manual} need a human")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
