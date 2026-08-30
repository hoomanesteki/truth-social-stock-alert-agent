from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tsalert.models import Post
from tsalert.reliability import CircuitBreaker, with_retries
from tsalert.sources.base import SourceError, SourceHealth

logger = logging.getLogger(__name__)


@dataclass
class Transition:
    at: datetime
    from_source: str
    to_source: str
    reason: str


class FailoverSource:
    """Wraps a primary and a fallback source behind one PostSource interface.

    Every switch, primary to fallback and back again, is logged at WARNING and
    recorded on last_transition, which is what the dashboard reads. Nothing
    dispatches a transition as an ops alert today: it is visible, not pushed.
    """

    name = "failover"

    def __init__(self, primary: Any, fallback: Any, breaker: CircuitBreaker | None = None,
                 retry_attempts: int = 3, sleep: Any = None) -> None:
        # Retries live in here rather than in the caller. The breaker counts
        # one failure per call, so when the runner wrapped this in
        # with_retries(attempts=3) a single poll spent the entire threshold
        # of 3 and failed over inside that one poll. One six second blip
        # demoted ingestion to the slower mirror for the whole cooldown, and
        # the documented "three consecutive failures" read as three polls,
        # which is not what happened. Retrying before counting restores that
        # meaning: one poll, one tick.
        self.retry_attempts = retry_attempts
        self._sleep = sleep or time.sleep
        self.primary = primary
        self.fallback = fallback
        self.breaker = breaker or CircuitBreaker()
        self._active = primary.name
        self.last_transition: Transition | None = None

    @property
    def active_source_name(self) -> str:
        return self._active

    def fetch_latest(self, since_id: str | None = None, limit: int = 20) -> list[Post]:
        return self._call("fetch_latest", since_id=since_id, limit=limit)

    def fetch_history(self, before_id: str | None = None, limit: int = 20) -> list[Post]:
        return self._call("fetch_history", before_id=before_id, limit=limit)

    def health(self) -> SourceHealth:
        active = self.primary if self._active == self.primary.name else self.fallback
        return active.health()

    def _call(self, method_name: str, **kwargs: Any) -> list[Post]:
        if self.breaker.is_open:
            self._transition(self.fallback.name, "primary breaker open, cooldown still active")
            return getattr(self.fallback, method_name)(**kwargs)

        try:
            result = with_retries(
                lambda: getattr(self.primary, method_name)(**kwargs),
                attempts=self.retry_attempts,
                sleep=self._sleep,
            )
        except SourceError as exc:
            self.breaker.record_failure()
            if self.breaker.is_open:
                self._transition(self.fallback.name, f"primary failed: {exc}")
                return getattr(self.fallback, method_name)(**kwargs)
            # Below threshold: let it propagate. The poll fails and the next
            # one tries the primary again, which is the right cost for a blip
            # compared with demoting ingestion to the mirror.
            raise

        self.breaker.record_success()
        self._transition(self.primary.name, "primary recovered")
        return result

    def _transition(self, to_source: str, reason: str) -> None:
        if to_source == self._active:
            return
        transition = Transition(
            at=datetime.now(timezone.utc),
            from_source=self._active,
            to_source=to_source,
            reason=reason,
        )
        self.last_transition = transition
        logger.warning(
            "source failover: %s -> %s (%s)", transition.from_source, transition.to_source, reason
        )
        self._active = to_source
