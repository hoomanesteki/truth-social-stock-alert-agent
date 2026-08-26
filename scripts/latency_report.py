#!/usr/bin/env python3
"""Report latency percentiles from the latency table.

    uv run python scripts/latency_report.py --db data/agent.db

Reports, in seconds, count/p50/p95/max for end to end (published to
delivered) and for each stage: publish to fetch, fetch to detect, detect to
deliver.

Publish to fetch is bounded below by the polling interval, since a post
cannot be fetched before the next poll after it goes up. That single stage
therefore dominates the end to end total for almost every post, which is
the latency versus request volume trade off central to this project: poll
more often and end to end latency falls, but so does the margin before
Cloudflare or a rate limit notices. AdaptiveInterval backs off during quiet
stretches for exactly this reason, and this report is what shows whether
that trade is landing where it should.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tsalert.models import parse_iso_datetime

_STAGES = [
    ("end to end", "published_at", "delivered_at"),
    ("publish to fetch", "published_at", "fetched_at"),
    ("fetch to detect", "fetched_at", "detected_at"),
    ("detect to deliver", "detected_at", "delivered_at"),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Latency percentile report")
    parser.add_argument("--db", default="data/agent.db", help="path to the sqlite db")
    return parser.parse_args(argv)


def load_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT published_at, fetched_at, detected_at, delivered_at FROM latency"
        ).fetchall()
    finally:
        conn.close()


def stage_durations(rows: list[sqlite3.Row], start_col: str, end_col: str) -> list[float]:
    durations = []
    for row in rows:
        start, end = row[start_col], row[end_col]
        if not start or not end:
            continue
        seconds = (parse_iso_datetime(end) - parse_iso_datetime(start)).total_seconds()
        if seconds < 0:
            # Clock skew between the values that produced these timestamps,
            # not a real negative duration. Drop it rather than let it skew
            # the percentiles.
            continue
        durations.append(seconds)
    return durations


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    # Nearest-rank method, good enough for a report at this sample size and
    # simpler than interpolating between ranks.
    index = max(0, min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1)))))
    return sorted_values[index]


def format_report(rows: list[sqlite3.Row]) -> str:
    header = f"{'stage':<20}{'count':>8}{'p50':>10}{'p95':>10}{'max':>10}"
    lines = [header, "-" * len(header)]
    for label, start_col, end_col in _STAGES:
        durations = sorted(stage_durations(rows, start_col, end_col))
        count = len(durations)
        p50 = percentile(durations, 0.50)
        p95 = percentile(durations, 0.95)
        worst = durations[-1] if durations else 0.0
        lines.append(f"{label:<20}{count:>8}{p50:>10.1f}{p95:>10.1f}{worst:>10.1f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = load_rows(args.db)
    print(format_report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
