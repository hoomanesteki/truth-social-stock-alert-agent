from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tsalert.models import Detection, Post, TickerMention, parse_iso_datetime

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
    mentions_json TEXT,
    alert_eligible INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alerts (
    post_id TEXT,
    channel TEXT,
    status TEXT,
    attempts INTEGER,
    cycles INTEGER DEFAULT 0,
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

_CHANNEL_PAUSED_PREFIX = "channel_paused_"
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
        self._migrate_add_alert_eligible()
        self._migrate_add_alert_cycles()
        self._conn.commit()

    def _migrate_add_quoted_text(self) -> None:
        # Databases created before quoted_text existed are still around locally.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(posts)")}
        if "quoted_text" not in columns:
            self._conn.execute("ALTER TABLE posts ADD COLUMN quoted_text TEXT DEFAULT ''")

    def _migrate_add_alert_cycles(self) -> None:
        # Databases created before the attempts/cycles split existed are still
        # around locally. Existing rows default to 0 cycles, which is correct:
        # they have not yet been through a retry_failed pass under the new scheme.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(alerts)")}
        if "cycles" not in columns:
            self._conn.execute("ALTER TABLE alerts ADD COLUMN cycles INTEGER DEFAULT 0")

    def _migrate_add_alert_eligible(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(posts)")}
        if "alert_eligible" not in cols:
            self._conn.execute(
                "ALTER TABLE posts ADD COLUMN alert_eligible INTEGER NOT NULL DEFAULT 1"
            )
            self._conn.commit()

    def is_alert_eligible(self, post_id: str) -> bool:
        row = self._conn.execute(
            "SELECT alert_eligible FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        return True if row is None else bool(row["alert_eligible"])

    def set_alert_eligible(self, post_id: str, eligible: bool) -> None:
        """Mark whether a post may ever produce an alert.

        Priming and backfill both store real detections for posts nobody wants
        an alert about. Without a durable flag the recovery sweep cannot tell
        those apart from a post that crashed before delivery, and sends them.
        """
        self._conn.execute(
            "UPDATE posts SET alert_eligible=? WHERE id=?", (int(eligible), post_id)
        )
        self._conn.commit()

    def upsert_post(self, post: Post, alert_eligible: bool = True) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO posts "
            "(id, account, created_at, text, url, raw_html, is_reply, is_repost, "
            "is_quote, has_media, source, fetched_at, quoted_text, alert_eligible) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                int(alert_eligible),
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
        self,
        post_id: str,
        channel: str,
        status: str,
        error: str | None = None,
        attempts_made: int = 1,
        is_retry: bool = False,
    ) -> None:
        """Record the outcome of a send.

        attempts counts real sends and accumulates across every call, dispatch()
        and retry_failed() alike. cycles is a separate lifetime counter that only
        moves when is_retry is True, i.e. when this result comes from a
        retry_failed pass rather than the original dispatch(). That split is what
        lets retryable_alerts bound lifetime retries independently of the
        per-call send budget (max_attempts).
        """
        now = datetime.now(timezone.utc).isoformat()
        delivered_at = now if status == "delivered" else None
        cycle_increment = 1 if is_retry else 0
        self._conn.execute(
            "UPDATE alerts SET status=?, attempts=attempts+?, cycles=cycles+?, "
            "delivered_at=COALESCE(?, delivered_at), last_error=? WHERE post_id=? AND channel=?",
            (status, attempts_made, cycle_increment, delivered_at, error, post_id, channel),
        )
        self._conn.commit()

    def channel_stats(self) -> dict[str, dict[str, Any]]:
        """Per channel delivery counts and the last error each one saw.

        The aggregate in stats() hides the thing you actually want during an
        outage, which is which channel is failing. One channel sitting at
        zero delivered with a queue behind it is a different problem from
        every channel failing at once.
        """
        rows = self._conn.execute(
            "SELECT channel, status, COUNT(*) AS n FROM alerts GROUP BY channel, status"
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = out.setdefault(
                row["channel"],
                {"delivered": 0, "queued": 0, "failed": 0, "given_up": 0, "last_error": None},
            )
            key = {
                "delivered": "delivered",
                "pending": "queued",
                "failed": "failed",
                "permanent_failure": "given_up",
            }.get(row["status"])
            if key:
                entry[key] = row["n"]
        for channel in out:
            err = self._conn.execute(
                "SELECT last_error FROM alerts WHERE channel=? AND last_error IS NOT NULL "
                "ORDER BY first_attempt_at DESC LIMIT 1",
                (channel,),
            ).fetchone()
            out[channel]["last_error"] = err["last_error"] if err else None
        return out

    def is_channel_paused(self, channel: str) -> bool:
        """Paused channels are skipped without being recorded as failures.

        Set from the dashboard and read by the running agent, so the two
        processes agree through the database rather than needing a restart.
        """
        return self.get_state(_CHANNEL_PAUSED_PREFIX + channel) == "1"

    def set_channel_paused(self, channel: str, paused: bool) -> None:
        self.set_state(_CHANNEL_PAUSED_PREFIX + channel, "1" if paused else "0")

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

    def retryable_alerts(self, max_cycles: int, limit: int = 50) -> list[tuple[str, str]]:
        """Alerts worth retrying, as (post_id, channel).

        Covers both 'failed' (every attempt in a dispatch() call was used up
        for a transient reason, so the channel may simply have been down) and
        'pending' (claim_alert inserted the row but the process crashed
        before record_alert_result ever ran). A 'pending' row is not a sign
        delivery is still in flight, since nothing else in this codebase can
        be mid-send concurrently: it is a stuck claim, and stuck is exactly
        what retrying is for.

        Status alone excludes 'permanent_failure': a 4xx, bad token or bad
        chat id cannot succeed by retrying, so those rows are never selected
        here, however many cycles pass.

        cycles is the lifetime retry budget, separate from max_attempts (the
        per-call send budget). In practice it now bounds very little: the
        dispatcher does not spend a cycle on a failure that took the whole
        channel down, because that failure says nothing about the individual
        alert. A channel that stays down therefore keeps its queue rather
        than working through max_cycles and discarding it, which is the right
        trade for an alerting system but leaves the queue bounded by nothing.

        That is what limit is for. Oldest first, so the queue drains in the
        order the alerts happened and one poll cannot be swallowed by a long
        backlog. The depth itself shows up in `agent.py stats` as
        alerts_queued, since a backlog nothing reports is a backlog nobody
        notices.
        """
        rows = self._conn.execute(
            "SELECT post_id, channel FROM alerts "
            "WHERE status IN ('failed', 'pending') AND cycles < ? "
            "ORDER BY first_attempt_at ASC LIMIT ?",
            (max_cycles, limit),
        ).fetchall()
        return [(row["post_id"], row["channel"]) for row in rows]

    def undelivered_stock_posts(self, channel: str, limit: int = 50) -> list[str]:
        """Stock related post ids with no alerts row at all for this channel.

        dedup and delivery are separate concerns on separate schedules.
        upsert_post answers "have I seen this post before", which is
        permanent and global: once a post row exists it never goes away and
        is never revisited by dedup. claim_alert/record_alert_result answer
        "have I delivered this alert on this channel", which is per channel
        and can fail independently of dedup, including failing before a
        claim row is ever written (a crash between save_detection and
        dispatch). Conflating the two, by letting upsert_post's is_new gate
        dispatch, is what let a correctly detected post vanish forever if
        the process died before claim_alert ran. This query is the recovery
        path for exactly that gap: it finds posts dedup already knows about
        that this channel has no record of ever having tried.

        alert_eligible keeps priming and backfill out of it. Those store real
        detections for posts nobody wants an alert about, and without the flag
        this query cannot tell them from a post that crashed before delivery.
        """
        rows = self._conn.execute(
            "SELECT id FROM posts WHERE is_stock_related = 1 "
            "AND COALESCE(alert_eligible, 1) = 1 "
            "AND id NOT IN (SELECT post_id FROM alerts WHERE channel = ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (channel, limit),
        ).fetchall()
        return [row["id"] for row in rows]

    def undetected_posts(self, limit: int = 50) -> list[Post]:
        """Posts that were ingested but never made it through the detector.

        detected_at IS NULL is what a crash between upsert_post and
        save_detection leaves behind: the posts row exists, so upsert_post's
        is_new gate will never return True for this id again, and nothing
        else in the codebase revisits a row once it exists. This is the same
        gap undelivered_stock_posts closes for detected-but-undelivered
        posts, one stage earlier in the pipeline. Bounded like that query so
        a large backlog cannot stall a poll cycle.
        """
        rows = self._conn.execute(
            "SELECT * FROM posts WHERE detected_at IS NULL ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_post(row) for row in rows]

    def get_post_with_detection(self, post_id: str) -> tuple[Post, Detection] | None:
        """Rebuild a post and its stored detection, for retrying a failed delivery."""
        row = self._conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if row is None:
            return None
        raw = json.loads(row["mentions_json"]) if row["mentions_json"] else []
        detection = Detection(
            post_id=post_id,
            is_stock_related=bool(row["is_stock_related"]),
            mentions=tuple(TickerMention(**m) for m in raw),
            # The detector name and timing are not persisted, so a retried alert
            # reports this placeholder. It only affects the alert's label line.
            detector="retry",
            latency_ms=0.0,
        )
        return self._row_to_post(row), detection

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
        # Queued is not the same as failed. During a channel outage the
        # dispatcher skips sends rather than burning each alert's retry
        # budget, which leaves rows at 'pending' with nothing written. Those
        # alerts are waiting, not lost, and without a count for them an
        # operator sees a backlog of zero while it quietly grows.
        queued = self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='pending'"
        ).fetchone()[0]
        permanent = self._conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='permanent_failure'"
        ).fetchone()[0]
        return {
            "posts": posts,
            "stock_related": stock_related,
            "alerts_delivered": delivered,
            "alerts_queued": queued,
            "alerts_failed": failed,
            "alerts_given_up": permanent,
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
