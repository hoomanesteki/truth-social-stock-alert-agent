#!/usr/bin/env python3
"""Control room dashboard for the alert agent.

    uv run python scripts/dashboard.py --port 8000 --db data/agent.db

One file, standard library http.server, inline CSS and JS, no external
assets or CDN links. The page renders a static shell on first load, then a
tiny client side script fetches GET /api/state and paints every dynamic
figure from that JSON, repeating every 10 seconds. That is deliberate: a
full page reload every 10 seconds would blow away anything half typed into
the custom interval or backfill fields.

There is no authentication of any kind. This is a local prototype and must
never be bound to anything other than localhost or exposed to a network.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _extra in (str(_REPO_ROOT / "src"), str(_SCRIPTS_DIR)):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from latency_report import _STAGES, load_rows, percentile, stage_durations  # noqa: E402
from tsalert.models import parse_iso_datetime  # noqa: E402
from tsalert.monitor import HealthMonitor  # noqa: E402
from tsalert.store import Store  # noqa: E402

_DEFAULT_DB = "data/agent.db"
_DEFAULT_LEXICON = _REPO_ROOT / "data" / "lexicon" / "tickers.csv"
_DEFAULT_PID = _REPO_ROOT / "data" / "agent.pid"
_DEFAULT_METRICS = _REPO_ROOT / "data" / "eval" / "metrics.md"
_AGENT_SCRIPT = _REPO_ROOT / "agent.py"
_BACKFILL_SCRIPT = _REPO_ROOT / "scripts" / "backfill.py"
_LEXICON_HEADER = ["ticker", "company", "aliases", "ambiguity", "ambiguous_aliases", "kind", "notes"]
_TEXT_PREVIEW_CHARS = 200
_DEFAULT_INTERVAL_SECONDS = 90
_DEFAULT_BACKFILL_DAYS = 45
_STOP_WAIT_SECONDS = 2.0

_METRICS_RE = re.compile(
    r"### (?P<arm>\w+) arm\s*\n\s*weighted:\s*precision=(?P<precision>[\d.]+)"
    r"\s+recall=(?P<recall>[\d.]+)\s+f1=(?P<f1>[\d.]+)"
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = _TEXT_PREVIEW_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _int_or(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _json_for_script(data: object) -> str:
    # A post's own text could legitimately contain "</script>", which would
    # otherwise close the tag early and truncate the embedded state.
    return json.dumps(data).replace("</", "<\\/")


# ---------------------------------------------------------------------------
# Process control
#
# A pid file only records what was true at the moment it was written. The
# process it names can die at any point after that, from a crash, an OOM
# kill, a machine reboot, without anything ever touching the file again. So
# "is a pid recorded" and "is that process actually alive" are two different
# questions, and only os.kill(pid, 0) answers the second one: it sends no
# signal, it just asks the kernel whether the pid still exists. Trusting the
# file alone would leave the dashboard reporting RUNNING forever after a
# crash, which is a worse failure mode than having no status page at all.
# ---------------------------------------------------------------------------


def is_running(pid: int) -> bool:
    # Reap first. An agent we spawned that has already exited stays a zombie
    # until someone waits on it, and os.kill succeeds on a zombie, so without
    # this the page reports RUNNING for a process that is dead and also
    # refuses to start a new one.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError):
        pass

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but is owned by another user. Still alive.
        return True
    except OSError:
        return False
    return True


def read_pid(pid_path: Path) -> int | None:
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def clear_pid(pid_path: Path) -> None:
    pid_path.unlink(missing_ok=True)


def process_status(pid_path: Path) -> tuple[bool, int | None]:
    """(running, pid). pid is returned even when stale, for display only."""
    pid = read_pid(pid_path)
    if pid is None:
        return False, None
    return is_running(pid), pid


# ---------------------------------------------------------------------------
# Data for the page
# ---------------------------------------------------------------------------


def pipeline_counts(db_path: str) -> dict[str, int]:
    """fetched -> has_text -> candidate -> alerted, the funnel the page draws."""
    conn = sqlite3.connect(db_path)
    try:
        fetched = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        has_text = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE text IS NOT NULL AND TRIM(text) != ''"
        ).fetchone()[0]
        candidate = conn.execute("SELECT COUNT(*) FROM posts WHERE is_stock_related = 1").fetchone()[0]
        alerted = conn.execute(
            "SELECT COUNT(DISTINCT post_id) FROM alerts WHERE status = 'delivered'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"fetched": fetched, "has_text": has_text, "candidate": candidate, "alerted": alerted}


def load_mentions_and_tickers(db_path: str) -> tuple[list[dict], list[str]]:
    """All stock related posts newest first, plus every ticker seen among them.

    Queried directly against sqlite rather than through Store: Store has no
    method for listing detections together with their mentions, and adding
    one is outside the scope of this change. The ticker list is built from
    the full set, not a truncated page of it, so the filter always offers
    every ticker that is really in the data.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, created_at, text, url, mentions_json FROM posts "
            "WHERE is_stock_related = 1 ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()

    mentions = []
    ticker_set: set[str] = set()
    for row in rows:
        raw = json.loads(row["mentions_json"]) if row["mentions_json"] else []
        tickers = [m["ticker"] for m in raw]
        ticker_set.update(tickers)
        mentions.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "tickers": tickers,
                "companies": [m["company"] for m in raw],
                "text": _truncate(row["text"] or ""),
                "url": row["url"],
            }
        )
    return mentions, sorted(ticker_set)


def latency_table(db_path: str) -> list[dict]:
    """Count/p50/p95/max per stage, reusing latency_report.py's own
    computation rather than a second copy of the percentile maths."""
    rows = load_rows(db_path)
    table = []
    for label, start_col, end_col in _STAGES:
        durations = sorted(stage_durations(rows, start_col, end_col))
        count = len(durations)
        p50 = percentile(durations, 0.50)
        p95 = percentile(durations, 0.95)
        worst = durations[-1] if durations else 0.0
        table.append({"stage": label, "count": count, "p50": p50, "p95": p95, "max": worst})
    return table


def active_alarms(monitor: HealthMonitor) -> list[str]:
    """Read only mirror of HealthMonitor.check()'s three conditions.

    check() writes suppression state on every call so a real ops alert is
    not re-sent on the very next poll. Calling it from a page meant only
    for viewing would spend that suppression window itself and could hide
    a real alarm the next time the agent actually checks, so this
    recomputes the same three conditions from status(), which only reads.
    """
    status = monitor.status()
    now = datetime.now(timezone.utc)
    alarms = []

    last_success = status["last_successful_poll_at"]
    if last_success:
        age = now - parse_iso_datetime(last_success)
        if age > timedelta(minutes=monitor.stale_minutes):
            alarms.append(f"no_successful_poll: last successful poll was {age} ago")

    last_new_post = status["last_new_post_at"]
    if last_new_post:
        age = now - parse_iso_datetime(last_new_post)
        if age > timedelta(hours=monitor.no_posts_hours):
            alarms.append(f"no_new_posts: no new post in {age}")

    consecutive_errors = status["consecutive_errors"] or 0
    if consecutive_errors >= monitor.error_threshold:
        alarms.append(f"repeated_errors: {consecutive_errors} consecutive poll failures")

    return alarms


def read_metrics(metrics_path: Path) -> list[dict] | None:
    """Precision/recall/f1 per detector arm from the headline section of
    data/eval/metrics.md, or None when the file has not been produced yet."""
    if not metrics_path.exists():
        return None
    text = metrics_path.read_text(encoding="utf-8")
    matches = [
        {
            "arm": m.group("arm"),
            "precision": float(m.group("precision")),
            "recall": float(m.group("recall")),
            "f1": float(m.group("f1")),
        }
        for m in _METRICS_RE.finditer(text)
    ]
    return matches or None


def build_state(db_path: str, pid_path: Path, metrics_path: Path, ticker_filter: str | None) -> dict:
    with Store(db_path) as store:
        stats = store.stats()
        monitor = HealthMonitor(store)
        health = monitor.status()
        alarms = active_alarms(monitor)
        interval = _int_or(store.get_state("poll_interval_seconds"), _DEFAULT_INTERVAL_SECONDS)
        backfill_days = _int_or(store.get_state("backfill_days"), _DEFAULT_BACKFILL_DAYS)

    running, pid = process_status(pid_path)
    consecutive_errors = health["consecutive_errors"] or 0

    if not running:
        status = "STOPPED"
    elif alarms or consecutive_errors > 0:
        status = "DEGRADED"
    else:
        status = "RUNNING"

    last_poll_at = health["last_poll_at"]
    next_poll_at = None
    if running and last_poll_at:
        next_poll_at = (parse_iso_datetime(last_poll_at) + timedelta(seconds=interval)).isoformat()

    all_mentions, tickers = load_mentions_and_tickers(db_path)
    if ticker_filter:
        mentions = [m for m in all_mentions if ticker_filter in m["tickers"]]
    else:
        mentions = all_mentions

    return {
        "status": status,
        "pid": pid,
        "stats": stats,
        "consecutive_errors": consecutive_errors,
        "last_poll_at": last_poll_at,
        "last_successful_poll_at": health["last_successful_poll_at"],
        "last_new_post_at": health["last_new_post_at"],
        "poll_interval_seconds": interval,
        "next_poll_at": next_poll_at,
        "backfill_days": backfill_days,
        "alarms": alarms,
        "pipeline": pipeline_counts(db_path),
        "mentions": mentions[:200],
        "tickers": tickers,
        "ticker_filter": ticker_filter,
        "latency": latency_table(db_path),
        "metrics": read_metrics(metrics_path),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def validate_lexicon_csv(text: str) -> str | None:
    """Return an error message, or None when the header row is well formed."""
    lines = text.splitlines()
    if not lines:
        return "empty file"
    header = next(csv.reader([lines[0]]))
    if header != _LEXICON_HEADER:
        return "header row must be exactly: " + ",".join(_LEXICON_HEADER)
    return None


# ---------------------------------------------------------------------------
# Page template
#
# Kept as a single non f-string template with one substitution marker for
# the initial state JSON. An f-string of this size would need every CSS and
# JS brace doubled, which is a losing trade for readability.
# ---------------------------------------------------------------------------

_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alert agent control room</title>
<style>
:root {
  --bg: #f6f6f4;
  --surface: #ffffff;
  --surface-2: #f0f0ee;
  --border: #e0e0dc;
  --text: #17181a;
  --muted: #6b6f76;
  --accent: #2b6cb0;
  --accent-weak: #dce8f5;
  --green: #2f9e56;
  --grey: #8a8f98;
  --amber: #c67c1f;
  --amber-bg: #fbeed9;
  --radius: 12px;
  --radius-sm: 10px;
  font-variant-numeric: tabular-nums;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #121314;
    --surface: #1a1b1d;
    --surface-2: #202224;
    --border: #303234;
    --text: #edeef0;
    --muted: #9a9ea6;
    --accent: #6ea8dc;
    --accent-weak: #203247;
    --green: #4cbf78;
    --grey: #8a8f98;
    --amber: #e0a445;
    --amber-bg: #3a2f18;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.45;
}
.wrap {
  max-width: 1080px;
  margin: 0 auto;
  padding: 2rem 1.25rem 4rem;
}
header.page-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 1.5rem;
}
header.page-head h1 {
  font-size: 1.35rem;
  margin: 0;
  font-weight: 650;
  letter-spacing: -0.01em;
}
header.page-head p {
  margin: 0;
  color: var(--muted);
  font-size: 0.85rem;
}
section.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem;
  margin-bottom: 1.25rem;
}
section.card h2 {
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin: 0 0 1rem;
  font-weight: 650;
}
.hint {
  color: var(--muted);
  font-size: 0.82rem;
  margin: 0.3rem 0 0;
}
/* ---- status hero ---- */
.hero-top {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 1.25rem;
}
.dot {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: var(--grey);
  transition: background 150ms ease;
  flex-shrink: 0;
}
.dot.running { background: var(--green); }
.dot.stopped { background: var(--grey); }
.dot.degraded { background: var(--amber); }
.status-word {
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: 0.01em;
}
.status-timing {
  margin-left: auto;
  text-align: right;
  color: var(--muted);
  font-size: 0.85rem;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 1rem;
}
.stat-tile {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.85rem 1rem;
  background: var(--surface-2);
}
.stat-tile .n {
  font-size: 1.6rem;
  font-weight: 700;
  display: block;
}
.stat-tile .l {
  color: var(--muted);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
/* ---- controls ---- */
.control-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.5rem;
}
@media (max-width: 700px) {
  .control-grid { grid-template-columns: 1fr; }
}
.control-block { display: flex; flex-direction: column; gap: 0.5rem; }
.btn-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
button {
  font: inherit;
  cursor: pointer;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text);
  padding: 0.5rem 0.9rem;
  transition: background 150ms ease, border-color 150ms ease, transform 150ms ease;
}
button:hover { background: var(--accent-weak); border-color: var(--accent); }
button:active { transform: translateY(1px); }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button.primary:hover { filter: brightness(1.08); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
button.preset.active { background: var(--accent); color: #fff; border-color: var(--accent); }
input[type="number"], input[type="text"] {
  font: inherit;
  font-variant-numeric: tabular-nums;
  padding: 0.45rem 0.6rem;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  width: 100%;
  max-width: 160px;
}
.inline-message {
  font-size: 0.82rem;
  min-height: 1.1em;
  color: var(--accent);
}
.inline-message.error { color: var(--amber); }
/* ---- pipeline ---- */
.funnel {
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.funnel-stage {
  flex: 1 1 140px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 1rem;
  background: var(--surface-2);
  text-align: center;
}
.funnel-stage .n { font-size: 1.9rem; font-weight: 700; display: block; }
.funnel-stage .l { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }
.funnel-arrow {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: 0.78rem;
  min-width: 60px;
  padding: 0 0.25rem;
}
.funnel-arrow .arrow-glyph { font-size: 1.1rem; color: var(--muted); }
/* ---- ticker chips & mentions ---- */
.chip-row { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 1rem; }
.chip {
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text);
  border-radius: 999px;
  padding: 0.3rem 0.75rem;
  font-size: 0.8rem;
  cursor: pointer;
  transition: background 150ms ease, border-color 150ms ease;
}
.chip:hover { border-color: var(--accent); }
.chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.mention-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.9rem 1rem;
  margin-bottom: 0.6rem;
}
.mention-card .meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
  margin-bottom: 0.4rem;
  font-size: 0.78rem;
  color: var(--muted);
}
.ticker-badge {
  background: var(--accent-weak);
  color: var(--accent);
  border-radius: 6px;
  padding: 0.1rem 0.45rem;
  font-weight: 650;
}
.mention-card .text { font-size: 0.92rem; }
.mention-card a { color: var(--accent); text-decoration: none; }
.mention-card a:hover { text-decoration: underline; }
.empty-note { color: var(--muted); font-size: 0.88rem; }
/* ---- tables ---- */
table { width: 100%; border-collapse: collapse; }
th, td {
  text-align: left;
  padding: 0.5rem 0.6rem;
  border-bottom: 1px solid var(--border);
  font-size: 0.85rem;
}
th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.04em; }
.two-col-table { display: grid; grid-template-columns: 1fr; gap: 1.5rem; }
@media (min-width: 700px) { .two-col-table { grid-template-columns: 1fr 1fr; } }
.alarm-item { color: var(--amber); background: var(--amber-bg); border-radius: var(--radius-sm); padding: 0.5rem 0.7rem; margin-bottom: 0.4rem; font-size: 0.85rem; }
/* ---- metrics bars ---- */
.metric-row { margin-bottom: 0.8rem; }
.metric-row .label-line { display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 0.25rem; }
.bar-track { background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px; height: 10px; overflow: hidden; }
.bar-fill { background: var(--accent); height: 100%; border-radius: 999px; transition: width 150ms ease; }
.metric-def { color: var(--muted); font-size: 0.78rem; margin: 0.1rem 0 0.9rem; }
.metric-arm-title { font-size: 0.85rem; font-weight: 650; margin: 1rem 0 0.5rem; }
.metric-arm-title:first-child { margin-top: 0; }
footer.foot { text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 2rem; }
</style>
</head>
<body>
<div class="wrap">

<header class="page-head">
  <h1>Alert agent control room</h1>
  <p>Local dashboard. No authentication. Not for use off localhost.</p>
</header>

<section class="card">
  <div class="hero-top">
    <span class="dot" id="status-dot"></span>
    <span class="status-word" id="status-word">-</span>
    <span class="status-timing" id="status-timing"></span>
  </div>
  <div class="stat-grid">
    <div class="stat-tile"><span class="n" id="stat-posts">-</span><span class="l">posts stored</span></div>
    <div class="stat-tile"><span class="n" id="stat-mentions">-</span><span class="l">mentions detected</span></div>
    <div class="stat-tile"><span class="n" id="stat-alerts">-</span><span class="l">alerts delivered</span></div>
    <div class="stat-tile"><span class="n" id="stat-errors">-</span><span class="l">consecutive errors</span></div>
  </div>
</section>

<section class="card">
  <h2>Controls</h2>
  <div class="control-grid">
    <div class="control-block">
      <div class="btn-row">
        <button class="primary" id="start-btn" type="button">Start</button>
        <button id="stop-btn" type="button">Stop</button>
      </div>
      <p class="hint">Start launches the crawler in the background with the interval below. Stop
      sends it a termination signal and waits briefly for it to exit.</p>
      <div class="inline-message" id="control-message"></div>

      <div class="btn-row" id="interval-buttons" style="margin-top:0.5rem;">
        <button class="preset" type="button" data-interval="60">60s Fast</button>
        <button class="preset" type="button" data-interval="90">90s Normal</button>
        <button class="preset" type="button" data-interval="180">180s Relaxed</button>
        <button class="preset" type="button" data-interval="300">300s Quiet</button>
      </div>
      <input type="number" id="interval-custom" min="10" step="1" value="90">
      <p class="hint" id="interval-guidance">Poll interval controls how often the agent checks for
      new posts. Shorter means faster alerts and more requests.</p>
    </div>

    <div class="control-block">
      <div class="btn-row" id="backfill-buttons">
        <button class="preset" type="button" data-days="7">7 days</button>
        <button class="preset" type="button" data-days="30">30 days</button>
        <button class="preset" type="button" data-days="45">45 days</button>
        <button class="preset" type="button" data-days="90">90 days</button>
      </div>
      <input type="number" id="backfill-custom" min="1" step="1" value="45">
      <button id="backfill-run-btn" type="button">Run backfill</button>
      <p class="hint" id="backfill-guidance">Backfill walks the public archive for the chosen
      window and fills in older posts. It runs in the background and does not block this page.</p>
      <div class="inline-message" id="backfill-message"></div>
    </div>
  </div>
</section>

<section class="card">
  <h2>Pipeline</h2>
  <p class="hint">Where posts go, in order. The drop between two stages is the count that did not
  make it to the next one.</p>
  <div class="funnel" id="funnel"></div>
</section>

<section class="card">
  <h2>Mentions</h2>
  <p class="hint">Newest first. Pick a ticker to narrow the list, or All to see everything.</p>
  <div class="chip-row" id="ticker-chips"></div>
  <div id="mentions-list"></div>
</section>

<section class="card">
  <h2>Health and latency</h2>
  <div class="two-col-table">
    <div>
      <table>
        <tr><th>last successful poll</th><td id="health-last-success">-</td></tr>
        <tr><th>last new post</th><td id="health-last-new-post">-</td></tr>
        <tr><th>consecutive errors</th><td id="health-errors">-</td></tr>
      </table>
      <p class="hint" style="margin-top:0.75rem;">Active alarms, if any. These clear on their own
      once the underlying condition does.</p>
      <div id="alarms-list"></div>
    </div>
    <div>
      <table>
        <thead><tr><th>stage</th><th>count</th><th>p50</th><th>p95</th><th>max</th></tr></thead>
        <tbody id="latency-body"></tbody>
      </table>
      <p class="hint" style="margin-top:0.5rem;">Seconds from one pipeline stamp to the next, across
      every post that has both.</p>
    </div>
  </div>
</section>

<section class="card">
  <h2>Detection metrics</h2>
  <p class="hint">From the last evaluation run. Absent until scripts/evaluate.py has produced
  data/eval/metrics.md.</p>
  <div id="metrics-container"></div>
</section>

<section class="card">
  <h2>Ticker lexicon</h2>
  <p class="hint">The ticker/company/alias table the detector matches against. First line must stay
  exactly the header row shown below the box.</p>
  <p class="hint">ticker,company,aliases,ambiguity,ambiguous_aliases,kind,notes</p>
  <textarea id="lexicon-text" rows="8" style="width:100%; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82rem; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--surface); color: var(--text); padding: 0.6rem;"></textarea>
  <div class="btn-row" style="margin-top:0.5rem;">
    <button id="lexicon-save-btn" type="button">Save lexicon</button>
  </div>
  <div class="inline-message" id="lexicon-message"></div>
</section>

<footer class="foot">Refreshes every 10 seconds. Local process only.</footer>
</div>

<script id="initial-state" type="application/json">__INITIAL_STATE__</script>
<script>
(function () {
  "use strict";

  var state = null;
  var currentTicker = null;

  function qs(id) { return document.getElementById(id); }

  function fmtAgo(iso) {
    if (!iso) return "never";
    var then = new Date(iso).getTime();
    var diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) return Math.floor(diff) + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return Math.floor(diff / 86400) + "d ago";
  }

  function fmtCountdown(iso) {
    if (!iso) return null;
    var target = new Date(iso).getTime();
    var diff = Math.round((target - Date.now()) / 1000);
    if (diff <= 0) return "any moment";
    if (diff < 60) return diff + "s";
    var minutes = Math.floor(diff / 60);
    var seconds = diff % 60;
    return minutes + "m " + (seconds < 10 ? "0" : "") + seconds + "s";
  }

  function describeInterval(seconds) {
    seconds = Math.max(10, seconds);
    var perDay = Math.round((86400 / seconds) / 10) * 10;
    var perDayStr = perDay.toLocaleString("en-US");
    var wait;
    if (seconds <= 60) {
      wait = seconds === 60 ? "alerts land within a minute" : "alerts land within " + seconds + " seconds";
    } else {
      var minutes = Math.round(seconds / 60);
      var words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"};
      var word = words[minutes] || String(minutes);
      wait = "an alert can wait up to " + word + " minute" + (minutes === 1 ? "" : "s");
    }
    return "about " + perDayStr + " requests a day, " + wait + ".";
  }

  function describeBackfill(days) {
    days = Math.max(1, days);
    var posts = days * 28;
    var pages = Math.max(1, Math.round(posts / 20));
    var seconds = pages * 2.5;
    var timeText;
    if (seconds < 30) {
      timeText = "roughly " + Math.round(seconds) + " seconds";
    } else if (seconds < 60) {
      timeText = "roughly a minute";
    } else {
      var minutes = Math.round(seconds / 60);
      timeText = "roughly " + minutes + " minute" + (minutes === 1 ? "" : "s");
    }
    return "about " + pages + " pages, " + timeText + ".";
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function setStatus(s) {
    var dot = qs("status-dot");
    var word = qs("status-word");
    dot.className = "dot " + s.status.toLowerCase();
    word.textContent = s.status;

    var timing = "last poll " + fmtAgo(s.last_poll_at);
    if (s.status === "RUNNING" && s.next_poll_at) {
      var countdown = fmtCountdown(s.next_poll_at);
      if (countdown) timing += " . next poll in " + countdown;
    }
    qs("status-timing").textContent = timing;
  }

  function setStats(s) {
    qs("stat-posts").textContent = s.stats.posts;
    qs("stat-mentions").textContent = s.stats.stock_related;
    qs("stat-alerts").textContent = s.stats.alerts_delivered;
    qs("stat-errors").textContent = s.consecutive_errors;
  }

  function setFunnel(p) {
    var stages = [
      ["fetched", p.fetched],
      ["has text", p.has_text],
      ["candidate", p.candidate],
      ["alerted", p.alerted]
    ];
    var html = "";
    for (var i = 0; i < stages.length; i++) {
      html += '<div class="funnel-stage"><span class="n">' + stages[i][1] +
        '</span><span class="l">' + stages[i][0] + "</span></div>";
      if (i < stages.length - 1) {
        var drop = stages[i][1] - stages[i + 1][1];
        html += '<div class="funnel-arrow"><span class="arrow-glyph">-&gt;</span><span>-' + drop + "</span></div>";
      }
    }
    qs("funnel").innerHTML = html;
  }

  function setChips(s) {
    var html = '<button type="button" class="chip' + (currentTicker === null ? " active" : "") +
      '" data-ticker="">All</button>';
    for (var i = 0; i < s.tickers.length; i++) {
      var t = s.tickers[i];
      html += '<button type="button" class="chip' + (currentTicker === t ? " active" : "") +
        '" data-ticker="' + escapeHtml(t) + '">' + escapeHtml(t) + "</button>";
    }
    qs("ticker-chips").innerHTML = html;
    var chips = qs("ticker-chips").querySelectorAll(".chip");
    for (var j = 0; j < chips.length; j++) {
      chips[j].addEventListener("click", function () {
        currentTicker = this.getAttribute("data-ticker") || null;
        refresh();
      });
    }
  }

  function setMentions(s) {
    if (!s.mentions.length) {
      qs("mentions-list").innerHTML = '<p class="empty-note">No mentions yet.</p>';
      return;
    }
    var html = "";
    for (var i = 0; i < s.mentions.length; i++) {
      var m = s.mentions[i];
      var badges = m.tickers.map(function (t) { return '<span class="ticker-badge">' + escapeHtml(t) + "</span>"; }).join(" ");
      html += '<div class="mention-card"><div class="meta">' + badges +
        "<span>" + escapeHtml(m.companies.join(", ")) + "</span>" +
        "<span>" + fmtAgo(m.created_at) + '</span></div><div class="text">' + escapeHtml(m.text) +
        ' <a href="' + escapeHtml(m.url) + '" target="_blank" rel="noopener">open post</a></div></div>';
    }
    qs("mentions-list").innerHTML = html;
  }

  function setHealth(s) {
    qs("health-last-success").textContent = s.last_successful_poll_at ? fmtAgo(s.last_successful_poll_at) : "never";
    qs("health-last-new-post").textContent = s.last_new_post_at ? fmtAgo(s.last_new_post_at) : "never";
    qs("health-errors").textContent = s.consecutive_errors;
    if (!s.alarms.length) {
      qs("alarms-list").innerHTML = '<p class="empty-note">none</p>';
    } else {
      qs("alarms-list").innerHTML = s.alarms.map(function (a) {
        return '<div class="alarm-item">' + escapeHtml(a) + "</div>";
      }).join("");
    }
  }

  function setLatency(s) {
    var rows = "";
    for (var i = 0; i < s.latency.length; i++) {
      var r = s.latency[i];
      rows += "<tr><td>" + escapeHtml(r.stage) + "</td><td>" + r.count + "</td><td>" +
        r.p50.toFixed(1) + "</td><td>" + r.p95.toFixed(1) + "</td><td>" + r.max.toFixed(1) + "</td></tr>";
    }
    qs("latency-body").innerHTML = rows;
  }

  function metricBar(label, def, value) {
    var pct = Math.round(value * 1000) / 10;
    return '<div class="metric-row"><div class="label-line"><span>' + label + "</span><span>" +
      pct.toFixed(1) + '%</span></div><div class="bar-track"><div class="bar-fill" style="width:' +
      pct + '%"></div></div><p class="metric-def">' + def + "</p></div>";
  }

  function setMetrics(s) {
    if (!s.metrics) {
      qs("metrics-container").innerHTML =
        '<p class="empty-note">No evaluation run yet. Run scripts/evaluate.py to populate this section.</p>';
      return;
    }
    var defs = {
      precision: "Of everything flagged as a mention, the share that was actually right.",
      recall: "Of every real mention that existed, the share the system actually caught.",
      f1: "One balance of precision and recall, low if either one is weak."
    };
    var html = "";
    for (var i = 0; i < s.metrics.length; i++) {
      var m = s.metrics[i];
      html += '<div class="metric-arm-title">' + escapeHtml(m.arm) + " arm</div>";
      html += metricBar("Precision", defs.precision, m.precision);
      html += metricBar("Recall", defs.recall, m.recall);
      html += metricBar("F1", defs.f1, m.f1);
    }
    qs("metrics-container").innerHTML = html;
  }

  function render(s) {
    state = s;
    setStatus(s);
    setStats(s);
    setFunnel(s.pipeline);
    setChips(s);
    setMentions(s);
    setHealth(s);
    setLatency(s);
    setMetrics(s);

    var startBtn = qs("start-btn");
    var stopBtn = qs("stop-btn");
    startBtn.disabled = s.status !== "STOPPED";
    stopBtn.disabled = s.status === "STOPPED";

    if (document.activeElement !== qs("interval-custom")) {
      qs("interval-custom").value = s.poll_interval_seconds;
    }
    if (document.activeElement !== qs("backfill-custom")) {
      qs("backfill-custom").value = s.backfill_days;
    }
    updateIntervalGuidance();
    updateBackfillGuidance();
    markActivePreset("interval-buttons", "data-interval", s.poll_interval_seconds);
    markActivePreset("backfill-buttons", "data-days", s.backfill_days);

    if (document.activeElement !== qs("lexicon-text")) {
      // Lexicon text is loaded once via a dedicated small endpoint below.
    }
  }

  function markActivePreset(containerId, attr, value) {
    var buttons = qs(containerId).querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      var v = buttons[i].getAttribute(attr);
      buttons[i].classList.toggle("active", v !== null && parseInt(v, 10) === parseInt(value, 10));
    }
  }

  function updateIntervalGuidance() {
    var v = parseInt(qs("interval-custom").value, 10) || 90;
    qs("interval-guidance").textContent = "Poll interval controls how often the agent checks for new posts. " + describeInterval(v);
  }

  function updateBackfillGuidance() {
    var v = parseInt(qs("backfill-custom").value, 10) || 45;
    qs("backfill-guidance").textContent = "Backfill walks the public archive for the chosen window. " + describeBackfill(v);
  }

  function postForm(url, data) {
    var body = Object.keys(data).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(data[k]);
    }).join("&");
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body
    }).then(function (r) { return r.json(); });
  }

  function refresh() {
    var url = "/api/state";
    if (currentTicker) url += "?ticker=" + encodeURIComponent(currentTicker);
    return fetch(url).then(function (r) { return r.json(); }).then(render);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var initial = JSON.parse(qs("initial-state").textContent);
    currentTicker = initial.ticker_filter || null;
    render(initial);

    var intervalButtons = qs("interval-buttons").querySelectorAll("button");
    for (var i = 0; i < intervalButtons.length; i++) {
      intervalButtons[i].addEventListener("click", function () {
        var v = this.getAttribute("data-interval");
        qs("interval-custom").value = v;
        updateIntervalGuidance();
        postForm("/settings", { interval: v });
        markActivePreset("interval-buttons", "data-interval", v);
      });
    }
    qs("interval-custom").addEventListener("input", updateIntervalGuidance);
    qs("interval-custom").addEventListener("change", function () {
      postForm("/settings", { interval: qs("interval-custom").value });
    });

    var backfillButtons = qs("backfill-buttons").querySelectorAll("button");
    for (var j = 0; j < backfillButtons.length; j++) {
      backfillButtons[j].addEventListener("click", function () {
        var v = this.getAttribute("data-days");
        qs("backfill-custom").value = v;
        updateBackfillGuidance();
        markActivePreset("backfill-buttons", "data-days", v);
      });
    }
    qs("backfill-custom").addEventListener("input", updateBackfillGuidance);

    qs("start-btn").addEventListener("click", function () {
      postForm("/start", { interval: qs("interval-custom").value }).then(function (res) {
        var msg = qs("control-message");
        msg.textContent = res.message || "";
        msg.className = "inline-message" + (res.ok ? "" : " error");
        refresh();
      });
    });

    qs("stop-btn").addEventListener("click", function () {
      postForm("/stop", {}).then(function (res) {
        var msg = qs("control-message");
        msg.textContent = res.message || "";
        msg.className = "inline-message" + (res.ok ? "" : " error");
        refresh();
      });
    });

    qs("backfill-run-btn").addEventListener("click", function () {
      var days = qs("backfill-custom").value;
      postForm("/backfill", { days: days }).then(function (res) {
        var msg = qs("backfill-message");
        msg.textContent = res.message || "";
        msg.className = "inline-message" + (res.ok ? "" : " error");
        refresh();
      });
    });

    fetch("/lexicon").then(function (r) { return r.json(); }).then(function (res) {
      qs("lexicon-text").value = res.text || "";
    });

    qs("lexicon-save-btn").addEventListener("click", function () {
      postForm("/lexicon", { csv: qs("lexicon-text").value }).then(function (res) {
        var msg = qs("lexicon-message");
        msg.textContent = res.message || "";
        msg.className = "inline-message" + (res.ok ? "" : " error");
      });
    });

    setInterval(refresh, 10000);
    setInterval(function () { if (state) setStatus(state); }, 1000);
  });
})();
</script>
</body>
</html>
"""


def render_page(state: dict) -> str:
    return _PAGE_TEMPLATE.replace("__INITIAL_STATE__", _json_for_script(state))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _parse_int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _get_setting_int(db_path: str, key: str, default: int) -> int:
    with Store(db_path) as store:
        return _int_or(store.get_state(key), default)


def _save_setting(db_path: str, key: str, value: str) -> None:
    with Store(db_path) as store:
        store.set_state(key, value)


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: str = _DEFAULT_DB
    lexicon_path: Path = _DEFAULT_LEXICON
    pid_path: Path = _DEFAULT_PID
    metrics_path: Path = _DEFAULT_METRICS
    # Injected in tests so nothing here ever spawns the real agent.
    spawn_fn = staticmethod(subprocess.Popen)
    server_version = "TSAlertDashboard/2.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(render_page(self._build_state(parsed)))
        elif parsed.path == "/api/state":
            self._send_json(200, self._build_state(parsed))
        elif parsed.path == "/lexicon":
            text = self.lexicon_path.read_text(encoding="utf-8") if self.lexicon_path.exists() else ""
            self._send_json(200, {"text": text})
        else:
            self._send_text(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        handlers = {
            "/start": self._handle_start,
            "/stop": self._handle_stop,
            "/backfill": self._handle_backfill,
            "/settings": self._handle_settings,
            "/lexicon": self._handle_lexicon,
        }
        handler = handlers.get(parsed.path)
        if handler is None:
            self._send_text(404, "not found")
            return
        handler()

    def _build_state(self, parsed) -> dict:
        query = parse_qs(parsed.query)
        ticker = (query.get("ticker", [""])[0] or "").strip().upper() or None
        return build_state(self.db_path, self.pid_path, self.metrics_path, ticker)

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(body)
        return {k: v[0] for k, v in parsed.items()}

    def _handle_start(self) -> None:
        form = self._read_form()
        running, pid = process_status(self.pid_path)
        if running:
            self._send_json(200, {"ok": False, "message": f"already running (pid {pid})"})
            return

        default_interval = _get_setting_int(self.db_path, "poll_interval_seconds", _DEFAULT_INTERVAL_SECONDS)
        interval = max(10, _parse_int(form.get("interval"), default_interval))
        _save_setting(self.db_path, "poll_interval_seconds", str(interval))

        env = dict(os.environ)
        env["POLL_INTERVAL_SECONDS"] = str(interval)
        # Without this the agent writes to whatever DB_PATH the environment
        # happens to hold, and the page ends up describing a different
        # database from the process it just started.
        env["DB_PATH"] = str(self.db_path)
        proc = self.spawn_fn(
            [sys.executable, str(_AGENT_SCRIPT), "run"],
            cwd=str(_REPO_ROOT),
            env=env,
        )
        write_pid(self.pid_path, proc.pid)
        self._send_json(
            200,
            {"ok": True, "message": f"started (pid {proc.pid})", "poll_interval_seconds": interval},
        )

    def _handle_stop(self) -> None:
        running, pid = process_status(self.pid_path)
        if not running or pid is None:
            clear_pid(self.pid_path)
            self._send_json(200, {"ok": False, "message": "not running"})
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + _STOP_WAIT_SECONDS
        while time.monotonic() < deadline and is_running(pid):
            time.sleep(0.05)
        clear_pid(self.pid_path)
        self._send_json(200, {"ok": True, "message": f"stopped (pid {pid})"})

    def _handle_backfill(self) -> None:
        form = self._read_form()
        default_days = _get_setting_int(self.db_path, "backfill_days", _DEFAULT_BACKFILL_DAYS)
        days = max(1, _parse_int(form.get("days"), default_days))
        _save_setting(self.db_path, "backfill_days", str(days))
        # Fire and forget: the request must not block on a job that can run
        # for minutes and talks to the live network.
        # Write to a separate file. The default output is data/history.jsonl,
        # which is the frozen corpus the evaluation set is built from, and a
        # backfill appends to it.
        out_path = _REPO_ROOT / "data" / "backfill_latest.jsonl"
        self.spawn_fn(
            [sys.executable, str(_BACKFILL_SCRIPT), "--days", str(days),
             "--db", str(self.db_path), "--out", str(out_path)],
            cwd=str(_REPO_ROOT),
        )
        self._send_json(200, {"ok": True, "message": f"backfill started for {days} day(s)", "backfill_days": days})

    def _handle_settings(self) -> None:
        form = self._read_form()
        result: dict[str, object] = {"ok": True}
        if "interval" in form:
            stored = _get_setting_int(self.db_path, "poll_interval_seconds", _DEFAULT_INTERVAL_SECONDS)
            interval = max(10, _parse_int(form.get("interval"), stored))
            _save_setting(self.db_path, "poll_interval_seconds", str(interval))
            result["poll_interval_seconds"] = interval
        if "backfill_days" in form:
            stored_days = _get_setting_int(self.db_path, "backfill_days", _DEFAULT_BACKFILL_DAYS)
            days = max(1, _parse_int(form.get("backfill_days"), stored_days))
            _save_setting(self.db_path, "backfill_days", str(days))
            result["backfill_days"] = days
        self._send_json(200, result)

    def _handle_lexicon(self) -> None:
        form = self._read_form()
        text = form.get("csv", "")
        error = validate_lexicon_csv(text)
        if error is not None:
            self._send_json(200, {"ok": False, "message": f"Not saved: {error}"})
            return
        self.lexicon_path.write_text(text, encoding="utf-8")
        self._send_json(200, {"ok": True, "message": "Saved."})

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status: int, data: object) -> None:
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_text(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Quiet by default. This is a local prototype, not a service worth
        # a request log.
        pass


def make_server(
    host: str,
    port: int,
    db_path: str,
    lexicon_path: Path,
    pid_path: Path = _DEFAULT_PID,
    metrics_path: Path = _DEFAULT_METRICS,
    spawn_fn=None,
) -> HTTPServer:
    # A per-server subclass instead of module globals, so more than one
    # server (as in tests) can point at different databases, pid files and
    # spawn functions at once.
    attrs = {
        "db_path": db_path,
        "lexicon_path": Path(lexicon_path),
        "pid_path": Path(pid_path),
        "metrics_path": Path(metrics_path),
    }
    if spawn_fn is not None:
        attrs["spawn_fn"] = staticmethod(spawn_fn)
    bound_handler = type("BoundDashboardHandler", (DashboardHandler,), attrs)
    return HTTPServer((host, port), bound_handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local control room dashboard for the alert agent")
    parser.add_argument("--host", default="127.0.0.1", help="bind address, local only by default")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=_DEFAULT_DB, help="path to the sqlite db")
    parser.add_argument("--lexicon", default=str(_DEFAULT_LEXICON), help="path to tickers.csv")
    parser.add_argument("--pid", default=str(_DEFAULT_PID), help="path to the agent pid file")
    parser.add_argument("--metrics", default=str(_DEFAULT_METRICS), help="path to data/eval/metrics.md")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = make_server(
        args.host,
        args.port,
        args.db,
        Path(args.lexicon),
        Path(args.pid),
        Path(args.metrics),
    )
    print(f"Dashboard serving on http://{args.host}:{args.port} (local only, no auth)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
