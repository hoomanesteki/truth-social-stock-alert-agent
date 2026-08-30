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
from tsalert.config import Config  # noqa: E402
from tsalert.models import parse_iso_datetime  # noqa: E402
from tsalert.monitor import HealthMonitor  # noqa: E402
from tsalert.sources.rss_mirror import TrumpsTruthRssSource  # noqa: E402
from tsalert.sources.truthsocial import TruthSocialApiSource  # noqa: E402
from tsalert.store import Store  # noqa: E402

_DEFAULT_DB = "data/agent.db"
_DEFAULT_LEXICON = _REPO_ROOT / "data" / "lexicon" / "tickers.csv"
_DEFAULT_PID = _REPO_ROOT / "data" / "agent.pid"
_DEFAULT_BACKFILL_PID = _REPO_ROOT / "data" / "backfill.pid"
_DEFAULT_METRICS = _REPO_ROOT / "data" / "eval" / "metrics.md"
_AGENT_SCRIPT = _REPO_ROOT / "agent.py"
_BACKFILL_SCRIPT = _REPO_ROOT / "scripts" / "backfill.py"
_LEXICON_HEADER = ["ticker", "company", "aliases", "ambiguity", "ambiguous_aliases", "kind", "notes"]
_TEXT_PREVIEW_CHARS = 200
_DEFAULT_INTERVAL_SECONDS = 90
# Both ends of the poll interval, enforced on the server. The number input's
# min attribute is a client hint and nothing more. Without an upper bound a
# pasted number reached timedelta() in build_state and time.sleep() in the
# agent, and both raise OverflowError past their limits: the page died with
# no response at all, the bad value was already persisted so restarting the
# dashboard did not help, and Stop was unreachable because the page was gone.
_MIN_INTERVAL_SECONDS = 10
_MAX_INTERVAL_SECONDS = 86400  # a day. Anything slower is not monitoring.


def _clamp_interval(value: object, default: int) -> int:
    return max(_MIN_INTERVAL_SECONDS, min(_MAX_INTERVAL_SECONDS, _parse_int(value, default)))
_MAX_BACKFILL_DAYS = 3650  # ten years, well past anything the archive holds
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
    # Only a real, individually addressable process id. 0 means "every
    # process in my own group" and -1 means "every process I am allowed to
    # signal", and both of those answer os.kill(pid, 0) with success, so a
    # pid file holding 0 made the page report RUNNING and then made Stop
    # SIGTERM the dashboard itself. Negative values below -1 are process
    # groups, which is not what this file ever holds.
    if pid <= 0:
        return False
    # Reap first. An agent we spawned that has already exited stays a zombie
    # until someone waits on it, and os.kill succeeds on a zombie, so without
    # this the page reports RUNNING for a process that is dead and also
    # refuses to start a new one.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError, OverflowError):
        # OverflowError: a pid too large for the platform's pid_t. read_pid
        # checks the format but not the range, and an unhandled exception
        # here takes the whole page down rather than one control.
        pass

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
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


def looks_like_our_agent(pid: int) -> bool:
    """Is this pid actually the agent, or has the number been reused?

    A pid file outlives the process it names, and pids get reused. Without
    this check, Stop sent SIGTERM to whatever now holds that number: I put a
    live unrelated process's pid in the file and the page cheerfully killed
    it, reporting success.

    Reads the command line rather than trusting the file. Anything that
    cannot be determined is treated as "not ours", because refusing to stop
    is recoverable and killing a stranger is not.
    """
    if pid <= 0:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    return _AGENT_SCRIPT.name in out.stdout


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


# Which channels exist, and what each one needs before it can send. Kept
# beside the page rather than imported from agent.build_channels because the
# page has to describe a channel that is switched off, and build_channels
# deliberately leaves those out of its list.
_CHANNEL_SPECS = [
    ("file", "always on", "Append only JSONL on local disk. Cannot fail, needs nothing."),
    ("console", "always on", "Prints to the agent's stdout."),
    ("discord", "DISCORD_WEBHOOK_URL", "Primary remote channel. One webhook URL is the whole credential."),
    ("telegram", "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID", "Optional second remote channel."),
]


def channel_health(db_path: str) -> list[dict]:
    """One row per channel: is it set up, is it paused, and how is it doing.

    stats() answers "are alerts going out" in aggregate, which is the wrong
    question during an outage. The useful question is which channel is
    failing, because one channel at zero with a queue behind it and every
    channel failing at once need different responses.
    """
    try:
        # Reading .env from the repo root, not the process CWD. load_dotenv
        # resolves a bare ".env" relative to wherever the dashboard was
        # started, so launched from anywhere else the panel reported live
        # channels as "not set up" while the agent, which is spawned with the
        # repo root as its cwd, was happily delivering.
        config = Config.from_env(str(_REPO_ROOT / ".env"))
    except Exception as exc:
        # One malformed setting used to raise out of build_state, and
        # BaseHTTPRequestHandler answers an unhandled exception by sending
        # nothing at all. The page, the JSON and every control died together
        # over a value that only this panel needed.
        logger_message = f"config unreadable: {exc}"
        return [{
            "name": name, "needs": needs, "description": description,
            "configured": False, "paused": False, "delivered": 0, "queued": 0,
            "failed": 0, "given_up": 0, "last_error": logger_message,
        } for name, needs, description in _CHANNEL_SPECS]
    configured = {
        "file": True,
        "console": True,
        "discord": bool(config.discord_webhook_url),
        "telegram": bool(config.telegram_bot_token and config.telegram_chat_id),
    }
    with Store(db_path) as store:
        counts = store.channel_stats()
        paused = {name: store.is_channel_paused(name) for name, _, _ in _CHANNEL_SPECS}

    rows = []
    for name, needs, description in _CHANNEL_SPECS:
        seen = counts.get(name, {})
        rows.append({
            "name": name,
            "needs": needs,
            "description": description,
            "configured": configured[name],
            "paused": paused[name],
            "delivered": seen.get("delivered", 0),
            "queued": seen.get("queued", 0),
            "failed": seen.get("failed", 0),
            "given_up": seen.get("given_up", 0),
            "last_error": seen.get("last_error"),
        })
    return rows


def ingestion_state(store: Store) -> dict:
    """Which source is live, and whether it has ever fallen back.

    Written by the running agent each poll. When the agent has never run
    these are all empty, which the page renders as "not polling yet" rather
    than inventing a healthy looking default.
    """
    raw = store.get_state("last_source_transition")
    transition = None
    if raw:
        try:
            transition = json.loads(raw)
        except json.JSONDecodeError:
            transition = None
    active = store.get_state("active_source") or ""
    detail = store.get_state("source_detail") or ""

    # The roster is built here, from the source classes' own name attributes,
    # and handed to the page ready to render. The first version hard coded
    # "truthsocial" and "rss" in the JavaScript while the classes actually
    # call themselves truthsocial_api and trumpstruth_rss, so no card ever
    # matched: nothing showed as live, the live error text was unreachable,
    # and a real mirror run rendered as "Replay". Naming things in two places
    # is what made that possible, so now there is one place.
    sources = [
        {
            "key": TruthSocialApiSource.name,
            "role": "Primary",
            "who": "Truth Social JSON",
            "note": "Mastodon endpoint behind Cloudflare. Fast, and the fragile one.",
        },
        {
            "key": TrumpsTruthRssSource.name,
            "role": "Fallback",
            "who": "trumpstruth.org RSS",
            "note": "Takes over once the breaker opens on the primary.",
        },
    ]
    known = {src["key"] for src in sources}
    if active and active not in known:
        # A replay run (--source demo or fixture) is neither of the two.
        sources.insert(0, {
            "key": active,
            "role": "Replay",
            "who": active,
            "note": "recorded posts, no network",
        })
    for src in sources:
        src["active"] = src["key"] == active

    return {
        "active": active,
        "detail": detail,
        "ok": store.get_state("source_ok") == "1",
        "last_transition": transition,
        "sources": sources,
    }


def build_state(db_path: str, pid_path: Path, metrics_path: Path, ticker_filter: str | None) -> dict:
    with Store(db_path) as store:
        stats = store.stats()
        monitor = HealthMonitor(store)
        health = monitor.status()
        alarms = active_alarms(monitor)
        # Clamped on read as well as on write, so a value stored by an older
        # build cannot keep the page dead.
        interval = max(_MIN_INTERVAL_SECONDS, min(
            _MAX_INTERVAL_SECONDS,
            _int_or(store.get_state("poll_interval_seconds"), _DEFAULT_INTERVAL_SECONDS),
        ))
        ingestion = ingestion_state(store)
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
        "channels": channel_health(db_path),
        "ingestion": ingestion,
        "latency": latency_table(db_path),
        "metrics": read_metrics(metrics_path),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


def validate_lexicon_csv(text: str) -> str | None:
    """Return an error message, or None when the table is safe to save.

    Checking the header alone was not enough. A correct header with no rows
    under it saved cleanly, reported "Saved.", and left the detector with an
    empty lexicon: every alias and bare ticker match gone, only cashtags
    still firing, and nothing said so until posts stopped being detected. The
    row checks below are the difference between rejecting a paste that went
    wrong and silently disarming detection.
    """
    lines = text.splitlines()
    if not lines:
        return "empty file"
    try:
        rows = list(csv.reader(lines))
    except csv.Error as exc:
        return f"could not be read as CSV: {exc}"
    header = rows[0]
    if header != _LEXICON_HEADER:
        return "header row must be exactly: " + ",".join(_LEXICON_HEADER)

    body = [row for row in rows[1:] if row and any(cell.strip() for cell in row)]
    if not body:
        return "no rows under the header, which would leave the detector with nothing to match"
    seen = set()
    for number, row in enumerate(body, start=2):
        if len(row) != len(_LEXICON_HEADER):
            return f"line {number} has {len(row)} columns, expected {len(_LEXICON_HEADER)}"
        ticker = row[0].strip().upper()
        if not ticker:
            return f"line {number} has no ticker"
        if ticker in seen:
            return f"line {number} repeats ticker {ticker}"
        seen.add(ticker)
        if not row[1].strip():
            return f"line {number} ({ticker}) has no company name"
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
  /* Apple's system greys, which are tuned to sit calmly behind content
     rather than compete with it. The page is mostly numbers, so the palette
     stays neutral and colour is reserved for state. */
  --bg: #f5f5f7;
  --surface: rgba(255, 255, 255, 0.82);
  --surface-solid: #ffffff;
  --surface-2: #f0f0f3;
  --border: rgba(0, 0, 0, 0.08);
  --border-strong: rgba(0, 0, 0, 0.14);
  --text: #1d1d1f;
  --muted: #6e6e73;
  --faint: #a1a1a6;
  --accent: #0071e3;
  --accent-hover: #0077ed;
  --accent-weak: rgba(0, 113, 227, 0.1);
  --green: #34c759;
  --green-weak: rgba(52, 199, 89, 0.14);
  --amber: #ff9f0a;
  --amber-weak: rgba(255, 159, 10, 0.14);
  --red: #ff3b30;
  --red-weak: rgba(255, 59, 48, 0.12);
  --grey: #8e8e93;
  --amber-bg: rgba(255, 159, 10, 0.12);
  --radius: 18px;
  --radius-sm: 12px;
  --radius-xs: 8px;
  --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.06);
  --shadow-lift: 0 2px 6px rgba(0,0,0,0.06), 0 16px 40px rgba(0,0,0,0.10);
  --ease: cubic-bezier(0.4, 0, 0.2, 1);
  font-variant-numeric: tabular-nums;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #000000;
    --surface: rgba(28, 28, 30, 0.82);
    --surface-solid: #1c1c1e;
    --surface-2: #2c2c2e;
    --border: rgba(255, 255, 255, 0.1);
    --border-strong: rgba(255, 255, 255, 0.18);
    --text: #f5f5f7;
    --muted: #98989d;
    --faint: #636366;
    --accent: #0a84ff;
    --accent-hover: #409cff;
    --accent-weak: rgba(10, 132, 255, 0.18);
    --green: #30d158;
    --green-weak: rgba(48, 209, 88, 0.18);
    --amber: #ff9f0a;
    --amber-weak: rgba(255, 159, 10, 0.18);
    --red: #ff453a;
    --red-weak: rgba(255, 69, 58, 0.18);
    --amber-bg: rgba(255, 159, 10, 0.16);
    --shadow: 0 1px 2px rgba(0,0,0,0.5), 0 8px 24px rgba(0,0,0,0.4);
    --shadow-lift: 0 2px 6px rgba(0,0,0,0.6), 0 16px 40px rgba(0,0,0,0.5);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  /* SF on Apple platforms, the closest system face elsewhere. Never a web
     font: the page has to render instantly on localhost with no network. */
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
  line-height: 1.47;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  letter-spacing: -0.011em;
}
.wrap {
  max-width: 1120px;
  margin: 0 auto;
  padding: 3rem 1.5rem 5rem;
}
header.page-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 2rem;
}
header.page-head h1 {
  font-size: 2rem;
  margin: 0;
  font-weight: 700;
  letter-spacing: -0.028em;
}
header.page-head p {
  margin: 0;
  color: var(--muted);
  font-size: 0.875rem;
}
section.card {
  background: var(--surface);
  /* Translucency over the page background, the way system panels sit on the
     desktop. Falls back to a flat surface where backdrop-filter is missing. */
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.75rem;
  margin-bottom: 1.25rem;
  box-shadow: var(--shadow);
}
@supports not (backdrop-filter: blur(1px)) {
  section.card { background: var(--surface-solid); }
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
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--grey);
  transition: background 200ms var(--ease), box-shadow 200ms var(--ease);
  flex-shrink: 0;
  position: relative;
}
.dot.running {
  background: var(--green);
  box-shadow: 0 0 0 4px var(--green-weak);
}
/* A slow pulse only while it is actually polling. Motion here means the
   process is alive, so it must not run when the agent is stopped. */
.dot.running::after {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: var(--green);
  animation: pulse 2.2s var(--ease) infinite;
}
@keyframes pulse {
  0%   { transform: scale(1);   opacity: 0.7; }
  70%  { transform: scale(2.6); opacity: 0; }
  100% { transform: scale(2.6); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .dot.running::after { animation: none; }
}
.dot.stopped { background: var(--grey); }
.dot.degraded {
  background: var(--amber);
  box-shadow: 0 0 0 4px var(--amber-weak);
}
.status-word {
  font-size: 1.75rem;
  font-weight: 700;
  letter-spacing: -0.02em;
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
  padding: 1rem 1.1rem;
  background: var(--surface-2);
  transition: transform 200ms var(--ease), box-shadow 200ms var(--ease);
}
.stat-tile:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow);
}
.stat-tile .n {
  font-size: 2rem;
  font-weight: 600;
  display: block;
  letter-spacing: -0.03em;
  line-height: 1.1;
}
.stat-tile .l {
  color: var(--muted);
  font-size: 0.75rem;
  letter-spacing: -0.005em;
}
section.card h2 {
  font-size: 0.75rem;
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
  font-size: 0.9rem;
  font-weight: 500;
  cursor: pointer;
  border-radius: 980px;
  border: 1px solid var(--border-strong);
  background: var(--surface-solid);
  color: var(--text);
  padding: 0.5rem 1.1rem;
  transition: background 200ms var(--ease), border-color 200ms var(--ease),
              transform 120ms var(--ease), opacity 200ms var(--ease);
}
button:hover:not(:disabled) { background: var(--surface-2); border-color: var(--accent); }
button:active:not(:disabled) { transform: scale(0.97); }
button:focus-visible { outline: 3px solid var(--accent-weak); outline-offset: 2px; }
button.primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
  font-weight: 600;
}
button.primary:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
button.danger { color: var(--red); border-color: var(--red-weak); }
button.danger:hover:not(:disabled) { background: var(--red-weak); border-color: var(--red); }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button.preset.active { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; }
.source-row { display: flex; gap: 0.75rem; flex-wrap: wrap; margin-top: 1rem; }
.source-card {
  flex: 1 1 240px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 1rem 1.1rem;
  background: var(--surface-2);
  transition: border-color 200ms var(--ease), box-shadow 200ms var(--ease);
}
.source-card.active { border-color: var(--green); box-shadow: 0 0 0 3px var(--green-weak); }
.source-card .role { font-size: 0.72rem; color: var(--muted); letter-spacing: 0.02em; }
.source-card .who { font-size: 1.05rem; font-weight: 600; margin-top: 0.15rem; letter-spacing: -0.015em; }
.source-card .detail { font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }
.transition {
  margin-top: 0.9rem;
  padding: 0.6rem 0.85rem;
  border-radius: var(--radius-xs);
  background: var(--amber-weak);
  border-left: 3px solid var(--amber);
  font-size: 0.85rem;
}
.channel-table { width: 100%; border-collapse: collapse; margin-top: 0.75rem; }
.channel-table th, .channel-table td { padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--border); text-align: left; }
.channel-table th.num, .channel-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.channel-name { font-weight: 600; }
.channel-note { color: var(--muted); font-size: 0.85em; }
.pill { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.78em; font-weight: 600; letter-spacing: -0.005em; }
.pill.live { background: var(--green-weak); color: var(--green); }
.pill.paused { background: var(--amber-weak); color: var(--amber); }
.pill.off { background: var(--surface-2); color: var(--muted); }
.pill.failing { background: var(--red-weak); color: var(--red); }
.risk { margin-top: 0.5rem; padding: 0.55rem 0.7rem; border-radius: 6px; border-left: 4px solid; font-size: 0.9em; }
.risk.caution { background: var(--amber-weak); border-color: var(--amber); color: var(--text); }
.risk.danger { background: var(--red-weak); border-color: var(--red); color: var(--text); }
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
.funnel-stage {
  position: relative;
  overflow: hidden;
}
/* Width proportional to the stage count, so the shape of the funnel is
   visible at a glance rather than something you work out from four numbers. */
.funnel-stage .bar {
  position: absolute;
  left: 0; bottom: 0;
  height: 3px;
  background: var(--accent);
  border-radius: 0 3px 0 0;
  transition: width 500ms var(--ease);
}
.funnel-stage .n { font-size: 1.9rem; font-weight: 600; display: block; letter-spacing: -0.03em; }
.funnel-stage .l { color: var(--muted); font-size: 0.75rem; letter-spacing: -0.005em; }
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
  <h2>Ingestion</h2>
  <p class="hint">Where posts are coming from right now. The primary is the JSON endpoint behind
  Cloudflare, which is the fast path and the fragile one. After three consecutive failed polls the
  circuit breaker opens and the RSS mirror takes over, then the primary is probed again after a
  cooldown. Both return the same status ids, so a switch cannot re-alert a post.</p>
  <div class="source-row" id="source-row"></div>
  <div class="transition" id="source-transition" hidden></div>
</section>

<section class="card">
  <h2>Alert channels</h2>
  <p class="hint">Every stock post is offered to all four. Delivery is tracked per channel, so one
  being down never holds up another. Pausing takes effect on the next poll and anything missed
  while a channel is off goes out when it comes back.</p>
  <table class="channel-table">
    <thead>
      <tr><th>Channel</th><th>State</th><th class="num">Sent</th><th class="num">Queued</th>
      <th class="num">Dropped</th><th></th></tr>
    </thead>
    <tbody id="channel-rows"></tbody>
  </table>
  <div class="inline-message" id="channel-message"></div>
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
      <div class="risk" id="interval-risk" hidden></div>
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

  function showMessage(id, text, ok) {
    var msg = qs(id);
    msg.textContent = text || "";
    msg.className = "inline-message" + (ok ? "" : " error");
  }

  function renderChannels(rows) {
    var tbody = qs("channel-rows");
    tbody.innerHTML = "";
    (rows || []).forEach(function (c) {
      var tr = document.createElement("tr");

      var nameCell = document.createElement("td");
      var nameEl = document.createElement("div");
      nameEl.className = "channel-name";
      nameEl.textContent = c.name;
      var note = document.createElement("div");
      note.className = "channel-note";
      note.textContent = c.configured ? c.description : "needs " + c.needs;
      nameCell.appendChild(nameEl);
      nameCell.appendChild(note);
      tr.appendChild(nameCell);

      // State reads from three separate things, so decide once here rather
      // than letting the table imply it. A channel can be set up and paused,
      // or set up and failing, and those are not the same thing.
      var state = document.createElement("td");
      var pill = document.createElement("span");
      if (!c.configured) {
        pill.className = "pill off";
        pill.textContent = "not set up";
      } else if (c.paused) {
        pill.className = "pill paused";
        pill.textContent = "paused";
      } else if (c.given_up > 0) {
        // permanent_failure: a deleted webhook or a revoked token. These are
        // never retried, so they leave no queue and no failed rows behind
        // them, and the panel used to call that "live" while the channel
        // dropped every single alert.
        pill.className = "pill failing";
        pill.textContent = "dropping";
      } else if (c.failed > 0 || c.queued > 0) {
        pill.className = "pill failing";
        pill.textContent = "failing";
      } else {
        pill.className = "pill live";
        pill.textContent = "live";
      }
      state.appendChild(pill);
      if (c.last_error) {
        var err = document.createElement("div");
        err.className = "channel-note";
        err.textContent = c.last_error;
        state.appendChild(err);
      }
      tr.appendChild(state);

      ["delivered", "queued", "given_up"].forEach(function (key) {
        var td = document.createElement("td");
        td.className = "num";
        td.textContent = c[key];
        tr.appendChild(td);
      });

      var action = document.createElement("td");
      if (c.configured && c.name !== "file") {
        // No button for the file sink: switching off the record of what the
        // agent decided is not something a web page should offer. The
        // endpoint refuses it too.
        // The file sink has no pause button on purpose. It is the record of
        // what the agent decided, and being able to switch off the audit
        // trail from a web page is not a feature.
        var btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = c.paused ? "Resume" : "Pause";
        btn.addEventListener("click", function () {
          toggleChannel(c.name, !c.paused);
        });
        action.appendChild(btn);
      }
      tr.appendChild(action);
      tbody.appendChild(tr);
    });
  }

  function toggleChannel(name, paused) {
    var body = "name=" + encodeURIComponent(name) + "&paused=" + (paused ? "1" : "0");
    fetch("/channel", {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: body
    }).then(function (r) { return r.json(); }).then(function (data) {
      showMessage("channel-message", data.message, data.ok);
      refresh();
    }).catch(function () {
      showMessage("channel-message", "could not reach the dashboard", false);
    });
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
    // Bar widths are relative to the first stage, so the funnel narrows the
    // way the numbers do. Four bare numbers made you do that arithmetic.
    var top = stages[0][1] || 1;
    var html = "";
    for (var i = 0; i < stages.length; i++) {
      var pct = Math.max(1, Math.round((stages[i][1] / top) * 100));
      html += '<div class="funnel-stage"><span class="n">' + stages[i][1] +
        '</span><span class="l">' + stages[i][0] +
        '</span><span class="bar" style="width:' + pct + '%"></span></div>';
      if (i < stages.length - 1) {
        var drop = stages[i][1] - stages[i + 1][1];
        html += '<div class="funnel-arrow"><span class="arrow-glyph">&#8594;</span><span>-' + drop + "</span></div>";
      }
    }
    qs("funnel").innerHTML = html;
  }

  function setIngestion(s) {
    var ing = s.ingestion || {};
    var row = qs("source-row");
    row.innerHTML = "";

    // Everything here comes from the server, including which source is
    // active. The page used to carry its own copy of the source names and
    // they did not match the ones the code actually uses, so no card was
    // ever marked live.
    var sources = ing.sources || [];
    var agentRunning = s.status !== "STOPPED";

    sources.forEach(function (src) {
      var card = document.createElement("div");
      // Live is only live while the agent is actually up. The recorded
      // source outlives the process that wrote it, so without this the panel
      // announced a source as live under a big STOPPED header.
      var live = src.active && agentRunning;
      card.className = "source-card" + (live ? " active" : "");

      var role = document.createElement("div");
      role.className = "role";
      if (live) {
        role.textContent = src.role + " - live now";
      } else if (src.active) {
        role.textContent = src.role + " - last used";
      } else {
        role.textContent = src.role;
      }

      var who = document.createElement("div");
      who.className = "who";
      who.textContent = src.who;

      var detail = document.createElement("div");
      detail.className = "detail";
      if (!ing.active) {
        detail.textContent = "not polling yet";
      } else if (src.active) {
        detail.textContent = ing.detail || (ing.ok ? "ok" : "no successful fetch yet");
      } else {
        detail.textContent = src.note;
      }

      card.appendChild(role);
      card.appendChild(who);
      card.appendChild(detail);
      row.appendChild(card);
    });

    var box = qs("source-transition");
    var t = ing.last_transition;
    if (!t) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    // "from" is a reserved word in some parsers, so bracket access.
    box.textContent = "Switched " + t["from"] + " to " + t["to"] + " " +
      fmtAgo(t.at) + ". Reason: " + t.reason;
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
    renderChannels(s.channels);
    setIngestion(s);

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
    showIntervalRisk(v);
  }

  // Truth Social sits behind Cloudflare and gives no permission for any of
  // this. The agent already enforces a 2.5 second floor between requests and
  // refuses past 600 in an hour, so a very short interval here does not
  // actually produce the request rate it implies, it just runs into those
  // limits. Worth saying out loud, because the interval box will happily
  // accept a number that reads as ten times faster and is not.
  function describeIntervalRisk(seconds) {
    if (seconds < 6) {
      return {
        level: "danger",
        text: "Below the hourly cap. The agent refuses past 600 requests an hour, which is one "
            + "every 6 seconds, so requests get held back and the real interval is not what this "
            + "says. Nothing is gained and the pattern is exactly what bot protection looks for."
      };
    }
    if (seconds < 15) {
      return {
        level: "danger",
        text: "Aggressive. This is unofficial access to a site behind Cloudflare with no "
            + "permission for it, and a fast, perfectly regular request pattern is the easiest "
            + "kind to notice. If the fingerprint stops working the agent falls back to the RSS "
            + "mirror, so you lose latency rather than everything, but recovering means finding "
            + "a new fingerprint."
      };
    }
    if (seconds < 30) {
      return {
        level: "caution",
        text: "Brisk. Fine for a short demo. For anything left running, 30 seconds or more costs "
            + "very little latency and asks a lot less of a server nobody agreed to give us."
      };
    }
    return null;
  }

  function showIntervalRisk(seconds) {
    var box = qs("interval-risk");
    var risk = describeIntervalRisk(seconds);
    if (!risk) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    box.className = "risk " + risk.level;
    box.textContent = risk.text;
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


def _parse_int(value: object, default):
    """int(value), or default when it is not a number.

    default is deliberately untyped: passing None is how callers ask
    "was this even a number?" so they can tell a typo apart from a value
    that was simply out of range.
    """
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
    # Separate file from the agent's. A backfill is a different process with
    # a different lifetime, and sharing one pid file would let Stop kill a
    # backfill or a backfill guard block Start.
    backfill_pid_path: Path = _DEFAULT_BACKFILL_PID
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
            "/channel": self._handle_channel,
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

    def _handle_channel(self) -> None:
        """Pause or resume one channel.

        Written to the store rather than held in the page, because the agent
        is a separate process. It reads the flag on every dispatch, so this
        takes effect on the next poll without a restart.
        """
        form = self._read_form()
        name = (form.get("name") or "").strip()
        known = {spec[0] for spec in _CHANNEL_SPECS}
        if name not in known:
            self._send_json(400, {"ok": False, "message": f"unknown channel: {name}"})
            return
        if name == "file":
            # The client renders no button for this one, but the endpoint has
            # to say no as well. Client side is not enforcement, and a paused
            # file sink is an audit trail switched off with no control in the
            # page to switch it back on.
            self._send_json(
                400,
                {"ok": False, "message": "the file sink is the audit trail and cannot be paused"},
            )
            return
        raw = form.get("paused")
        if raw not in ("0", "1"):
            # Anything else used to be read as "resume", so a typo or a
            # missing field silently un-paused a channel and reported success.
            self._send_json(
                400, {"ok": False, "message": "paused must be 0 or 1"}
            )
            return
        paused = raw == "1"
        with Store(self.db_path) as store:
            store.set_channel_paused(name, paused)
        word = "paused" if paused else "resumed"
        note = ""
        if not paused:
            # Stock alerts missed during the pause are recovered, bounded by
            # the same age gate live alerts use, so resuming after a long
            # pause does not announce last month. Ops alarms are not resent:
            # they are point in time, and every one of them already went to
            # the file sink, which cannot be paused.
            note = (" Stock alerts missed while it was off go out on the next poll, "
                    "if they are still recent enough to be worth sending.")
        self._send_json(200, {"ok": True, "message": f"{name} {word}.{note}"})

    def _handle_start(self) -> None:
        form = self._read_form()
        running, pid = process_status(self.pid_path)
        if running:
            self._send_json(200, {"ok": False, "message": f"already running (pid {pid})"})
            return

        default_interval = _get_setting_int(self.db_path, "poll_interval_seconds", _DEFAULT_INTERVAL_SECONDS)
        interval = _clamp_interval(form.get("interval"), default_interval)
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
        if not looks_like_our_agent(pid):
            # The pid is live but is not the agent, so the number has been
            # reused since the file was written. Clear the stale file and
            # refuse rather than signalling a process we did not start.
            clear_pid(self.pid_path)
            self._send_json(200, {
                "ok": False,
                "message": (f"pid {pid} is not the agent, so it was not signalled. "
                            "The stale pid file has been cleared."),
            })
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OverflowError):
            pass
        deadline = time.monotonic() + _STOP_WAIT_SECONDS
        while time.monotonic() < deadline and is_running(pid):
            time.sleep(0.05)
        clear_pid(self.pid_path)
        self._send_json(200, {"ok": True, "message": f"stopped (pid {pid})"})

    def _handle_backfill(self) -> None:
        form = self._read_form()
        default_days = _get_setting_int(self.db_path, "backfill_days", _DEFAULT_BACKFILL_DAYS)
        days = max(1, min(_MAX_BACKFILL_DAYS, _parse_int(form.get("days"), default_days)))

        # One at a time. Every click used to spawn another backfill, and they
        # all share the same paging cursor in agent_state, each reading it
        # once at startup and then overwriting it per page. Concurrent runs
        # interleaved each other's paging, so history came back duplicated
        # and with gaps, and the request rate against Truth Social multiplied
        # by however many times the button had been pressed, which is exactly
        # what the 2.5 second floor exists to prevent.
        existing = read_pid(self.backfill_pid_path)
        if existing is not None and is_running(existing):
            self._send_json(200, {
                "ok": False,
                "message": f"a backfill is already running (pid {existing})",
            })
            return

        _save_setting(self.db_path, "backfill_days", str(days))
        # Fire and forget: the request must not block on a job that can run
        # for minutes and talks to the live network.
        # Write to a separate file. The default output is data/history.jsonl,
        # which is the frozen corpus the evaluation set is built from, and a
        # backfill appends to it.
        out_path = _REPO_ROOT / "data" / "backfill_latest.jsonl"
        proc = self.spawn_fn(
            [sys.executable, str(_BACKFILL_SCRIPT), "--days", str(days),
             "--db", str(self.db_path), "--out", str(out_path)],
            cwd=str(_REPO_ROOT),
        )
        write_pid(self.backfill_pid_path, proc.pid)
        self._send_json(200, {"ok": True, "message": f"backfill started for {days} day(s)", "backfill_days": days})

    def _handle_settings(self) -> None:
        form = self._read_form()
        result: dict[str, object] = {"ok": True}
        # Say when the value saved is not the value asked for. This used to
        # answer {"ok": true} with the old number after silently discarding
        # the input, so a typo looked like a successful change.
        notes = []
        if "interval" in form:
            stored = _get_setting_int(self.db_path, "poll_interval_seconds", _DEFAULT_INTERVAL_SECONDS)
            raw = str(form.get("interval", "")).strip()
            interval = _clamp_interval(raw, stored)
            if raw != str(interval):
                if _parse_int(raw, None) is None:
                    notes.append(f"'{raw}' is not a number, so the poll interval "
                                 f"is unchanged at {interval}s")
                else:
                    notes.append(f"poll interval set to {interval}s, the nearest "
                                 f"allowed value between {_MIN_INTERVAL_SECONDS} "
                                 f"and {_MAX_INTERVAL_SECONDS}")
            _save_setting(self.db_path, "poll_interval_seconds", str(interval))
            result["poll_interval_seconds"] = interval
        if "backfill_days" in form:
            stored_days = _get_setting_int(self.db_path, "backfill_days", _DEFAULT_BACKFILL_DAYS)
            raw_days = str(form.get("backfill_days", "")).strip()
            days = max(1, min(_MAX_BACKFILL_DAYS, _parse_int(raw_days, stored_days)))
            if raw_days != str(days):
                if _parse_int(raw_days, None) is None:
                    notes.append(f"'{raw_days}' is not a number, so the backfill "
                                 f"window is unchanged at {days} day(s)")
                else:
                    notes.append(f"backfill window set to {days} day(s), the nearest "
                                 f"allowed value")
            _save_setting(self.db_path, "backfill_days", str(days))
            result["backfill_days"] = days
        if notes:
            result["message"] = ". ".join(notes)
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
