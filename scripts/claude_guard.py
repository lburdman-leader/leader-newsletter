#!/usr/bin/env python3
"""PreToolUse guard hook for Claude Code (Bash tool).

Deterministic, fast and narrow: it blocks a short list of clearly unsafe or
secret-exposing shell commands. It is not a sandbox and it is not a substitute
for the permission rules in ``.claude/settings.json`` -- it is the last line of
defence against shell-level bypasses of those rules.

Protocol
--------
stdin  : JSON payload from Claude Code (``tool_name``, ``tool_input``, ...)
exit 0 : allow the command
exit 2 : block the command; stderr is returned to Claude as the reason
"""

from __future__ import annotations

import json
import re
import sys

# Files whose name starts with ".env" but that are safe to read/commit.
_SAFE_ENV_SUFFIX = r"(?!\.(?:example|sample|template|dist))"

# Matches a real dotenv file, but not `myapp.env` and not `.env.example`.
_DOTENV = r"(?<![\w.-])\.env" + _SAFE_ENV_SUFFIX + r"(\.[\w.-]+)?\b"

_READERS = r"cat|type|more|less|head|tail|strings|nl|xxd|od|bat|Get-Content|gc"

# Each rule is (compiled pattern, human readable reason).
_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(" + _READERS + r")\b[^|;&\n]*" + _DOTENV, re.IGNORECASE),
        "reading a dotenv file is denied; read .env.example instead",
    ),
    (
        re.compile(r"\bgit\s+add\b[^|;&\n]*" + _DOTENV, re.IGNORECASE),
        "staging a dotenv file is denied; secrets must never be committed",
    ),
    (
        re.compile(r"(?:^|[;|&]\s*)(printenv|env)\b(?![^|;&\n]*=)", re.IGNORECASE),
        "dumping the environment can expose secrets; read one named variable in code instead",
    ),
    (
        re.compile(
            r"\becho\b[^|;&\n]*\$\{?\w*(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)",
            re.IGNORECASE,
        ),
        "echoing a credential environment variable is denied",
    ),
    (
        re.compile(r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba)?sh\b", re.IGNORECASE),
        "piping a download straight into a shell is denied",
    ),
    (
        re.compile(r"\brm\s+(-\S+\s+)*-\S*[rR]\S*\s+(/|~|\$HOME)(\s|$)"),
        "recursive deletion of a root or home path is denied",
    ),
    (
        re.compile(r"\bgit\s+push\b[^|;&\n]*(--force(?!-with-lease)|\s-f\b)", re.IGNORECASE),
        "force push is denied; ask the user and prefer --force-with-lease",
    ),
    (
        re.compile(r"\bgit\s+clean\b[^|;&\n]*-\S*x", re.IGNORECASE),
        "git clean -x removes ignored and untracked work; ask the user instead",
    ),
    (
        re.compile(r"--dangerously-skip-permissions"),
        "--dangerously-skip-permissions is forbidden by the project PRD",
    ),
]


def check(command: str) -> str | None:
    """Return the block reason for ``command``, or None when it is allowed."""
    for pattern, reason in _RULES:
        if pattern.search(command):
            return reason
    return None


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Never break the session because the hook payload changed shape.
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = str(payload.get("tool_input", {}).get("command", ""))
    if not command:
        return 0

    reason = check(command)
    if reason:
        print(f"Blocked by scripts/claude_guard.py: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
