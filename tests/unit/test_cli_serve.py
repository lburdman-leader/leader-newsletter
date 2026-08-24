"""``serve`` and ``submit`` under a closed intake.

The two commands share a flag and must not share a verdict. ``submit`` is intake,
so it refuses. ``serve`` publishes the newspaper and only incidentally hosts the
form, so it starts anyway -- otherwise turning intake off takes the edition
offline with it, which is not what the flag says.
"""

from __future__ import annotations

import argparse
import wsgiref.simple_server
from pathlib import Path

import pytest

from newsletter import cli
from newsletter.config import AppConfig, NewsletterSettings, RuntimeSettings, SubmissionSettings
from newsletter.models import SourceConfig


class FakeServer:
    """A bound server that never runs, so no socket is ever opened."""

    def __init__(self) -> None:
        self.served = False

    def serve_forever(self) -> None:
        self.served = True
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


def make_config(tmp_path: Path, *, enabled: bool) -> AppConfig:
    return AppConfig(
        sources=[
            SourceConfig(
                id="wire",
                name="Wire Example",
                entrypoint="https://wire.example/feed",
                strategy="rss",
                priority=9,
            )
        ],
        newsletter=NewsletterSettings(),
        runtime=RuntimeSettings(
            db_path=tmp_path / "newsletter.sqlite", output_dir=tmp_path / "output"
        ),
        submissions=SubmissionSettings(enabled=enabled),
    )


def test_serve_starts_even_when_intake_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeServer()
    monkeypatch.setattr(wsgiref.simple_server, "make_server", lambda *a, **k: server)

    code = cli.cmd_serve(
        make_config(tmp_path, enabled=False),
        argparse.Namespace(host="127.0.0.1", port=8765),
    )

    assert code == cli.EXIT_OK
    assert server.served, "the newspaper must still be served"


def test_the_banner_stops_advertising_a_form_nobody_can_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wsgiref.simple_server, "make_server", lambda *a, **k: FakeServer())

    cli.cmd_serve(
        make_config(tmp_path, enabled=False), argparse.Namespace(host="127.0.0.1", port=8765)
    )
    closed = capsys.readouterr().out
    assert "/submit" not in closed
    assert "closed" in closed

    cli.cmd_serve(
        make_config(tmp_path, enabled=True), argparse.Namespace(host="127.0.0.1", port=8765)
    )
    assert "http://127.0.0.1:8765/submit" in capsys.readouterr().out


def test_submit_still_refuses_when_intake_is_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard that belongs on the intake command, and only there."""
    code = cli.cmd_submit(
        make_config(tmp_path, enabled=False),
        argparse.Namespace(
            url="https://news.example/story", submitted_by=None, note=None, requeue=False
        ),
    )

    assert code == cli.EXIT_ERROR
    assert "disabled" in capsys.readouterr().err
    assert not (tmp_path / "newsletter.sqlite").exists(), "it must refuse before opening anything"
