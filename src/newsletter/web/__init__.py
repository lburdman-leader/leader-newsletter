"""The reader-facing intake: one form, three fields, no framework."""

from newsletter.web.app import SubmissionApp, create_app

__all__ = ["SubmissionApp", "create_app"]
