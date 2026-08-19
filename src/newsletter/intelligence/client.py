"""A deliberately boring wrapper around the OpenAI Responses API.

Everything the model could use to reach beyond "return this structure" is either
switched off or never offered:

* no tools, no function calling, no browsing, no file access;
* ``store=False`` so responses are not retained remotely;
* one fixed schema per call, validated by Pydantic before anything downstream
  sees it;
* an explicit timeout and a bounded retry budget.

Retries are bounded and typed: transient transport problems are retried with
backoff, while refusals and client errors fail immediately, because retrying
them would only burn tokens.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import openai
from pydantic import BaseModel, ValidationError

from newsletter.logging_setup import get_logger

logger = get_logger("intelligence.client")

T = TypeVar("T", bound=BaseModel)

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_OUTPUT_TOKENS = 2000


class ModelError(Exception):
    """Base class for every failure of the semantic layer."""


class ModelRefusal(ModelError):
    """The model declined to answer. Never retried -- it will decline again."""


class ModelTimeout(ModelError):
    """The request exceeded its deadline on every allowed attempt."""


class ModelUnavailable(ModelError):
    """Transport, rate limit or server-side failure that outlived the retry budget."""


class ModelContractError(ModelError):
    """A response that does not satisfy the requested schema."""


#: Retried with backoff: the same request may well succeed shortly.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)

#: Never retried: the outcome is deterministic, so a retry only costs money.
FATAL_ERRORS: tuple[type[Exception], ...] = (
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.BadRequestError,
    openai.NotFoundError,
)


@dataclass(frozen=True)
class StructuredResult:
    """A validated response plus the provenance the manifest needs."""

    parsed: BaseModel
    model: str
    attempts: int


class StructuredClient:
    """Issue one structured request and return a validated Pydantic object."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.client = client
        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.max_output_tokens = max_output_tokens
        self._sleep = sleeper

    def parse(
        self,
        *,
        instructions: str,
        content: str,
        schema: type[T],
    ) -> tuple[T, int]:
        """Return ``(validated_object, attempts_used)``.

        ``instructions`` are application instructions; ``content`` is untrusted
        source material. They are passed in separate fields so the boundary is
        structural, not a formatting convention.
        """
        last_transient: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.responses.parse(
                    model=self.model,
                    instructions=instructions,
                    input=content,
                    text_format=schema,
                    store=False,
                    timeout=self.timeout,
                    max_output_tokens=self.max_output_tokens,
                )
            except FATAL_ERRORS as exc:
                raise ModelUnavailable(f"request rejected: {exc}") from exc
            except openai.LengthFinishReasonError as exc:
                raise ModelContractError(
                    f"response truncated at {self.max_output_tokens} tokens"
                ) from exc
            except openai.ContentFilterFinishReasonError as exc:
                raise ModelRefusal(f"content filtered: {exc}") from exc
            except TRANSIENT_ERRORS as exc:
                last_transient = exc
                if attempt >= self.max_attempts:
                    break
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "transient model failure (attempt %d/%d): %s; retrying in %.1fs",
                    attempt,
                    self.max_attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)
                continue
            except openai.OpenAIError as exc:  # anything the SDK raises that is not mapped
                raise ModelUnavailable(f"unexpected SDK error: {exc}") from exc

            refusal = extract_refusal(response)
            if refusal:
                raise ModelRefusal(refusal)

            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise ModelContractError("response contained no parsed output")
            if not isinstance(parsed, schema):
                # The SDK validates for us; this catches a mocked or future SDK
                # handing back something else, rather than trusting duck typing.
                try:
                    parsed = schema.model_validate(parsed)
                except ValidationError as exc:
                    raise ModelContractError(f"response failed schema validation: {exc}") from exc

            return parsed, attempt

        assert last_transient is not None  # only reachable via the transient break
        if isinstance(last_transient, openai.APITimeoutError):
            raise ModelTimeout(
                f"timed out after {self.max_attempts} attempt(s) at {self.timeout}s each"
            ) from last_transient
        raise ModelUnavailable(
            f"failed after {self.max_attempts} attempt(s): {last_transient}"
        ) from last_transient


def extract_refusal(response: Any) -> str | None:
    """Return the refusal text if the model declined, else None."""
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            refusal = getattr(part, "refusal", None)
            if refusal:
                return str(refusal)
    return None


def build_openai_client(api_key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """Construct the SDK client.

    ``max_retries=0`` hands retry policy to :class:`StructuredClient`, so the
    budget is one visible, testable number rather than two multiplying ones.
    """
    return openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
