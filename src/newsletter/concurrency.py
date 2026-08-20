"""Bounded concurrency for the two network-bound stages.

Article analysis and per-article fetching are independent, I/O-bound and slow, so
they run on a small thread pool instead of one call at a time. Concurrency is an
execution detail and must never reach the artifacts (AC9), which takes three
rules — written once, here, rather than twice at the call sites:

* **Order is restored, never observed.** Work is submitted with its position and
  read back by position, so the caller sees exactly the sequence it passed in.
  Completion order is never used: no ``as_completed``, no set, no dict iteration.
* **Failures come back as values.** A worker exception is captured and returned in
  its own slot, so the caller records it on its own thread, in input order,
  against the run manifest — instead of workers racing to append to it. Nothing
  is swallowed: every slot is either a value or an exception the caller must
  handle.
* **One is one.** ``concurrency=1`` runs the work inline in a plain loop, with no
  pool and no thread at all, reproducing the sequential behaviour it replaced.

Only exception types the caller names in ``capture`` are turned into values;
anything else propagates, so an unexpected bug still fails loudly rather than
being filed as a per-article failure.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")

#: How many article assessments may be in flight at once. A model call takes about
#: six seconds and spends all of it waiting, so a handful of threads turns the
#: longest stage of a run into one of the shortest.
DEFAULT_ANALYSIS_CONCURRENCY = 8

#: How many articles of one source may be fetched at once. Lower than the model's
#: budget, because parallelism inside a source means every one of those requests
#: lands on the same origin: this is roughly the per-host budget a browser allows
#: itself.
DEFAULT_FETCH_CONCURRENCY = 6

# Configuration owns the values actually used -- ``runtime.analysis_concurrency``
# and ``runtime.fetch_concurrency``. These are the defaults those keys start from,
# and they live here, in a module with no heavy imports, so that reading a
# configuration file never drags in the OpenAI SDK or a feed parser.


@dataclass(frozen=True)
class Outcome(Generic[R]):
    """One slot of a concurrent map: what the worker returned, or how it failed."""

    value: R | None = None
    error: Exception | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


def map_ordered(
    worker: Callable[[T], R],
    items: Sequence[T],
    *,
    concurrency: int,
    capture: tuple[type[Exception], ...],
    thread_name_prefix: str = "worker",
) -> list[Outcome[R]]:
    """Apply ``worker`` to every item, returning outcomes in **input order**.

    ``concurrency`` bounds how many workers run at once; ``1`` runs inline.
    Exceptions listed in ``capture`` are returned as failed outcomes; any other
    exception propagates to the caller once the pool has drained, from the
    earliest failing position, so which one surfaces is deterministic too.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    if concurrency == 1 or len(items) < 2:
        return [_call(worker, item, capture) for item in items]

    with ThreadPoolExecutor(
        max_workers=min(concurrency, len(items)),
        thread_name_prefix=thread_name_prefix,
    ) as pool:
        futures = [pool.submit(worker, item) for item in items]

    outcomes: list[Outcome[R]] = []
    for future in futures:  # input order, never completion order
        try:
            outcomes.append(Outcome(value=future.result()))
        except capture as exc:
            outcomes.append(Outcome(error=exc))
    return outcomes


def _call(worker: Callable[[T], R], item: T, capture: tuple[type[Exception], ...]) -> Outcome[R]:
    try:
        return Outcome(value=worker(item))
    except capture as exc:
        return Outcome(error=exc)
