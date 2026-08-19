"""Run context — the immutable facts of a single execution.

Created once at the start of a run and passed down the pipeline, so no stage has
to consult the clock, the environment or global state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ConfigDict, Field

from newsletter.config import AppConfig
from newsletter.models import DateWindow, MutableModel, RunManifest


def new_run_id(started_at: datetime) -> str:
    """Sortable, unique run identifier."""
    return f"{started_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"


class RunContext(MutableModel):
    """Everything a stage needs about *this* run."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    started_at: datetime
    config: AppConfig
    window: DateWindow
    manifest: RunManifest
    dry_run: bool = False
    edition_dir: Path = Field(default_factory=lambda: Path("output"))

    @classmethod
    def create(
        cls,
        config: AppConfig,
        window: DateWindow,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RunContext:
        started_at = now or datetime.now(UTC)
        run_id = new_run_id(started_at)
        manifest = RunManifest(
            run_id=run_id,
            started_at=started_at,
            window_start=window.start,
            window_end=window.end,
            dry_run=dry_run,
            analyzer_model=config.runtime.analyzer_model,
            editor_model=config.runtime.editor_model,
        )
        return cls(
            run_id=run_id,
            started_at=started_at,
            config=config,
            window=window,
            manifest=manifest,
            dry_run=dry_run,
            edition_dir=Path(config.runtime.output_dir) / window.issue_label(),
        )

    @property
    def issue_label(self) -> str:
        return self.window.issue_label()

    def finish(self, *, now: datetime | None = None) -> RunManifest:
        """Close the manifest and return it."""
        self.manifest.finished_at = now or datetime.now(UTC)
        return self.manifest
