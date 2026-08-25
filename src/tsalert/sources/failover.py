from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tsalert.models import Post
from tsalert.reliability import CircuitBreaker
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

    Break 5 turns transitions into an ops alert, so every switch (primary to
    fallback and back) is logged at WARNING and recorded on last_transition
    for easy observation, not just printed.
    """

    name = "failover"

    def __init__(self, primary: Any, fallback: Any, breaker: CircuitBreaker | None = None) -> None:
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
            result = getattr(self.primary, method_name)(**kwargs)
        except SourceError as exc:
            self.breaker.record_failure()
            if self.breaker.is_open:
                self._transition(self.fallback.name, f"primary failed: {exc}")
                return getattr(self.fallback, method_name)(**kwargs)
            # Below threshold: let this single failure propagate. The caller's
            # own retry logic decides what to do with a transient hiccup.
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
