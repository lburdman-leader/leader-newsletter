"""Logging and console reporting.

Two distinct channels, deliberately:

* ``logging`` (stderr) — diagnostics for developers and CI;
* :func:`report` (stdout) — the short, human-readable run narrative required by
  the PRD, e.g. ``+ 47 articles discovered``.

Never silently drop a failure: :func:`report_failure` exists so that a degraded
stage is still visible on the console even when the run continues.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging once, idempotently."""
    global _CONFIGURED
    resolved = getattr(logging, level.upper(), logging.INFO)
    if _CONFIGURED:
        logging.getLogger().setLevel(resolved)
        return
    logging.basicConfig(
        level=resolved,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"newsletter.{name}")


def _supports(symbol: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        symbol.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


#: Windows consoles still default to legacy code pages; degrade instead of crashing.
_OK = "✓" if _supports("✓") else "+"
_FAIL = "✗" if _supports("✗") else "!"


def report(message: str) -> None:
    """Print a successful step to stdout."""
    print(f"{_OK} {message}")


def report_failure(message: str) -> None:
    """Print a failed but non-fatal step to stdout, so it cannot be overlooked."""
    print(f"{_FAIL} {message}")


def report_plain(message: str = "") -> None:
    print(message)
