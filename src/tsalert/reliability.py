from __future__ import annotations

import logging
import math
import random
import time
from typing import Callable, TypeVar

from tsalert.sources.base import PermanentSourceError, TransientSourceError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Longest we will ever sit inside a single retry waiting on a server's
# Retry-After. Real rate limits ask for seconds or low minutes; anything past
# this is either a mistake or a server telling us to go away for the day, and
# neither is worth blocking a poll for.
MAX_RETRY_AFTER_SECONDS = 300.0


def sanitize_retry_after(value: object, ceiling: float = MAX_RETRY_AFTER_SECONDS) -> float | None:
    """Turn a server-supplied Retry-After into a delay that is safe to sleep on.

    The value comes off the wire, so it is whatever the other end felt like
    sending. Passing it to time.sleep unchecked is what makes a hostile or
    broken header dangerous: a negative raises ValueError out of the retry
    loop and kills the poll, inf raises OverflowError, nan compares false
    against every bound and sleeps forever, and a plain large number hangs
    the agent for as long as it says.

    None means the value is unusable and the caller should fall back to its
    own exponential backoff. Values above the ceiling are clamped rather than
    dropped: the server did ask us to wait, and waiting the ceiling is both
    polite and bounded.
    """
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(delay) or delay < 0:
        logger.warning("ignoring nonsense Retry-After (%r), backing off normally", value)
        return None
    if delay > ceiling:
        logger.warning("Retry-After asked for %.0fs, waiting %.0fs instead", delay, ceiling)
        return ceiling
    return delay


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
            retry_after = sanitize_retry_after(getattr(exc, "retry_after", None))
            if retry_after is not None:
                delay = retry_after
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
    the interval during the gaps roughly halves request volume: replaying the
    real history at the deployed 30 to 60 second range gives about 1,450
    requests a day against 2,880 for polling flat out every 30 seconds.
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
        # Jitter is applied to the returned delay only. The stored interval
        # stays clean, so repeated quiet polls grow smoothly rather than
        # compounding the randomness.
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
            # Every failure past the threshold pushes opened_at forward, not
            # just the first one. That way a failed half-open probe restarts
            # the full cooldown instead of letting is_open flip back and
            # forth on every call.
            self._opened_at = self._clock()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return (self._clock() - self._opened_at) < self.cooldown_seconds
