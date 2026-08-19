#!/usr/bin/env python3
"""Lightweight repository integrity validator.

Runs in well under a second so it can be wired to a Claude Code ``Stop`` hook.
It checks the *development harness* invariants (Claude configuration, durable
docs, secret hygiene, syntax) -- it deliberately does not run tests, lint or the
pipeline. Full validation belongs to ``/validate-stage`` and CI.

Usage:
    python scripts/validate_repo.py            # full report
    python scripts/validate_repo.py --quiet    # print only problems

Exit code 0 when there are no errors, 1 otherwise. Warnings never fail the run.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ERRORS: list[str] = []
WARNINGS: list[str] = []
CHECKS: list[str] = []


def error(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def ok(msg: str) -> None:
    CHECKS.append(msg)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def read_frontmatter(path: Path) -> dict[str, str] | None:
    """Parse a minimal top-level ``key: value`` YAML frontmatter block."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    _, _, rest = text.partition("---")
    body, sep, _ = rest.partition("\n---")
    if not sep:
        return None
    data: dict[str, str] = {}
    key: str | None = None
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            data[key] = value.strip()
        elif key:  # folded or continued value
            data[key] = (data[key] + " " + line.strip()).strip()
    return data


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_claude_md() -> None:
    path = ROOT / "CLAUDE.md"
    if not path.is_file():
        error("CLAUDE.md is missing (coordinator contract)")
        return
    if path.stat().st_size < 200:
        error("CLAUDE.md is too short to encode the coordinator contract")
        return
    ok("CLAUDE.md present")


def _load_settings() -> dict | None:
    path = ROOT / ".claude" / "settings.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def check_settings() -> None:
    path = ROOT / ".claude" / "settings.json"
    if not path.is_file():
        error(".claude/settings.json is missing")
        return
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        error(".claude/settings.json does not parse: " + str(exc))
        return

    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        error(".claude/settings.json has no permissions object")
        return

    for key in ("allow", "ask", "deny"):
        if not isinstance(permissions.get(key), list):
            error(".claude/settings.json permissions." + key + " must be a list")
            return

    deny = " ".join(permissions["deny"])
    if ".env" not in deny:
        error("permissions.deny does not protect .env files")
    if "ssh" not in deny:
        warn("permissions.deny does not protect ~/.ssh")

    ask = " ".join(permissions["ask"])
    for guarded in ("git commit", "git push", "Skill"):
        if guarded not in ask:
            error("permissions.ask does not gate " + guarded)

    allow = permissions["allow"]
    if "Bash" in allow or "Bash(*)" in allow:
        error("permissions.allow grants Bash globally; narrow it to specific commands")
    for rule in allow:
        if rule.startswith("Bash(git ") and not rule.startswith(
            (
                "Bash(git status",
                "Bash(git diff",
                "Bash(git log",
                "Bash(git show",
                "Bash(git branch --show-current",
                "Bash(git rev-parse",
                "Bash(git ls-files",
                "Bash(git config --get",
            )
        ):
            error("permissions.allow contains a non-read-only git rule: " + rule)

    ok(".claude/settings.json valid (" + str(len(allow)) + " allow rules)")


def check_hooks() -> None:
    settings = _load_settings()
    if settings is None:
        return
    hooks = settings.get("hooks", {})
    referenced = json.dumps(hooks)
    for script in ("scripts/claude_guard.py", "scripts/validate_repo.py"):
        if script in referenced and not (ROOT / script).is_file():
            error("hook references a missing script: " + script)
    if not hooks:
        warn("no hooks configured in .claude/settings.json")
    else:
        ok("hooks configured (" + ", ".join(sorted(hooks)) + ")")


def check_agents() -> None:
    agents_dir = ROOT / ".claude" / "agents"
    if not agents_dir.is_dir():
        error(".claude/agents/ is missing")
        return
    found = sorted(agents_dir.glob("*.md"))
    if not found:
        error(".claude/agents/ contains no subagent definitions")
        return
    for path in found:
        meta = read_frontmatter(path)
        if meta is None:
            error(rel(path) + " has no YAML frontmatter")
            continue
        name = meta.get("name", "")
        if not name:
            error(rel(path) + " frontmatter has no name")
        elif name != path.stem:
            error(rel(path) + " frontmatter name does not match the filename")
        if not meta.get("description"):
            error(rel(path) + " frontmatter has no description")
        if "Skill" in meta.get("tools", ""):
            warn(rel(path) + " grants Skill to a specialist subagent")
        for forbidden in ("git commit", "git push"):
            if forbidden in meta.get("tools", ""):
                error(rel(path) + " grants git write capability to a subagent")
    ok("subagents discoverable (" + ", ".join(p.stem for p in found) + ")")


def check_skills() -> None:
    skills_dir = ROOT / ".claude" / "skills"
    if not skills_dir.is_dir():
        error(".claude/skills/ is missing")
        return
    found = sorted(d for d in skills_dir.iterdir() if d.is_dir())
    if not found:
        error(".claude/skills/ contains no skills")
        return
    for directory in found:
        skill = directory / "SKILL.md"
        if not skill.is_file():
            error(rel(directory) + " has no SKILL.md")
            continue
        meta = read_frontmatter(skill)
        if meta is None:
            error(rel(skill) + " has no YAML frontmatter")
            continue
        if meta.get("name") != directory.name:
            error(rel(skill) + " frontmatter name must equal its directory name")
        if not meta.get("description"):
            error(rel(skill) + " frontmatter has no description")
    ok("skills discoverable (" + ", ".join(d.name for d in found) + ")")


def check_docs() -> None:
    missing = False
    for name in ("implementation-status.md", "architecture.md", "decisions.md"):
        path = ROOT / "docs" / name
        if not path.is_file():
            error("docs/" + name + " is missing (durable coordinator context)")
            missing = True
        elif path.stat().st_size < 80:
            error("docs/" + name + " is effectively empty")
            missing = True
    if not missing:
        ok("durable coordinator docs present")


def check_secret_hygiene() -> None:
    gitignore = ROOT / ".gitignore"
    ignored = ""
    if not gitignore.is_file():
        error(".gitignore is missing")
    else:
        ignored = gitignore.read_text(encoding="utf-8", errors="replace")
        for pattern in (".env", "output/"):
            if pattern not in ignored:
                error(".gitignore does not ignore " + pattern)

    for candidate in ROOT.rglob(".env"):
        if ".git" in candidate.parts:
            continue
        if ".env" not in ignored:
            error(rel(candidate) + " exists and is not ignored")

    if not (ROOT / ".env.example").is_file():
        warn(".env.example is missing")
    ok("secret hygiene checked")


def check_python_syntax() -> None:
    files: list[Path] = []
    for directory in ("src", "scripts", "tests"):
        base = ROOT / directory
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    for path in files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            error(
                rel(path) + " has a syntax error on line " + str(exc.lineno) + ": " + str(exc.msg)
            )
    ok("python syntax checked (" + str(len(files)) + " files)")


def check_configs() -> None:
    config_dir = ROOT / "config"
    if not config_dir.is_dir():
        return
    yaml_files = sorted(config_dir.glob("*.yaml")) + sorted(config_dir.glob("*.yml"))
    if not yaml_files:
        return
    try:
        import yaml
    except ImportError:
        warn("PyYAML not installed; skipped config YAML parse check")
        return
    for path in yaml_files:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            error(rel(path) + " is not valid YAML: " + str(exc))
    ok("config YAML parsed (" + str(len(yaml_files)) + " files)")


CHECK_FUNCTIONS = (
    check_claude_md,
    check_settings,
    check_hooks,
    check_agents,
    check_skills,
    check_docs,
    check_secret_hygiene,
    check_python_syntax,
    check_configs,
)


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv or "-q" in argv

    for check in CHECK_FUNCTIONS:
        try:
            check()
        except Exception as exc:  # a validator must never break the session
            error(check.__name__ + " crashed: " + exc.__class__.__name__ + ": " + str(exc))

    if not quiet:
        for line in CHECKS:
            print("  ok   " + line)
    for line in WARNINGS:
        print("  warn " + line)
    for line in ERRORS:
        print("  FAIL " + line, file=sys.stderr)

    if ERRORS:
        summary = (
            "validate_repo: "
            + str(len(ERRORS))
            + " error(s), "
            + str(len(WARNINGS))
            + " warning(s)"
        )
        print(summary, file=sys.stderr)
        return 1
    if not quiet:
        print(
            "validate_repo: OK ("
            + str(len(CHECKS))
            + " checks, "
            + str(len(WARNINGS))
            + " warning(s))"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
