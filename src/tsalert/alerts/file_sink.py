from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tsalert.sources.base import TransientSourceError


class FileChannel:
    """Appends every alert to a JSONL file. The channel that cannot go down.

    Every other channel depends on something outside this process: a network
    that allows the host, a token that has not been revoked, a webhook that
    still exists. All three failed here during development, at different
    times, and each one looked different from the outside. This one depends
    on the local disk, so when the remote channels are all failing there is
    still a complete, ordered record of what the agent decided and when.

    That makes it the audit trail as much as a delivery channel: it is what
    you diff against the store when you want to know whether a missing alert
    was never detected or merely never delivered.

    Writes are append only and flushed per line. A crash mid-run truncates at
    most the last line rather than corrupting what came before, and the
    dispatcher's own claim row is what stops a restart writing a duplicate.

    "Cannot go down" is about dependencies, not about being infallible. A
    full disk, a read only mount or a permissions change all still fail, and
    because this channel runs first that failure used to escape the
    dispatcher entirely and kill the poll loop before any other channel was
    tried. The one channel meant to be the safety net was the only one whose
    failure took the others with it. Disk errors are now transient failures
    like any other channel's, so the alert still reaches console and the
    remote channels and gets retried here later.
    """

    name = "file"

    def __init__(self, path: str | Path = "data/alerts.jsonl") -> None:
        self.path = Path(path)

    def is_configured(self) -> bool:
        return True

    def send(self, text: str) -> None:
        record = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "text": text,
        }
        line = json.dumps(record, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Append mode plus an explicit flush and fsync. Without the fsync
            # the line can sit in the OS buffer through a power loss, which is
            # one of the failures this channel exists to survive.
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Transient, not permanent: a full disk gets emptied and a bad
            # mount gets fixed, so this is worth retrying. Raising the OSError
            # instead put an exception the dispatcher does not catch into the
            # first channel of every dispatch, which killed the poll loop and
            # then killed it again on restart, since retry_failed runs first
            # and hit the same channel.
            raise TransientSourceError(
                f"alert file write failed: {type(exc).__name__}: {exc}"
            ) from None

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Most recent alerts first. Used by the dashboard.

        A half written final line is skipped rather than raised on: this file
        is a record of what happened, and refusing to show any of it because
        the last line was cut short would be the wrong trade. That covers a
        truncated multi byte character as well as truncated JSON.

        A limit of zero or less returns nothing, rather than the whole file
        the way a bare negative slice would.
        """
        if limit <= 0:
            return []
        if not self.path.exists():
            return []
        records = []
        # errors="replace" because a crash mid-write can cut a multi byte
        # character in half. Strict decoding turned that into a
        # UnicodeDecodeError for the whole file, so one truncated emoji made
        # every earlier line unreadable, which is the opposite of what this
        # method promises.
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records[-limit:][::-1]
