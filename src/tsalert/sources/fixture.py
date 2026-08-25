from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tsalert.models import Post
from tsalert.sources.base import SourceHealth
from tsalert.sources.parse import parse_status


class FixtureSource:
    """Replays recorded API pages from disk. Used by tests and --source fixture."""

    def __init__(self, paths: list[Path], source_name: str = "fixture") -> None:
        self.name = source_name
        self._posts = self._load(paths)
        self._last_success: datetime | None = None

    def _load(self, paths: list[Path]) -> list[Post]:
        posts = []
        for path in paths:
            statuses = json.loads(Path(path).read_text())
            for status in statuses:
                posts.append(parse_status(status, source=self.name))
        posts.sort(key=lambda p: int(p.id))
        return posts

    def fetch_latest(self, since_id: str | None = None, limit: int = 20) -> list[Post]:
        posts = self._posts
        if since_id is not None:
            threshold = int(since_id)
            posts = [p for p in posts if int(p.id) > threshold]
        self._last_success = datetime.now(timezone.utc)
        return posts[-limit:] if limit else posts

    def fetch_history(self, before_id: str | None = None, limit: int = 20) -> list[Post]:
        posts = self._posts
        if before_id is not None:
            threshold = int(before_id)
            posts = [p for p in posts if int(p.id) < threshold]
        # Newest-first, mirroring the real API's max_id pagination pages.
        ordered = sorted(posts, key=lambda p: int(p.id), reverse=True)
        self._last_success = datetime.now(timezone.utc)
        return ordered[:limit] if limit else ordered

    def health(self) -> SourceHealth:
        return SourceHealth(
            ok=self._last_success is not None,
            last_success=self._last_success,
            detail="fixture source" if self._last_success else "no fetch performed yet",
        )
