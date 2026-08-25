from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tsalert.models import Detection, Post, parse_iso_datetime

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    account TEXT,
    created_at TEXT,
    text TEXT,
    url TEXT,
    raw_html TEXT,
    is_reply INTEGER,
    is_repost INTEGER,
    is_quote INTEGER,
    has_media INTEGER,
    source TEXT,
    fetched_at TEXT,
    quoted_text TEXT,
    detected_at TEXT,
    is_stock_related INTEGER,
    mentions_json TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    post_id TEXT,
    channel TEXT,
    status TEXT,
    attempts INTEGER,
    first_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT,
    PRIMARY KEY (post_id, channel)
);

CREATE TABLE IF NOT EXISTS agent_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS latency (
    post_id TEXT PRIMARY KEY,
    published_at TEXT,
    fetched_at TEXT,
    detected_at TEXT,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_is_stock_related ON posts(is_stock_related);
"""

_LATENCY_COLUMNS = {"published_at", "fetched_at", "detected_at", "delivered_at"}


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row

    def __enter__(self) -> "Store":
        self.init_schema()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._migrate_add_quoted_text()
        self._conn.commit()

    def _migrate_add_quoted_text(self) -> None:
        # Dev DBs created by Break 1 predate the quoted_text column.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(posts)")}
        if "quoted_text" not in columns:
            self._conn.execute("ALTER TABLE posts ADD COLUMN quoted_text TEXT DEFAULT ''")

    def upsert_post(self, post: Post) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO posts "
            "(id, account, created_at, text, url, raw_html, is_reply, is_repost, "
            "is_quote, has_media, source, fetched_at, quoted_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                post.id,
                post.account,
                post.created_at.isoformat(),
                post.text,
                post.url,
                post.raw_html,
                int(post.is_reply),
                int(post.is_repost),
                int(post.is_quote),
                int(post.has_media),
                post.source,
                post.fetched_at.isoformat(),
                post.quoted_text,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def has_post(self, post_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone()
        return row is not None

    def save_detection(self, detection: Detection) -> None:
        mentions_json = json.dumps([asdict(m) for m in detection.mentions])
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE posts SET detected_at=?, is_stock_related=?, mentions_json=? WHERE id=?",
            (now, int(detection.is_stock_related), mentions_json, detection.post_id),
        )
        self._conn.commit()

    def claim_alert(self, post_id: str, channel: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO alerts (post_id, channel, status, attempts, first_attempt_at) "
            "VALUES (?, ?, 'pending', 0, ?)",
            (post_id, channel, now),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def record_alert_result(
        self, post_id: str, channel: str, status: str, error: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        delivered_at = now if status == "delivered" else None
        self._conn.execute(
            "UPDATE alerts SET status=?, attempts=attempts+1, "
            "delivered_at=COALESCE(?, delivered_at), last_error=? WHERE post_id=? AND channel=?",
            (status, delivered_at, error, post_id, channel),
        )
        self._conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM agent_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO agent_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        self._conn.commit()

    def record_latency(self, post_id: str, **stamps: str) -> None:
        unknown = set(stamps) - _LATENCY_COLUMNS
        if unknown:
            raise ValueError(f"unknown latency stamps: {sorted(unknown)}")
        self._conn.execute("INSERT OR IGNORE INTO latency (post_id) VALUES (?)", (post_id,))
        if stamps:
            columns = ", ".join(f"{col}=?" for col in stamps)
            values = list(stamps.values()) + [post_id]
            self._conn.execute(f"UPDATE latency SET {columns} WHERE post_id=?", values)
        self._conn.commit()

    def recent_posts(self, limit: int = 50) -> list[Post]:
        rows = self._conn.execute(
            "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_post(row) for row in rows]

    def iter_posts(self) -> Iterator[Post]:
        cur = self._conn.execute("SELECT * FROM posts ORDER BY created_at ASC")
        for row in cur:
            yield self._row_to_post(row)

    def stats(self) -> dict[str, int]:
        posts = self._conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        stock_related = self._conn.execute(
            "SELECT COUNT(*) FROM posts WHERE is_stock_related=1"
        ).fetchone()[0]
        delivered = self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='delivered'"
        ).fetchone()[0]
        failed = self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='failed'"
        ).fetchone()[0]
        return {
            "posts": posts,
            "stock_related": stock_related,
            "alerts_delivered": delivered,
            "alerts_failed": failed,
        }

    @staticmethod
    def _row_to_post(row: sqlite3.Row) -> Post:
        return Post(
            id=row["id"],
            account=row["account"],
            created_at=parse_iso_datetime(row["created_at"]),
            text=row["text"],
            url=row["url"],
            raw_html=row["raw_html"],
            is_reply=bool(row["is_reply"]),
            is_repost=bool(row["is_repost"]),
            is_quote=bool(row["is_quote"]),
            has_media=bool(row["has_media"]),
            source=row["source"],
            fetched_at=parse_iso_datetime(row["fetched_at"]),
            quoted_text=row["quoted_text"] or "",
        )
