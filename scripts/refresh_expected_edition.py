#!/usr/bin/env python3
"""Regenerate the golden edition fixtures and an inspectable sample.

The golden files pin rendering output so that a template change shows up as a
readable diff instead of a silent visual regression.

    python scripts/refresh_expected_edition.py            # refresh fixtures
    python scripts/refresh_expected_edition.py --sample   # also write output/sample/

Review the diff before committing: if the change is intended, the diff *is* the
review; if it is not, the test that compares against these files has just earned
its keep.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.unit.test_renderer import build_fixture_edition  # noqa: E402

from newsletter.rendering.renderer import (  # noqa: E402
    render_html,
    render_json,
    render_markdown,
    write_edition,
)

FIXTURES = ROOT / "tests" / "fixtures"
TAGLINE = "Platform, model and monetization intelligence for the week"


def main(argv: list[str]) -> int:
    edition, ranked = build_fixture_edition()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "expected_newsletter.md").write_text(
        render_markdown(edition), encoding="utf-8", newline="\n"
    )
    (FIXTURES / "expected_newsletter.json").write_text(
        render_json(edition), encoding="utf-8", newline="\n"
    )
    (FIXTURES / "expected_newsletter.html").write_text(
        render_html(edition, tagline=TAGLINE), encoding="utf-8", newline="\n"
    )
    print(f"refreshed golden fixtures in {FIXTURES}")

    if "--sample" in argv:
        target = ROOT / "output" / "sample-edition"
        written = write_edition(edition, target, ranked=ranked, tagline=TAGLINE)
        print("wrote a browsable sample edition:")
        for name, path in written.items():
            print(f"  {name:18} {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
