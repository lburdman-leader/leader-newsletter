"""Command line interface.

    python -m newsletter run [--from YYYY-MM-DD --to YYYY-MM-DD] [--dry-run]
    python -m newsletter validate
    python -m newsletter sources
    python -m newsletter submit <url> [--by NAME] [--note TEXT]
    python -m newsletter submissions [--status pending|approved|rejected|published]
    python -m newsletter serve [--host ADDR] [--port N]

Exit codes are stable, because CI and the weekly workflow depend on them:

===  =========================================================
  0  success
  1  configuration or runtime error
  2  usage error (argparse)
  4  nothing to publish -- a quiet week, not a failure
===  =========================================================
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from newsletter.config import AppConfig, ConfigError, load_config
from newsletter.context import RunContext
from newsletter.ingestion.submissions import SubmissionRejected, create_submission
from newsletter.logging_setup import configure_logging, get_logger, report, report_plain
from newsletter.models import DateWindow, SubmissionStatus
from newsletter.persistence.base import Storage
from newsletter.persistence.dsn import redact_dsn
from newsletter.pipeline import (
    NothingToPublish,
    PipelineError,
    open_database,
    run_pipeline,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
#: A run that worked but found nothing worth publishing. A scheduled job must be
#: able to tell that apart from a breakage.
EXIT_NOTHING_TO_PUBLISH = 4

#: Loopback by default: the submission form has no authentication, so reaching a
#: network is something an operator chooses, not something a default does.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

logger = get_logger("cli")


def _load_dotenv() -> None:
    """Load ``.env`` into the environment when python-dotenv is available.

    The file is read by the *application*, never printed. Existing environment
    variables always win, so CI secrets are not shadowed by a stale local file.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional convenience only
        return
    load_dotenv(override=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="newsletter",
        description="Generate a weekly enterprise intelligence newspaper.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config"),
        metavar="DIR",
        help="configuration directory (default: config)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="override LOG_LEVEL for this invocation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="generate an edition")
    run.add_argument(
        "--from", dest="date_from", metavar="YYYY-MM-DD", help="window start (inclusive)"
    )
    run.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="window end (inclusive)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve configuration and the date window without fetching or calling OpenAI",
    )

    subparsers.add_parser("validate", help="validate configuration and exit")
    subparsers.add_parser("sources", help="list configured sources")

    submit = subparsers.add_parser("submit", help="propose a link for a future edition")
    submit.add_argument("url", help="the article URL")
    submit.add_argument("--by", dest="submitted_by", metavar="NAME", help="who is submitting")
    submit.add_argument("--note", metavar="TEXT", help="why it matters (for humans only)")
    submit.add_argument(
        "--requeue",
        action="store_true",
        help="reconsider a link that was already decided, e.g. after a policy change",
    )

    serve = subparsers.add_parser(
        "serve", help="serve the latest edition and the submission form (localhost by default)"
    )
    serve.add_argument(
        "--host",
        default=DEFAULT_HOST,
        metavar="ADDR",
        help=(
            f"interface to bind (default: {DEFAULT_HOST}). The default reaches only this "
            "machine on purpose: the form is unauthenticated, so exposing it to a network "
            "is a deployment decision, taken with a real server in front of it."
        ),
    )
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port to bind (default: {DEFAULT_PORT})"
    )

    listing = subparsers.add_parser(
        "submissions", help="list reader submissions and what became of them"
    )
    listing.add_argument(
        "--status",
        choices=[status.value for status in SubmissionStatus],
        help="show only submissions in this state",
    )
    listing.add_argument("--limit", type=int, default=50, help="maximum rows (default: 50)")
    return parser


def resolve_window(config: AppConfig, date_from: str | None, date_to: str | None) -> DateWindow:
    """Deterministic window resolution. The model is never consulted."""
    if bool(date_from) != bool(date_to):
        raise ConfigError("--from and --to must be used together")
    if date_from and date_to:
        return DateWindow.from_dates(date_from, date_to, tz_name=config.newsletter.timezone)
    return DateWindow.last_days(
        config.newsletter.window_days,
        now=datetime.now(UTC),
        tz_name=config.newsletter.timezone,
        mode=config.newsletter.window_mode,
    )


def _describe_window(window: DateWindow) -> str:
    return (
        f"{window.start:%Y-%m-%d %H:%M %Z} -> {window.end:%Y-%m-%d %H:%M %Z} "
        f"({window.days:.0f}d, issue {window.issue_label()})"
    )


def cmd_validate(config: AppConfig) -> int:
    enabled = config.enabled_sources
    report(f"Configuration valid: {len(config.sources)} sources ({len(enabled)} enabled)")
    report(f"Masthead: {config.newsletter.masthead}")
    report(
        f"Window: {config.newsletter.window_days} days "
        f"({config.newsletter.window_mode.value}, {config.newsletter.timezone})"
    )
    report(
        f"Selection: min score {config.newsletter.min_score}, max {config.newsletter.max_items} stories"
    )
    limits = ", ".join(
        f"{category.value}={config.newsletter.limit_for(category)}"
        for category in config.newsletter.ordered_categories()
    )
    report(f"Section limits: {limits}")
    # How much of the edition is promised away before anything is earned: the one
    # number that decides whether "10 stories" means ten the rubric chose.
    reserved = config.submissions.reserved_slots
    if not config.submissions.enabled or reserved == 0:
        report("Reserved slots: none, submissions compete on score")
    elif reserved is None:
        report(f"Reserved slots: every submission, up to {config.newsletter.max_items}")
    else:
        report(f"Reserved slots: up to {reserved} for reader submissions")
    # The DSN is printed redacted: a connection string carries a password, and
    # nothing that reaches a terminal or a log may carry it.
    report(
        f"Output: {config.runtime.output_dir} | database: {redact_dsn(config.runtime.database_url)}"
    )
    report(
        "OpenAI key: present"
        if config.runtime.has_openai_key
        else "OpenAI key: absent (use --dry-run or fixtures)"
    )
    return EXIT_OK


def cmd_sources(config: AppConfig) -> int:
    report_plain(f"{'ID':<26} {'PRI':>3}  {'STRATEGY':<18} {'CATEGORY':<22} {'ON':<3} ENTRYPOINT")
    report_plain("-" * 118)
    for source in sorted(config.sources, key=lambda s: (-s.priority, s.id)):
        report_plain(
            f"{source.id:<26} {source.priority:>3}  {source.strategy.value:<18} "
            f"{source.category_hint.value:<22} {'yes' if source.enabled else 'no':<3} "
            f"{source.entrypoint}"
        )
    report_plain()
    report(f"{len(config.sources)} sources, {len(config.enabled_sources)} enabled")
    return EXIT_OK


def cmd_submit(config: AppConfig, args: argparse.Namespace) -> int:
    """Accept a link from anyone. It takes a reserved slot in the next edition."""
    if not config.submissions.enabled:
        print("Submissions are disabled in config/newsletter.yaml.", file=sys.stderr)
        return EXIT_ERROR

    try:
        submission = create_submission(
            args.url,
            submitted_by=args.submitted_by,
            note=args.note,
            require_https=config.submissions.require_https,
            blocked_hosts=config.submissions.blocked_hosts,
        )
    except SubmissionRejected as exc:
        logger.warning("submission rejected: %s", exc)
        print(f"Not accepted: {exc}", file=sys.stderr)
        return EXIT_ERROR

    database: Storage = open_database(config)
    try:
        existing = database.get_submission(submission.submission_id)
        decided = existing is not None and existing.status is not SubmissionStatus.PENDING
        if decided and not args.requeue:
            report(f"Already {existing.status.value}: {existing.reason or existing.url}")
            report_plain("    Use --requeue to have it reconsidered on the next run.")
            return EXIT_OK
        if decided:
            # Keep who submitted it and why; only the verdict is cleared.
            submission = submission.model_copy(
                update={
                    "submitted_by": submission.submitted_by or existing.submitted_by,
                    "note": submission.note or existing.note,
                    "submitted_at": existing.submitted_at,
                }
            )
            report(f"Requeued: was {existing.status.value} ({existing.reason})")
        is_new = database.save_submission(submission)
    finally:
        database.close()

    report("Submission accepted" if is_new else "Already queued")
    report_plain(f"    id       {submission.submission_id}")
    report_plain(f"    url      {submission.url}")
    if submission.submitted_by:
        report_plain(f"    by       {submission.submitted_by}")
    report_plain("")
    report_plain(
        "It will be fetched, assessed and scored with everything else on the next run.\n"
        "Being submitted does not guarantee publication."
    )
    return EXIT_OK


def cmd_serve(config: AppConfig, args: argparse.Namespace) -> int:
    """Serve the latest edition at ``/`` and the submission form it links to.

    ``wsgiref`` runs it here so a local run needs no dependency; the application
    is a plain WSGI callable, so a deployment runs the same object under gunicorn
    or uvicorn without changing a line.
    """
    from wsgiref.simple_server import make_server

    from newsletter.web.app import EDITION_PATH, FORM_PATH, SubmissionApp

    if not config.submissions.enabled:
        print("Submissions are disabled in config/newsletter.yaml.", file=sys.stderr)
        return EXIT_ERROR

    app = SubmissionApp(config)
    try:
        server = make_server(args.host, args.port, app)
    except OSError as exc:
        logger.error("could not bind %s:%s: %s", args.host, args.port, exc)
        print(f"Error: could not bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    report(f"Latest edition on http://{args.host}:{args.port}{EDITION_PATH}")
    report_plain(f"    form     http://{args.host}:{args.port}{FORM_PATH}")
    report_plain(f"    editions {config.runtime.output_dir}")
    report_plain(f"    database {redact_dsn(config.runtime.database_url)}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        report_plain(
            "    warning  this binding is reachable from the network and the form has no "
            "authentication"
        )
    report_plain("    Stop with Ctrl-C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        report_plain("")
        report("Stopped")
    finally:
        server.server_close()
    return EXIT_OK


def cmd_submissions(config: AppConfig, args: argparse.Namespace) -> int:
    database: Storage = open_database(config)
    try:
        status = SubmissionStatus(args.status) if args.status else None
        rows = database.list_submissions(status=status, limit=args.limit)
    finally:
        database.close()

    if not rows:
        report("No submissions yet. Add one with: python -m newsletter submit <url>")
        return EXIT_OK

    report_plain(f"{'ID':<18} {'STATUS':<10} {'SUBMITTED':<12} {'BY':<16} URL")
    report_plain("-" * 110)
    for row in rows:
        report_plain(
            f"{row.submission_id:<18} {row.status.value:<10} "
            f"{row.submitted_at:%Y-%m-%d}   {(row.submitted_by or '-'):<16} {row.url}"
        )
        if row.reason:
            report_plain(f"{'':<18} -> {row.reason}")
    report_plain("")
    report(f"{len(rows)} submissions")
    return EXIT_OK


def cmd_run(config: AppConfig, args: argparse.Namespace) -> int:
    window = resolve_window(config, args.date_from, args.date_to)
    context = RunContext.create(config, window, dry_run=args.dry_run)

    report(f"Run {context.run_id}")
    report(f"Window {_describe_window(window)}")
    if not args.dry_run:
        report(f"Edition directory: {context.edition_dir}")

    database: Storage | None = None if args.dry_run else open_database(config)
    try:
        result = run_pipeline(context, database=database)
    finally:
        if database is not None:
            database.close()

    if args.dry_run:
        report_plain("")
        report_plain("Dry run complete: sources were fetched and the deterministic stages ran.")
        report_plain("No OpenAI call was made and no edition was written.")
        return EXIT_OK

    report_plain("")
    for name, path in result.outputs.items():
        report_plain(f"    {name:18} {path}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    _load_dotenv()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        configure_logging(args.log_level or "INFO")
        logger.error("configuration error: %s", exc)
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    configure_logging(args.log_level or config.runtime.log_level)

    if args.command == "validate":
        return cmd_validate(config)
    if args.command == "sources":
        return cmd_sources(config)
    if args.command == "submit":
        return cmd_submit(config, args)
    if args.command == "submissions":
        return cmd_submissions(config, args)
    if args.command == "serve":
        return cmd_serve(config, args)
    if args.command == "run":
        try:
            return cmd_run(config, args)
        except NothingToPublish as exc:
            logger.info("nothing to publish: %s", exc)
            print(f"Nothing to publish: {exc}", file=sys.stderr)
            return EXIT_NOTHING_TO_PUBLISH
        except (PipelineError, ConfigError, ValueError) as exc:
            logger.error("run failed: %s", exc)
            print(f"Error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover - argparse guards this
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
