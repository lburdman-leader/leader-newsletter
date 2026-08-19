"""Regenerate output/fixture-edition/ from the integration pipeline.

The audit and the README both point at this directory for an edition you can
open without a key or a network. It is generated and gitignored, so it has to be
rebuilt whenever the templates change.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()
if not (ROOT / "pyproject.toml").exists():
    raise SystemExit("run this from the repository root")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.conftest import FakeHttpClient  # noqa: E402
from tests.integration import test_full_pipeline as harness  # noqa: E402

# The same three sources the integration suite runs: alpha and beta serve
# fixtures, gamma is down, so the sample also shows a partial failure.
http = FakeHttpClient(
    {
        harness.ALPHA_FEED: harness.fixture("alpha_feed.xml"),
        harness.ALPHA_1: harness.fixture("alpha_article_1.html"),
        harness.ALPHA_2: harness.fixture("alpha_article_2.html"),
        harness.BETA_INDEX: harness.fixture("beta_index.html"),
        harness.BETA_1: harness.fixture("beta_article_1.html"),
        harness.BETA_2: harness.fixture("beta_article_2.html"),
    },
    failures={harness.GAMMA_FEED: "connection refused"},
)

target = ROOT / "output" / "fixture-edition"
with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    result = harness.run_fixture_pipeline(tmp_path, http)
    if not result.succeeded:
        raise SystemExit("the fixture pipeline did not produce an edition")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(tmp_path / "output", target)

for path in sorted(target.rglob("*")):
    if path.is_file():
        print(f"  {path.relative_to(ROOT)}")
