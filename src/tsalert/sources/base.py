from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tsalert.models import Post


@dataclass
class SourceHealth:
    ok: bool
    last_success: datetime | None
    detail: str


class SourceError(Exception):
    pass


class TransientSourceError(SourceError):
    """Network errors, 429s, 5xxs. Caller should retry."""


class PermanentSourceError(SourceError):
    """404s, schema gone. Caller should not retry."""


class BlockedSourceError(TransientSourceError):
    """403/503 with a Cloudflare challenge in the body.

    Kept distinct from a plain TransientSourceError so the circuit breaker's
    log line and the ops alert can name the actual cause (blocked, not just
    slow or overloaded) even though it is handled like any other transient
    failure.
    """


def id_sort_key(post_id: str) -> tuple[int, int, str]:
    """Order ids numerically when they are numeric, lexically otherwise.

    Truth Social ids are numeric strings, but the RSS mirror passes through
    whatever the feed provides, so this must not raise on anything.
    """
    try:
        return (0, int(post_id), "")
    except (TypeError, ValueError):
        return (1, 0, str(post_id))


class PostSource(Protocol):
    name: str

    def fetch_latest(self, since_id: str | None = None, limit: int = 20) -> list[Post]:
        """Return posts newer than since_id, oldest-first."""
        ...

    def fetch_history(self, before_id: str | None = None, limit: int = 20) -> list[Post]:
        ...

    def health(self) -> SourceHealth:
        ...
