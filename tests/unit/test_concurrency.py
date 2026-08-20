"""The bounded-concurrency primitive both network stages are built on.

Everything downstream — the manifest, the cache, the selection, the artifacts —
rests on one promise made here: results come back in input order, whatever order
the work finished in. These tests use small sleeps to force completion order to
disagree with input order, never to simulate real latency.
"""

from __future__ import annotations

import threading
import time

import pytest

from newsletter.concurrency import map_ordered


class Tracker:
    """Counts how many workers are inside the callable at the same moment."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.threads: set[int] = set()
        self.finished: list[int] = []

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.get_ident())

    def leave(self, item: int) -> None:
        with self.lock:
            self.active -= 1
            self.finished.append(item)


def slow_double(tracker: Tracker, delays: dict[int, float]):
    def worker(item: int) -> int:
        tracker.enter()
        try:
            time.sleep(delays.get(item, 0.0))
            return item * 2
        finally:
            tracker.leave(item)

    return worker


def test_results_follow_the_input_even_when_completion_is_reversed() -> None:
    tracker = Tracker()
    items = [0, 1, 2, 3]
    # The first item finishes last, so completion order is the reverse of input.
    delays = {0: 0.08, 1: 0.05, 2: 0.02, 3: 0.0}

    outcomes = map_ordered(
        slow_double(tracker, delays), items, concurrency=4, capture=(ValueError,)
    )

    assert [outcome.value for outcome in outcomes] == [0, 2, 4, 6]
    assert tracker.finished != items  # the race really did happen
    assert tracker.peak > 1


def test_concurrency_is_bounded_by_the_limit() -> None:
    tracker = Tracker()
    items = list(range(12))

    map_ordered(
        slow_double(tracker, dict.fromkeys(items, 0.02)),
        items,
        concurrency=3,
        capture=(ValueError,),
    )

    assert tracker.peak <= 3
    assert tracker.peak > 1


def test_one_is_one_thread_and_strictly_in_order() -> None:
    """The escape hatch: concurrency=1 is the sequential code it replaced."""
    tracker = Tracker()
    items = list(range(5))

    outcomes = map_ordered(
        slow_double(tracker, {0: 0.02}), items, concurrency=1, capture=(ValueError,)
    )

    assert [outcome.value for outcome in outcomes] == [0, 2, 4, 6, 8]
    assert tracker.finished == items  # executed front to back
    assert tracker.threads == {threading.get_ident()}  # no worker thread at all
    assert tracker.peak == 1


def test_a_captured_failure_travels_back_in_its_own_slot() -> None:
    def worker(item: int) -> int:
        if item == 1:
            raise ValueError(f"no good: {item}")
        return item * 2

    outcomes = map_ordered(worker, [0, 1, 2], concurrency=3, capture=(ValueError,))

    assert [outcome.failed for outcome in outcomes] == [False, True, False]
    assert str(outcomes[1].error) == "no good: 1"
    assert [outcome.value for outcome in outcomes] == [0, None, 4]


def test_an_uncaptured_failure_is_not_filed_as_a_result() -> None:
    """A bug is not a per-article failure: it still stops the caller."""

    def worker(item: int) -> int:
        raise RuntimeError("this is not a fetch failure")

    with pytest.raises(RuntimeError, match="not a fetch failure"):
        map_ordered(worker, [0, 1], concurrency=2, capture=(ValueError,))


def test_a_concurrency_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        map_ordered(lambda item: item, [1], concurrency=0, capture=())
