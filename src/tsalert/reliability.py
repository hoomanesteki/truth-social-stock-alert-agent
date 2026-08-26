from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from tsalert.sources.base import PermanentSourceError, TransientSourceError

T = TypeVar("T")


def with_retries(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Retry fn on TransientSourceError, never on PermanentSourceError.

    rng is injectable (constructor-style default arg) so tests can seed it and
    assert exact delays instead of just ranges.
    """
    rng = rng if rng is not None else random.Random()
    last_exc: TransientSourceError | None = None

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except PermanentSourceError:
            raise
        except TransientSourceError as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                delay = float(retry_after)
            else:
                cap = min(max_delay, base_delay * (2 ** (attempt - 1)))
                # Full jitter (AWS backoff-and-jitter): pick uniformly under the
                # cap. `jitter` sets a floor as a fraction of the cap so a lucky
                # near-zero draw doesn't turn a backoff into an instant retry.
                delay = rng.uniform(cap * jitter, cap)
            sleep(delay)

    # Unreachable: the loop above always returns or raises.
    raise last_exc  # pragma: no cover


class AdaptiveInterval:
    """Grows the poll interval when nothing is arriving, snaps back when it is.

    His posting is bursty, long gaps then several in a row, so stretching
    the interval during the gaps drops request volume by about two thirds.
    Latency barely moves, because the first post of a burst pulls the
    interval straight back to base.
    """

    def __init__(
        self,
        base: float = 60,
        max_interval: float = 300,
        growth: float = 1.5,
        jitter: float = 0.2,
        rng: random.Random | None = None,
    ) -> None:
        self.base = base
        self.max_interval = max_interval
        self.growth = growth
        self.jitter = jitter
        self._rng = rng if rng is not None else random.Random()
        self._current = base

    def next_delay(self, got_new_posts: bool) -> float:
        if got_new_posts:
            self._current = self.base
        else:
            self._current = min(self.max_interval, self._current * self.growth)
        # Jitter only affects what we return here. The stored
        # state, so repeated quiet polls grow smoothly instead of drifting.
        jittered = self._current * (1 + self._rng.uniform(-self.jitter, self.jitter))
        return max(0.0, jittered)


class CircuitBreaker:
    """Opens after threshold consecutive failures, half-opens after cooldown."""

    def __init__(
        self,
        threshold: int = 3,
        cooldown_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold:
            # Push opened_at forward on each failure once past threshold. Doing it only on the
            # first, so a failed half-open probe restarts the full cooldown
            # instead of letting is_open flip back and forth every call.
            self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return (self._clock() - self._opened_at) < self.cooldown_seconds
