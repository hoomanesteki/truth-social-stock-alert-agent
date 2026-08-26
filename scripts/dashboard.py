#!/usr/bin/env python3
"""Local read only dashboard for the alert agent's sqlite store.

    uv run python scripts/dashboard.py --port 8000 --db data/agent.db

One file, standard library http.server, plain f-string HTML with inline
CSS. No javascript framework, no external assets, no CDN links.

There is no authentication of any kind. This is a local prototype and must
never be bound to anything other than localhost or exposed to a network.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import sqlite3
import subprocess
import sys
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
_BACKFILL_SCRIPT = _REPO_ROOT / "scripts" / "backfill.py"
_LEXICON_HEADER = ["ticker", "company", "aliases", "ambiguity", "ambiguous_aliases", "kind", "notes"]
_TEXT_PREVIEW_CHARS = 200

_PAGE_STYLE = """
body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { margin-bottom: 0.2rem; }
section { margin-bottom: 2rem; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }
th { background: #f0f0f0; }
textarea { width: 100%; font-family: monospace; font-size: 0.85rem; }
.message { padding: 0.5rem; background: #fff3cd; border: 1px solid #d0a000; }
"""


def _escape(text: str) -> str:
    return html.escape(str(text), quote=True)


def _truncate(text: str, limit: int = _TEXT_PREVIEW_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def load_mentions(db_path: str, ticker: str | None = None, limit: int = 100) -> list[dict]:
    """Stock related posts, newest first, optionally narrowed to one ticker.

    Queried directly against sqlite rather than through Store: Store has no
    method for listing detections together with their mentions, and adding
    one is outside the scope of this change.
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

    ticker_upper = ticker.strip().upper() if ticker else None
    mentions = []
    for row in rows:
        raw = json.loads(row["mentions_json"]) if row["mentions_json"] else []
        tickers = [m["ticker"] for m in raw]
        if ticker_upper and ticker_upper not in tickers:
            continue
        mentions.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "tickers": tickers,
                "companies": [m["company"] for m in raw],
                "text": row["text"] or "",
                "url": row["url"],
            }
        )
        if len(mentions) >= limit:
            break
    return mentions


def latency_table(db_path: str) -> list[tuple[str, int, float, float, float]]:
    """Count/p50/p95/max per stage, reusing scripts/latency_report.py's own
    computation rather than a second copy of the percentile logic."""
    rows = load_rows(db_path)
    table = []
    for label, start_col, end_col in _STAGES:
        durations = sorted(stage_durations(rows, start_col, end_col))
        count = len(durations)
        p50 = percentile(durations, 0.50)
        p95 = percentile(durations, 0.95)
        worst = durations[-1] if durations else 0.0
        table.append((label, count, p50, p95, worst))
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


def render_dashboard(db_path: str, ticker_filter: str | None) -> str:
    with Store(db_path) as store:
        stats = store.stats()
        monitor = HealthMonitor(store)
        health_status = monitor.status()
        alarms = active_alarms(monitor)

    mentions = load_mentions(db_path, ticker_filter)
    rows = latency_table(db_path)

    mention_rows = "".join(
        f"<tr><td>{_escape(m['created_at'])}</td>"
        f"<td>{_escape(', '.join(m['tickers']))}</td>"
        f"<td>{_escape(', '.join(m['companies']))}</td>"
        f"<td>{_escape(_truncate(m['text']))}</td>"
        f'<td><a href="{_escape(m["url"])}">link</a></td></tr>'
        for m in mentions
    ) or '<tr><td colspan="5">No mentions yet.</td></tr>'

    latency_rows_html = "".join(
        f"<tr><td>{_escape(label)}</td><td>{count}</td><td>{p50:.1f}</td>"
        f"<td>{p95:.1f}</td><td>{worst:.1f}</td></tr>"
        for label, count, p50, p95, worst in rows
    )

    alarms_html = "".join(f"<li>{_escape(a)}</li>" for a in alarms) if alarms else "<li>none</li>"
    filter_value = _escape(ticker_filter or "")

    return f"""<!doctype html>
<html>
<head><title>Alert agent dashboard</title><style>{_PAGE_STYLE}</style></head>
<body>
<h1>Alert agent dashboard</h1>

<section>
<h2>Store counts</h2>
<table>
<tr><th>posts</th><th>stock related</th><th>alerts delivered</th><th>alerts failed</th></tr>
<tr><td>{stats['posts']}</td><td>{stats['stock_related']}</td>
<td>{stats['alerts_delivered']}</td><td>{stats['alerts_failed']}</td></tr>
</table>
</section>

<section>
<h2>Health</h2>
<table>
<tr><th>last successful poll</th><td>{_escape(health_status['last_successful_poll_at'])}</td></tr>
<tr><th>last new post</th><td>{_escape(health_status['last_new_post_at'])}</td></tr>
<tr><th>consecutive errors</th><td>{health_status['consecutive_errors']}</td></tr>
</table>
<h3>Active alarms</h3>
<ul>{alarms_html}</ul>
</section>

<section>
<h2>Latency (seconds)</h2>
<table>
<tr><th>stage</th><th>count</th><th>p50</th><th>p95</th><th>max</th></tr>
{latency_rows_html}
</table>
</section>

<section>
<h2>Mentions</h2>
<form method="get" action="/">
<label>Ticker <input type="text" name="ticker" value="{filter_value}"></label>
<button type="submit">Filter</button>
<a href="/">clear</a>
</form>
<table>
<tr><th>timestamp</th><th>tickers</th><th>companies</th><th>text</th><th>link</th></tr>
{mention_rows}
</table>
</section>

<section>
<h2>Backfill</h2>
<form method="post" action="/backfill">
<label>Days <input type="number" name="days" value="45" min="1"></label>
<button type="submit">Run backfill</button>
</form>
</section>

<section>
<h2>Lexicon</h2>
<p><a href="/lexicon">Edit ticker lexicon</a></p>
</section>

</body>
</html>
"""


def validate_lexicon_csv(text: str) -> str | None:
    """Return an error message, or None when the header row is well formed."""
    lines = text.splitlines()
    if not lines:
        return "empty file"
    header = next(csv.reader([lines[0]]))
    if header != _LEXICON_HEADER:
        return "header row must be exactly: " + ",".join(_LEXICON_HEADER)
    return None


def render_lexicon_page(lexicon_path: Path, message: str = "") -> str:
    text = lexicon_path.read_text(encoding="utf-8") if lexicon_path.exists() else ""
    rows_html = "".join(
        f"<tr><td>{_escape(r.get('ticker', ''))}</td><td>{_escape(r.get('company', ''))}</td>"
        f"<td>{_escape(r.get('aliases', ''))}</td><td>{_escape(r.get('ambiguity', ''))}</td></tr>"
        for r in csv.DictReader(io.StringIO(text))
    ) if text else ""
    message_html = f'<p class="message">{_escape(message)}</p>' if message else ""

    return f"""<!doctype html>
<html>
<head><title>Ticker lexicon</title><style>{_PAGE_STYLE}</style></head>
<body>
<h1>Ticker lexicon</h1>
<p><a href="/">back to dashboard</a></p>
{message_html}
<form method="post" action="/lexicon">
<textarea name="csv" rows="20">{_escape(text)}</textarea>
<br>
<button type="submit">Save</button>
</form>
<h2>Current entries</h2>
<table>
<tr><th>ticker</th><th>company</th><th>aliases</th><th>ambiguity</th></tr>
{rows_html}
</table>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: str = _DEFAULT_DB
    lexicon_path: Path = _DEFAULT_LEXICON
    server_version = "TSAlertDashboard/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            ticker = (parse_qs(parsed.query).get("ticker", [""])[0] or "").strip() or None
            self._send_html(render_dashboard(self.db_path, ticker))
        elif parsed.path == "/lexicon":
            self._send_html(render_lexicon_page(self.lexicon_path))
        else:
            self._send_text(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/backfill":
            self._handle_backfill()
        elif parsed.path == "/lexicon":
            self._handle_lexicon_post()
        else:
            self._send_text(404, "not found")

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(body)
        return {k: v[0] for k, v in parsed.items()}

    def _handle_backfill(self) -> None:
        form = self._read_form()
        try:
            days = int(form.get("days", "45"))
        except ValueError:
            days = 45
        # Fire and forget: the request must not block on a job that can run
        # for minutes and talks to the live network.
        subprocess.Popen(
            [sys.executable, str(_BACKFILL_SCRIPT), "--days", str(days), "--db", self.db_path],
            cwd=str(_REPO_ROOT),
        )
        self._send_html(
            f"<p>Backfill started for {days} day(s) in the background.</p>"
            '<p><a href="/">back to dashboard</a></p>'
        )

    def _handle_lexicon_post(self) -> None:
        form = self._read_form()
        text = form.get("csv", "")
        error = validate_lexicon_csv(text)
        if error is not None:
            self._send_html(render_lexicon_page(self.lexicon_path, message=f"Not saved: {error}"))
            return
        self.lexicon_path.write_text(text, encoding="utf-8")
        self._send_html(render_lexicon_page(self.lexicon_path, message="Saved."))

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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


def make_server(host: str, port: int, db_path: str, lexicon_path: Path) -> HTTPServer:
    # A per-server subclass instead of module globals, so more than one
    # server (as in tests) can point at different databases at once.
    bound_handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {"db_path": db_path, "lexicon_path": Path(lexicon_path)},
    )
    return HTTPServer((host, port), bound_handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local read only dashboard for the alert agent")
    parser.add_argument("--host", default="127.0.0.1", help="bind address, local only by default")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=_DEFAULT_DB, help="path to the sqlite db")
    parser.add_argument("--lexicon", default=str(_DEFAULT_LEXICON), help="path to tickers.csv")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = make_server(args.host, args.port, args.db, Path(args.lexicon))
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
