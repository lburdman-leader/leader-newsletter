"""The reader-facing server: the edition at ``/``, the form, and hostile input.

The application is a WSGI callable, so it is tested as one -- an ``environ``
dictionary in, a status line and bytes out. No socket is opened; the autouse
``no_network`` guard in ``tests/conftest.py`` would fail the run if one were.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest

from newsletter.config import AppConfig, NewsletterSettings, RuntimeSettings, SubmissionSettings
from newsletter.ingestion.submissions import create_submission, submission_id_for
from newsletter.models import SourceConfig, SubmissionStatus
from newsletter.persistence.base import PersistenceError
from newsletter.persistence.sqlite import Database
from newsletter.web.app import (
    EDITION_FILENAME,
    FORM_MEDIA_TYPE,
    MAX_BODY_BYTES,
    RUN_COMMAND,
    SubmissionApp,
)
from tests.unit.test_persistence import make_edition

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
LINK = "https://news.example/2026/08/story"
ISSUE = "2026-W34"
EDITION_HTML = "<!DOCTYPE html>\n<html lang=es><body><h1>La edición</h1></body></html>\n"
#: Planted outside the output directory: if a label ever escapes, this shows up.
SECRET = "TOP SECRET"


@dataclass
class Reply:
    status: str
    headers: dict[str, str]
    body: str

    @property
    def code(self) -> int:
        return int(self.status.split(" ", 1)[0])


class UnreadableInput:
    """A body the test forbids reading, so an early refusal can be proven."""

    def read(self, size: int = -1) -> bytes:
        raise AssertionError("an oversized body must be refused before it is read")


def make_config(tmp_path: Path, **submission_overrides: object) -> AppConfig:
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
        newsletter=NewsletterSettings(masthead="Leader Intelligence Semanal"),
        runtime=RuntimeSettings(
            db_path=tmp_path / "newsletter.sqlite", output_dir=tmp_path / "output"
        ),
        submissions=SubmissionSettings(**submission_overrides),  # type: ignore[arg-type]
    )


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path / "newsletter.sqlite"


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "output"


def record_edition(storage_path: Path, label: str) -> None:
    """Tell the database an edition exists under ``label`` -- disk untouched."""
    edition = make_edition().model_copy(update={"edition_id": label, "issue_label": label})
    with Database(storage_path) as database:
        database.save_edition(edition)


def publish_edition(
    storage_path: Path, output_dir: Path, label: str = ISSUE, body: str | None = None
) -> Path:
    """Record an edition *and* write the artifact the run would have written."""
    record_edition(storage_path, label)
    path = output_dir / label / EDITION_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or EDITION_HTML, encoding="utf-8", newline="\n")
    return path


def plant_run_internals(output_dir: Path, label: str = ISSUE) -> None:
    """The two files a run writes beside the edition. Neither is publishable."""
    for filename in ("run_manifest.json", "selected_articles.json"):
        (output_dir / label / filename).write_text(SECRET, encoding="utf-8")


@pytest.fixture
def app(tmp_path: Path, storage_path: Path) -> SubmissionApp:
    # A file database, opened and closed per request exactly as in production;
    # the DNS-resolving half of the URL gate is off, as everywhere else offline.
    return SubmissionApp(
        make_config(tmp_path),
        storage_factory=lambda: Database(storage_path),
        check_address=False,
    )


def stored(storage_path: Path) -> list:
    with Database(storage_path) as database:
        return database.list_submissions()


def call(
    app: SubmissionApp,
    *,
    method: str = "GET",
    path: str = "/submit",
    fields: dict[str, str] | None = None,
    body: bytes | None = None,
    content_type: str = FORM_MEDIA_TYPE,
    content_length: str | None = None,
    wsgi_input: object | None = None,
    script_name: str = "",
) -> Reply:
    if fields is not None and body is None:
        body = urlencode(fields).encode("utf-8")
    payload = body or b""
    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SCRIPT_NAME": script_name,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": content_length if content_length is not None else str(len(payload)),
        "wsgi.input": wsgi_input if wsgi_input is not None else io.BytesIO(payload),
    }
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    chunks = app(environ, start_response)  # type: ignore[arg-type]
    return Reply(
        status=str(captured["status"]),
        headers=dict(captured["headers"]),  # type: ignore[call-overload]
        body=b"".join(chunks).decode("utf-8"),
    )


# --------------------------------------------------------------------------- #
# the edition at /
# --------------------------------------------------------------------------- #


def test_the_root_serves_the_latest_edition(
    app: SubmissionApp, storage_path: Path, output_dir: Path
) -> None:
    publish_edition(storage_path, output_dir)

    reply = call(app, path="/")

    assert reply.code == 200
    assert reply.body == EDITION_HTML
    assert reply.headers["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("never generated", id="no edition at all"),
        pytest.param("artifact deleted", id="the database names one the disk lost"),
    ],
)
def test_without_a_readable_edition_the_root_says_how_to_print_one(
    app: SubmissionApp, storage_path: Path, output_dir: Path, state: str
) -> None:
    """A newspaper nobody printed yet is an empty page, never a 500."""
    if state == "artifact deleted":
        publish_edition(storage_path, output_dir).unlink()

    reply = call(app, path="/")

    assert reply.code == 200
    assert RUN_COMMAND in reply.body


@pytest.mark.parametrize(
    "label",
    [
        pytest.param("../secret", id="climbs out of the output directory"),
        pytest.param("..", id="the parent itself"),
        pytest.param("2026-W34/../..", id="climbs from a real issue"),
        pytest.param("/etc/passwd", id="an absolute path"),
        pytest.param(".hidden", id="a dotfile"),
        pytest.param("W" * 200, id="absurdly long"),
    ],
)
def test_a_stored_label_that_is_not_a_plain_issue_name_reads_nothing(
    app: SubmissionApp, storage_path: Path, output_dir: Path, tmp_path: Path, label: str
) -> None:
    """The label names one directory under the output directory, or it names nothing."""
    planted = tmp_path / "secret" / EDITION_FILENAME
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(SECRET, encoding="utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    record_edition(storage_path, label)

    reply = call(app, path="/")

    assert reply.code == 200
    assert SECRET not in reply.body
    assert RUN_COMMAND in reply.body


@pytest.mark.parametrize(
    "path",
    [
        "/../",
        "/../../etc/passwd",
        "/%2e%2e/",
        f"/{EDITION_FILENAME}",
        f"/output/{ISSUE}/{EDITION_FILENAME}",
    ],
)
def test_a_request_for_a_path_that_is_no_route_gets_a_404(
    app: SubmissionApp, storage_path: Path, output_dir: Path, path: str
) -> None:
    """Two paths serve an edition: ``/`` and one issue directory named in full."""
    publish_edition(storage_path, output_dir)

    reply = call(app, path=path)

    assert reply.code == 404
    assert EDITION_HTML not in reply.body
    assert path not in reply.body


# --------------------------------------------------------------------------- #
# the edition at /<issue>/newsletter.html -- where the masthead arrows lead
# --------------------------------------------------------------------------- #


def test_an_older_issue_is_served_by_name_not_as_an_alias_for_the_latest(
    app: SubmissionApp, storage_path: Path, output_dir: Path
) -> None:
    """What ``../2026-W33/newsletter.html`` resolves to once the file is served."""
    older = "<!DOCTYPE html>\n<html lang=es><body><h1>La semana pasada</h1></body></html>\n"
    publish_edition(storage_path, output_dir, "2026-W33", body=older)
    publish_edition(storage_path, output_dir)

    reply = call(app, path=f"/2026-W33/{EDITION_FILENAME}")

    assert reply.code == 200
    assert reply.body == older
    assert reply.headers["Content-Type"] == "text/html; charset=utf-8"
    assert reply.headers["X-Content-Type-Options"] == "nosniff"
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in reply.headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(f"/{ISSUE}/run_manifest.json", id="the run manifest beside the edition"),
        pytest.param(f"/{ISSUE}/selected_articles.json", id="the scored story list"),
        pytest.param(f"/../secret/{EDITION_FILENAME}", id="climbs out of the output directory"),
        pytest.param(f"/{ISSUE}/../{ISSUE}/{EDITION_FILENAME}", id="climbs and comes back"),
        pytest.param(f"/.hidden/{EDITION_FILENAME}", id="a dotfile directory"),
        pytest.param(f"/2026-W99/{EDITION_FILENAME}", id="a week nobody ever printed"),
    ],
)
def test_the_issue_route_reaches_one_filename_in_one_directory_and_nothing_else(
    app: SubmissionApp, storage_path: Path, output_dir: Path, tmp_path: Path, path: str
) -> None:
    """The request may contribute a label. It may never contribute a filename."""
    publish_edition(storage_path, output_dir)
    plant_run_internals(output_dir)
    planted = tmp_path / "secret" / EDITION_FILENAME
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(SECRET, encoding="utf-8")

    reply = call(app, path=path)

    assert reply.code == 404
    assert SECRET not in reply.body
    assert path not in reply.body


def test_the_issue_route_answers_only_get(
    app: SubmissionApp, storage_path: Path, output_dir: Path
) -> None:
    publish_edition(storage_path, output_dir)

    reply = call(app, method="POST", path=f"/{ISSUE}/{EDITION_FILENAME}", fields={})

    assert reply.code == 405
    assert reply.headers["Allow"] == "GET"


# --------------------------------------------------------------------------- #
# the form
# --------------------------------------------------------------------------- #


def test_the_form_offers_exactly_the_three_fields(app: SubmissionApp) -> None:
    reply = call(app)

    assert reply.code == 200
    assert 'name="name"' in reply.body and "required" in reply.body
    assert 'name="url"' in reply.body and 'type="url"' in reply.body
    assert 'name="note"' in reply.body and 'maxlength="500"' in reply.body
    assert reply.body.count("<input") == 2 and reply.body.count("<textarea") == 1


def test_the_form_page_is_self_contained_and_scriptless(app: SubmissionApp) -> None:
    """It is served to strangers, so it asks for nothing and runs nothing."""
    reply = call(app)

    assert "<script" not in reply.body.lower()
    assert "http://" not in reply.body and "https://" not in reply.body.replace(
        'placeholder="https://..."', ""
    )
    assert reply.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert reply.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/", 405),
        ("DELETE", "/submit", 405),
        ("GET", "/wp-login.php", 404),
        ("POST", "/anything", 404),
    ],
)
def test_routing_answers_with_the_right_status(
    app: SubmissionApp, method: str, path: str, expected: int
) -> None:
    reply = call(app, method=method, path=path)

    assert reply.code == expected
    if expected == 405:
        assert "GET" in reply.headers["Allow"]


def test_every_link_it_prints_respects_the_prefix_it_is_mounted_under(app: SubmissionApp) -> None:
    """A real deployment may mount it at /intake; the form must post back to itself."""
    form = call(app, script_name="/intake")
    missing = call(app, path="/nowhere", script_name="/intake")

    assert 'action="/intake/submit"' in form.body
    assert "/intake/submit" in missing.body


# --------------------------------------------------------------------------- #
# accepting a submission
# --------------------------------------------------------------------------- #


def test_a_valid_submission_is_persisted_as_pending(app: SubmissionApp, storage_path: Path) -> None:
    reply = call(
        app,
        method="POST",
        fields={"name": "Ana Pérez", "url": LINK, "note": "why it matters"},
    )

    assert reply.code == 200
    rows = stored(storage_path)
    assert len(rows) == 1
    assert rows[0].url == LINK
    assert rows[0].submitted_by == "Ana Pérez"
    assert rows[0].note == "why it matters"
    assert rows[0].status is SubmissionStatus.PENDING


def test_the_same_link_twice_is_one_submission(app: SubmissionApp, storage_path: Path) -> None:
    """The id is a hash of the canonical URL, so resubmitting is idempotent."""
    first = call(app, method="POST", fields={"name": "Ana", "url": LINK})
    second = call(app, method="POST", fields={"name": "Bo", "url": f"{LINK}?utm_source=x"})

    assert first.code == 200 and second.code == 200
    assert "ya estaba en la fila" in second.body
    assert len(stored(storage_path)) == 1


def test_a_link_already_decided_is_reported_not_requeued(
    app: SubmissionApp, storage_path: Path
) -> None:
    decided = create_submission(LINK, now=NOW, check_address=False).decide(
        SubmissionStatus.REJECTED, "below the threshold", now=NOW
    )
    with Database(storage_path) as database:
        database.save_submission(decided)

    reply = call(app, method="POST", fields={"name": "Ana", "url": LINK})

    assert reply.code == 200
    assert "rechazado" in reply.body
    assert stored(storage_path)[0].status is SubmissionStatus.REJECTED


def test_over_long_fields_are_cut_to_the_model_limits(
    app: SubmissionApp, storage_path: Path
) -> None:
    """The form's maxlength is a courtesy to browsers, never the enforcement."""
    call(app, method="POST", fields={"name": "n" * 500, "url": LINK, "note": "d" * 5_000})

    row = stored(storage_path)[0]
    assert len(row.submitted_by or "") == 80
    assert len(row.note or "") == 500


# --------------------------------------------------------------------------- #
# refusals -- nothing reaches the database
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"name": "", "url": LINK}, id="no name"),
        pytest.param({"name": "Ana", "url": ""}, id="no link"),
        pytest.param({"name": "Ana", "url": "http://news.example/a"}, id="not https"),
        pytest.param({"name": "Ana", "url": "javascript:alert(1)"}, id="not a web link"),
        pytest.param({"name": "Ana", "url": "news.example/a"}, id="no scheme"),
    ],
)
def test_invalid_input_is_refused_and_stores_nothing(
    app: SubmissionApp, storage_path: Path, fields: dict[str, str]
) -> None:
    reply = call(app, method="POST", fields=fields)

    assert reply.code == 400
    assert stored(storage_path) == []


def test_a_blocked_host_is_refused(tmp_path: Path, storage_path: Path) -> None:
    app = SubmissionApp(
        make_config(tmp_path, blocked_hosts=["spam.example"]),
        storage_factory=lambda: Database(storage_path),
        check_address=False,
    )

    reply = call(app, method="POST", fields={"name": "Ana", "url": "https://spam.example/a"})

    assert reply.code == 400
    assert stored(storage_path) == []


def test_hostile_text_is_escaped_on_the_way_out(app: SubmissionApp) -> None:
    """Name and description are attacker-controlled and are echoed back (rule 3)."""
    reply = call(
        app,
        method="POST",
        fields={
            "name": '<script>alert("x")</script>',
            "url": LINK,
            "note": "<img src=x onerror=alert(1)>",
        },
    )

    assert reply.code == 200
    assert "<script>" not in reply.body
    assert "<img" not in reply.body
    assert "&lt;script&gt;" in reply.body
    assert "&lt;img src=x onerror=alert(1)&gt;" in reply.body


def test_an_oversized_body_is_refused_before_it_is_read(app: SubmissionApp) -> None:
    reply = call(
        app,
        method="POST",
        content_length=str(MAX_BODY_BYTES + 1),
        wsgi_input=UnreadableInput(),
    )

    assert reply.code == 413


@pytest.mark.parametrize(
    ("content_type", "content_length", "expected"),
    [
        pytest.param("application/json", "2", 415, id="not a form"),
        pytest.param(FORM_MEDIA_TYPE, "", 400, id="no length"),
        pytest.param(FORM_MEDIA_TYPE, "-1", 400, id="negative length"),
    ],
)
def test_a_body_that_is_not_a_form_submission_is_refused(
    app: SubmissionApp, content_type: str, content_length: str, expected: int
) -> None:
    reply = call(
        app,
        method="POST",
        body=b"{}",
        content_type=content_type,
        content_length=content_length,
    )

    assert reply.code == expected


def test_closing_intake_closes_the_form_and_nothing_else(
    tmp_path: Path, storage_path: Path, output_dir: Path
) -> None:
    """Reading the newspaper and proposing a link are two different things.

    ``submissions.enabled: false`` means "we are not taking links this week". It
    has never meant "stop publishing", and ``/`` has served the edition itself
    since long after that flag was introduced.
    """
    publish_edition(storage_path, output_dir)
    app = SubmissionApp(
        make_config(tmp_path, enabled=False),
        storage_factory=lambda: Database(storage_path),
        check_address=False,
    )

    edition = call(app, path="/")
    assert edition.code == 200
    assert edition.body == EDITION_HTML

    assert call(app).code == 403
    assert call(app, method="POST", fields={"name": "Ana", "url": LINK}).code == 403
    assert stored(storage_path) == []


def test_a_storage_failure_answers_generically_and_leaks_nothing(tmp_path: Path) -> None:
    """A traceback can carry a path or a DSN; the client gets neither."""

    def broken() -> Database:
        raise PersistenceError("could not open postgres://user:secret@db.internal/newsletter")

    app = SubmissionApp(make_config(tmp_path), storage_factory=broken, check_address=False)

    reply = call(app, method="POST", fields={"name": "Ana", "url": LINK})

    assert reply.code == 500
    assert "secret" not in reply.body and "Traceback" not in reply.body


def test_the_submission_id_matches_the_command_line_path(
    app: SubmissionApp, storage_path: Path
) -> None:
    """One link is one submission however it arrived, so the form cannot fork the queue."""
    call(app, method="POST", fields={"name": "Ana", "url": LINK})

    assert stored(storage_path)[0].submission_id == submission_id_for(LINK)
