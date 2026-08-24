"""The newspaper and its submission form, as a WSGI application.

``GET /`` serves the latest edition and ``GET|POST /submit`` takes a proposal, so
the whole loop -- read the newspaper, propose a link, have it considered on the
next run -- happens without a terminal.

**``/`` is served from a name the database chose, never one the request
carried.** It takes no parameter: the issue label comes from
:meth:`~newsletter.persistence.base.Storage.latest_issue_label`, is matched
against :data:`~newsletter.models.ISSUE_LABEL_PATTERN`, and only then joins the
configured output directory with a fixed filename. The joined path is resolved
and checked to be inside that directory, so a symlinked edition folder cannot
reach out of it either.

**``/<issue>/newsletter.html`` is the one route that takes a path from the
request**, because the arrows printed in the masthead have to lead somewhere.
It is the same machinery with one extra step in front: the request may
contribute *only* the label, it goes through the identical pattern, and the
filename is the literal :data:`EDITION_FILENAME` rather than anything the
request said. The output directory also holds ``run_manifest.json`` and
``selected_articles.json`` -- scores, withheld stories, source internals -- so
"only one filename" is structural here, not a filter: no request can name a
file, only a directory to look for that one file in. A label the pattern refuses,
or one no edition was written for, is the same 404 as any other unknown path,
and that 404 echoes no path but the form's.

**Why WSGI and not a framework.** The application is a plain callable, which
``wsgiref.simple_server`` runs locally with no dependency at all and which
``gunicorn newsletter.web.app:application`` runs in production without a single
line changing. A framework would buy routing for four routes and cost a
dependency in a project whose whole argument is that control flow is ordinary
Python.

**Everything the form sends is hostile** (architecture rule 3). The URL goes
through the same :func:`create_submission` gate as the CLI -- scheme, host
blocklist and the SSRF address check -- and the name and description are echoed
back to the sender, so every value that reaches a page is escaped on the way out.
The body is size-capped *before* it is read, because a request that is too big to
be a submission should never be buffered in the first place.

Nothing here authenticates anybody: whoever can reach the page can queue a link.
That is the point, and it is also why the server binds to localhost until someone
deliberately decides otherwise -- more so now that a queued link holds a reserved
slot in the next edition rather than merely a place in the running.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from newsletter.config import AppConfig
from newsletter.ingestion.submissions import SubmissionRejected, create_submission
from newsletter.logging_setup import get_logger
from newsletter.models import ISSUE_LABEL_PATTERN, Submission, SubmissionStatus
from newsletter.persistence.base import Storage
from newsletter.persistence.factory import create_storage

logger = get_logger("web")

StartResponse = Callable[[str, list[tuple[str, str]]], Any]
Environ = dict[str, Any]
Response = tuple[str, list[tuple[str, str]], bytes]

#: A submission is a name, a URL and at most 500 characters. Anything larger is
#: not a submission, and is refused before a byte of it is read.
MAX_BODY_BYTES = 8 * 1024

FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"
FORM_PATH = "/submit"
EDITION_PATH = "/"

#: The one file either edition route may open, inside the one directory it may
#: look in. It is a constant, never interpolated from a request, which is what
#: keeps ``run_manifest.json`` and ``selected_articles.json`` unreachable.
EDITION_FILENAME = "newsletter.html"


def issue_label_in(path: str) -> str | None:
    """The issue label in ``/<label>/newsletter.html``, or None if that is not it.

    Written as a split rather than a regex over the whole path so that the two
    halves of the rule are visible separately: the last segment must *equal*
    :data:`EDITION_FILENAME`, and what precedes it must be a single label that
    :data:`~newsletter.models.ISSUE_LABEL_PATTERN` accepts. The pattern contains
    no separator, so ``/a/b/newsletter.html``, ``/../newsletter.html`` and
    ``/output/2026-W34/newsletter.html`` all fail it, and no other filename can
    reach the second half of the check at all.
    """
    head, slash, filename = path.rpartition("/")
    if not slash or filename != EDITION_FILENAME or not head.startswith("/"):
        return None
    label = head[1:]
    return label if ISSUE_LABEL_PATTERN.fullmatch(label) else None


#: The command that produces the thing ``/`` serves, printed when there is none.
RUN_COMMAND = "python -m newsletter run"

#: The page needs no script and no external asset, so it is allowed neither.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
)

_STATUS_LABELS: dict[SubmissionStatus, str] = {
    SubmissionStatus.PENDING: "en la fila",
    SubmissionStatus.APPROVED: "aprobado",
    SubmissionStatus.REJECTED: "rechazado",
    SubmissionStatus.PUBLISHED: "publicado",
}


class RequestRejected(Exception):
    """The request cannot be processed. Carries the status line to answer with."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# pages -- every interpolated value is escaped here, and only here
# --------------------------------------------------------------------------- #

_STYLE = """
  :root {
    --accent: #1a44d6; --ink: #14161d; --muted: #5b606c;
    --paper: #f3f1ec; --card: #ffffff; --rule: #d5d1c8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 1.25rem 3rem; background: var(--paper); color: var(--ink);
    font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif;
    line-height: 1.5;
  }
  main { max-width: 34rem; margin: 0 auto; background: var(--card);
         border: 1px solid var(--rule); padding: 1.75rem 1.5rem; }
  h1 { margin: 0 0 0.25rem; font-size: 1.5rem; line-height: 1.15; }
  .eyebrow { margin: 0 0 1.25rem; font-size: 0.7rem; letter-spacing: 0.12em;
             text-transform: uppercase; color: var(--muted); }
  label { display: block; margin: 1rem 0 0.25rem; font-size: 0.8rem; font-weight: 700;
          letter-spacing: 0.04em; text-transform: uppercase; }
  .hint { margin: 0.25rem 0 0; font-size: 0.75rem; color: var(--muted); }
  input, textarea {
    width: 100%; padding: 0.6rem 0.7rem; font: inherit; font-size: 0.95rem;
    border: 1px solid var(--rule); background: #fff; color: var(--ink);
  }
  textarea { min-height: 6rem; resize: vertical; }
  button {
    margin-top: 1.5rem; padding: 0.7rem 1.4rem; font: inherit; font-weight: 700;
    letter-spacing: 0.06em; text-transform: uppercase; border: none;
    background: var(--accent); color: #fff; cursor: pointer;
  }
  .outcome { margin: 0 0 1rem; padding: 0.75rem 0.9rem; border-left: 4px solid var(--accent);
             background: #eceaff; font-weight: 700; }
  .outcome.bad { border-color: #a3122b; background: #fbe9ec; }
  dl { margin: 1.25rem 0 0; font-size: 0.9rem; }
  dt { font-size: 0.7rem; letter-spacing: 0.1em; text-transform: uppercase;
       color: var(--muted); margin-top: 0.75rem; }
  dd { margin: 0.15rem 0 0; word-break: break-word; }
  .foot { margin: 1.75rem 0 0; padding-top: 1rem; border-top: 1px solid var(--rule);
          font-size: 0.75rem; color: var(--muted); }
  a { color: var(--accent); }
"""


def escape(value: str) -> str:
    """Escape a value for HTML, quotes included. The only way text reaches a page."""
    return html.escape(value, quote=True)


def page(title: str, body: str) -> bytes:
    """Wrap already-escaped body markup in the shared shell."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n"
        "</main>\n</body>\n</html>\n"
    ).encode()


def form_page(action: str, *, masthead: str) -> bytes:
    return page(
        "Proponer un enlace",
        f"<h1>Proponer un enlace</h1>\n"
        f'<p class="eyebrow">{escape(masthead)}</p>\n'
        f'<form method="post" action="{escape(action)}">\n'
        '  <label for="name">Nombre completo</label>\n'
        '  <input id="name" name="name" type="text" maxlength="80" required autocomplete="name">\n'
        '  <label for="url">Enlace</label>\n'
        '  <input id="url" name="url" type="url" inputmode="url" required\n'
        '         placeholder="https://...">\n'
        '  <p class="hint">Solo enlaces https a p&aacute;ginas p&uacute;blicas.</p>\n'
        '  <label for="note">Descripci&oacute;n (opcional)</label>\n'
        '  <textarea id="note" name="note" maxlength="500"></textarea>\n'
        '  <p class="hint">Para qui&eacute;n edita la newsletter; el modelo nunca la lee.</p>\n'
        '  <button type="submit">Enviar</button>\n'
        "</form>\n"
        '<p class="foot">Un enlace propuesto se busca, se analiza y se puntúa igual que\n'
        "cualquier otra nota en la siguiente edición. Proponerlo da consideración, no\n"
        "publicación.</p>",
    )


def no_edition_page(form_path: str, *, masthead: str) -> bytes:
    """What ``/`` answers before a first edition exists.

    Not an error page: nothing failed, the newspaper has simply not been printed
    yet, so it says which command prints it and leaves the form reachable.
    """
    return page(
        "Todavía no hay una edición",
        f"<h1>Todav&iacute;a no hay una edici&oacute;n</h1>\n"
        f'<p class="eyebrow">{escape(masthead)}</p>\n'
        "<p>Ninguna edici&oacute;n fue generada hasta ahora. Para publicar la primera, "
        f"ejecuta:</p>\n<p><code>{escape(RUN_COMMAND)}</code></p>\n"
        f'<p class="foot"><a href="{escape(form_path)}">Proponer un enlace</a> para la '
        "pr&oacute;xima edici&oacute;n.</p>",
    )


def outcome_page(
    headline: str,
    detail: str,
    *,
    submission: Submission | None,
    bad: bool,
    form_path: str = FORM_PATH,
) -> bytes:
    rows = ""
    if submission is not None:
        rows = (
            "<dl>\n"
            f"<dt>Enlace</dt><dd>{escape(submission.url)}</dd>\n"
            f"<dt>Nombre</dt><dd>{escape(submission.submitted_by or '-')}</dd>\n"
            f"<dt>Descripci&oacute;n</dt><dd>{escape(submission.note or '-')}</dd>\n"
            "</dl>\n"
        )
    return page(
        headline,
        f'<p class="outcome{" bad" if bad else ""}">{escape(headline)}</p>\n'
        f"<p>{escape(detail)}</p>\n"
        f"{rows}"
        f'<p class="foot"><a href="{escape(form_path)}">Proponer otro enlace</a></p>',
    )


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #


def respond(status: str, body: bytes, *, extra: Iterable[tuple[str, str]] = ()) -> Response:
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
        *extra,
    ]
    return status, headers, body


# --------------------------------------------------------------------------- #
# request parsing
# --------------------------------------------------------------------------- #


def read_form(environ: Environ) -> dict[str, str]:
    """Parse a form body, refusing anything that is not one.

    The length is checked against :data:`MAX_BODY_BYTES` before the body is read,
    so an oversized request costs a header parse rather than 8 MB of memory.
    """
    media_type = (environ.get("CONTENT_TYPE") or "").split(";", 1)[0].strip().lower()
    if media_type != FORM_MEDIA_TYPE:
        raise RequestRejected(
            "415 Unsupported Media Type",
            "El formulario debe enviarse como application/x-www-form-urlencoded.",
        )

    raw_length = (environ.get("CONTENT_LENGTH") or "").strip()
    try:
        length = int(raw_length)
    except ValueError:
        raise RequestRejected("400 Bad Request", "Falta el tamaño del contenido.") from None
    if length < 0:
        raise RequestRejected("400 Bad Request", "El tamaño del contenido no es válido.")
    if length > MAX_BODY_BYTES:
        raise RequestRejected(
            "413 Payload Too Large",
            f"El envío supera el máximo de {MAX_BODY_BYTES} bytes.",
        )

    body = environ["wsgi.input"].read(length)
    if len(body) > MAX_BODY_BYTES:  # a header that lied about the size
        raise RequestRejected(
            "413 Payload Too Large",
            f"El envío supera el máximo de {MAX_BODY_BYTES} bytes.",
        )

    return dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))


# --------------------------------------------------------------------------- #
# the application
# --------------------------------------------------------------------------- #


class SubmissionApp:
    """A WSGI callable serving ``GET /``, ``GET /submit`` and ``POST /submit``.

    ``storage_factory`` exists so a test can hand in an in-memory database; in
    production it resolves the configured DSN through
    :func:`~newsletter.persistence.factory.create_storage`, which is what keeps
    this deployable against the same server the pipeline writes to.

    ``check_address`` mirrors :func:`create_submission`: tests turn the DNS
    lookup off, a running server never does.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        storage_factory: Callable[[], Storage] | None = None,
        check_address: bool = True,
    ) -> None:
        self.config = config
        self.check_address = check_address
        self._storage_factory = storage_factory or (
            lambda: create_storage(config.runtime.database_url)
        )

    # -- WSGI entry point --------------------------------------------------- #

    def __call__(self, environ: Environ, start_response: StartResponse) -> list[bytes]:
        try:
            status, headers, body = self.dispatch(environ)
        except RequestRejected as exc:
            status, headers, body = respond(
                exc.status,
                outcome_page(
                    "No se pudo enviar",
                    exc.message,
                    submission=None,
                    bad=True,
                    form_path=self.url_for(environ, FORM_PATH),
                ),
            )
        except Exception:
            # The client learns that it failed and nothing else: a traceback can
            # carry a path, a DSN or a query. The operator gets the whole thing.
            logger.exception("unhandled error serving %s", environ.get("PATH_INFO"))
            status, headers, body = respond(
                "500 Internal Server Error",
                outcome_page(
                    "Algo falló de nuestro lado",
                    "El envío no se guardó. Vuelve a intentarlo en un momento.",
                    submission=None,
                    bad=True,
                    form_path=self.url_for(environ, FORM_PATH),
                ),
            )
        start_response(status, headers)
        return [body]

    def dispatch(self, environ: Environ) -> Response:
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = (environ.get("PATH_INFO") or "/").rstrip("/") or "/"
        form_path = self.url_for(environ, FORM_PATH)

        if path == EDITION_PATH:
            if method != "GET":
                return self.method_not_allowed(("GET",), form_path)
            return self.get_edition(form_path)

        if path == FORM_PATH:
            if method == "GET":
                return self.get_form(form_path)
            if method == "POST":
                return self.post_form(environ, form_path)
            return self.method_not_allowed(("GET", "POST"), form_path)

        label = issue_label_in(path)
        if label is not None:
            if method != "GET":
                return self.method_not_allowed(("GET",), form_path)
            edition = self.read_edition(label)
            if edition is not None:
                return respond("200 OK", edition)
            # Falls through to the same 404 an unknown path gets: a reader who
            # guesses a week that was never printed learns nothing a reader who
            # guesses a nonsense path does not.

        return respond(
            "404 Not Found",
            outcome_page(
                "Esa página no existe",
                f"El formulario está en {form_path}.",
                submission=None,
                bad=True,
                form_path=form_path,
            ),
        )

    # -- routes ------------------------------------------------------------- #

    def get_edition(self, form_path: str) -> Response:
        """The latest edition, or the page that says there is not one yet."""
        edition = self.read_latest_edition()
        if edition is None:
            return respond(
                "200 OK",
                no_edition_page(form_path, masthead=self.config.newsletter.masthead),
            )
        return respond("200 OK", edition)

    def get_form(self, form_path: str) -> Response:
        if not self.config.submissions.enabled:
            return self.closed(form_path)
        return respond("200 OK", form_page(form_path, masthead=self.config.newsletter.masthead))

    def post_form(self, environ: Environ, form_path: str) -> Response:
        if not self.config.submissions.enabled:
            return self.closed(form_path)

        fields = read_form(environ)
        name = fields.get("name", "").strip()
        url = fields.get("url", "").strip()
        note = fields.get("note", "").strip()

        if not name:
            raise RequestRejected("400 Bad Request", "Falta tu nombre completo.")
        if not url:
            raise RequestRejected("400 Bad Request", "Falta el enlace.")

        try:
            submission = create_submission(
                url,
                submitted_by=name,
                note=note or None,
                require_https=self.config.submissions.require_https,
                blocked_hosts=self.config.submissions.blocked_hosts,
                check_address=self.check_address,
            )
        except SubmissionRejected as exc:
            logger.warning("web submission rejected: %s", exc)
            raise RequestRejected("400 Bad Request", f"No se aceptó el enlace: {exc}") from exc

        return self.store(submission, form_path)

    # -- persistence -------------------------------------------------------- #

    def store(self, submission: Submission, form_path: str) -> Response:
        """Persist through the storage protocol, one connection per request.

        Per request rather than one held open for the life of the process:
        SQLite connections belong to the thread that made them and any real WSGI
        server is threaded, so a shared connection would be a latent
        cross-thread bug. A form nobody submits twice a minute cannot notice the
        cost, and a dropped server connection expires with its request instead of
        wedging the process.
        """
        storage = self._storage_factory().connect()
        try:
            existing = storage.get_submission(submission.submission_id)
            if existing is not None and existing.status is not SubmissionStatus.PENDING:
                label = _STATUS_LABELS[existing.status]
                detail = existing.reason or "Ya fue considerado en una edición anterior."
                return respond(
                    "200 OK",
                    outcome_page(
                        f"Ese enlace ya fue {label}",
                        detail,
                        submission=existing,
                        bad=False,
                        form_path=form_path,
                    ),
                )
            is_new = storage.save_submission(submission)
        finally:
            storage.close()

        headline = "Recibimos tu enlace" if is_new else "Ese enlace ya estaba en la fila"
        return respond(
            "200 OK",
            outcome_page(
                headline,
                "Se va a buscar, analizar y puntuar junto con todo lo demás en la próxima "
                "edición. Proponerlo da consideración, no publicación.",
                submission=submission,
                bad=False,
                form_path=form_path,
            ),
        )

    # -- the published edition ---------------------------------------------- #

    def latest_issue_label(self) -> str | None:
        """The issue the database says was generated last, through the protocol."""
        storage = self._storage_factory().connect()
        try:
            return storage.latest_issue_label()
        finally:
            storage.close()

    def read_latest_edition(self) -> bytes | None:
        """The bytes of the latest edition's HTML, or None when there is none."""
        label = self.latest_issue_label()
        return None if label is None else self.read_edition(label)

    def read_edition(self, label: str) -> bytes | None:
        """The bytes of one issue's HTML, or None when it cannot be served.

        The single place either route opens a file, so both are defended
        identically. Three things have to hold before a byte is read: the label
        is one :data:`~newsletter.models.ISSUE_LABEL_PATTERN` accepts, the
        filename is the constant :data:`EDITION_FILENAME` rather than anything a
        caller chose, and the resolved path is still inside the configured output
        directory -- which a pattern check alone cannot prove, because the
        edition directory may be a symlink. A label that fails any of them reads
        nothing: the reader is never told what the server looked for, and the
        operator gets the reason in the log.
        """
        if not ISSUE_LABEL_PATTERN.fullmatch(label):
            logger.warning("issue label is not servable: %r", label)
            return None

        root = Path(self.config.runtime.output_dir).resolve()
        path = (root / label / EDITION_FILENAME).resolve()
        if not path.is_relative_to(root):  # a symlinked edition directory
            logger.warning("edition for %s resolves outside %s", label, root)
            return None
        try:
            return path.read_bytes()
        except OSError as exc:
            # The artifacts were deleted, the output directory moved since the
            # run, or nobody ever printed that week. That is a missing
            # newspaper, not a server failure.
            logger.warning("cannot read the edition for %s: %s", label, exc)
            return None

    # -- helpers ------------------------------------------------------------ #

    def closed(self, form_path: str) -> Response:
        return respond(
            "403 Forbidden",
            outcome_page(
                "Las propuestas están cerradas",
                "La recepción de enlaces está desactivada en la configuración.",
                submission=None,
                bad=True,
                form_path=form_path,
            ),
        )

    def method_not_allowed(self, allowed: tuple[str, ...], form_path: str) -> Response:
        return respond(
            "405 Method Not Allowed",
            outcome_page(
                "Método no permitido",
                f"Esta dirección responde a {', '.join(allowed)}.",
                submission=None,
                bad=True,
                form_path=form_path,
            ),
            extra=[("Allow", ", ".join(allowed))],
        )

    @staticmethod
    def url_for(environ: Environ, path: str) -> str:
        """``path`` under the prefix the server mounted the app at, if any."""
        return (environ.get("SCRIPT_NAME") or "").rstrip("/") + path


def create_app(
    config: AppConfig,
    *,
    storage_factory: Callable[[], Storage] | None = None,
    check_address: bool = True,
) -> SubmissionApp:
    return SubmissionApp(config, storage_factory=storage_factory, check_address=check_address)


#: Built on the first request served by :func:`application`, then reused.
_APPLICATION: SubmissionApp | None = None


def application(environ: Environ, start_response: StartResponse) -> list[bytes]:
    """Module-level entry point for a real WSGI server.

    ``gunicorn newsletter.web.app:application`` works with no glue code. The
    configuration is loaded once, on the first request, from ``NEWSLETTER_CONFIG_DIR``
    (default ``config``) -- late enough that importing the module never touches
    the filesystem.
    """
    global _APPLICATION
    if _APPLICATION is None:
        import os

        from newsletter.config import load_config

        _APPLICATION = SubmissionApp(load_config(os.environ.get("NEWSLETTER_CONFIG_DIR", "config")))
    return _APPLICATION(environ, start_response)
