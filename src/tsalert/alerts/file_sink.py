from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class FileChannel:
    """Appends every alert to a JSONL file. The channel that cannot go down.

    Every other channel depends on something outside this process: a network
    that allows the host, a token that has not been revoked, a webhook that
    still exists. All three failed here during development, at different
    times, and each one looked different from the outside. This one depends
    on the local disk, so when the remote channels are all failing there is
    still a complete, ordered record of what the agent decided and when.

    That makes it the audit trail as much as a delivery channel: the
    dashboard reads it, and it is what you diff against the store when you
    want to know whether a missing alert was never detected or merely never
    delivered.

    Writes are append only and flushed per line. A crash mid-run truncates at
    most the last line rather than corrupting what came before, and the
    dispatcher's own claim row is what stops a restart writing a duplicate.
    """

    name = "file"

    def __init__(self, path: str | Path = "data/alerts.jsonl") -> None:
        self.path = Path(path)

    def is_configured(self) -> bool:
        return True

    def send(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            "text": text,
        }
        line = json.dumps(record, ensure_ascii=False)
        # Append mode plus an explicit flush and fsync. Without the fsync the
        # line can sit in the OS buffer through a power loss, which is the
        # one failure this channel exists to survive.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Most recent alerts first. Used by the dashboard.

        A half written final line is skipped rather than raised on: this file
        is a record of what happened, and refusing to show any of it because
        the last line was cut short would be the wrong trade.
        """
        if not self.path.exists():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records[-limit:][::-1]
