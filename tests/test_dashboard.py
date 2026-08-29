from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dashboard
from tsalert.models import Detection, Post, TickerMention
from tsalert.store import Store

_LEXICON_TEXT = (
    "ticker,company,aliases,ambiguity,ambiguous_aliases,kind,notes\n"
    "TSLA,Tesla,Tesla|Tesla Motors,low,,equity,\n"
)


def make_post(post_id: str, text: str) -> Post:
    now = datetime(2026, 8, 25, 18, 32, 56, tzinfo=timezone.utc)
    return Post(
        id=post_id,
        account="realDonaldTrump",
        created_at=now,
        text=text,
        url=f"https://truthsocial.com/@realDonaldTrump/{post_id}",
        raw_html=f"<p>{text}</p>",
        is_reply=False,
        is_repost=False,
        is_quote=False,
        has_media=False,
        source="fixture",
        fetched_at=now,
    )


def seed_store(db_path: Path) -> None:
    with Store(db_path) as store:
        post_a = make_post("1", "Huge news for Tesla today, stock is up.")
        store.upsert_post(post_a)
        store.save_detection(
            Detection(
                post_id="1",
                is_stock_related=True,
                mentions=(
                    TickerMention(
                        ticker="TSLA",
                        company="Tesla",
                        matched_text="Tesla",
                        method="alias",
                        confidence=0.9,
                    ),
                ),
                detector="rules",
                latency_ms=1.0,
            )
        )

        post_b = make_post("2", "Boeing built a great plane for the trip.")
        store.upsert_post(post_b)
        store.save_detection(
            Detection(
                post_id="2",
                is_stock_related=True,
                mentions=(
                    TickerMention(
                        ticker="BA", company="Boeing", matched_text="Boeing", method="alias", confidence=0.7
                    ),
                ),
                detector="rules",
                latency_ms=1.0,
            )
        )


class _FakeProc:
    """Stands in for subprocess.Popen's return value.

    pid is the test process's own pid, which os.kill(pid, 0) will always
    find alive without this test ever spawning a real child process.
    """

    def __init__(self) -> None:
        self.pid = os.getpid()


class SpawnRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeProc()


@contextmanager
def running_server(db_path: Path, lexicon_path: Path, pid_path: Path | None = None, spawn_fn=None):
    server = dashboard.make_server(
        "127.0.0.1",
        0,
        str(db_path),
        lexicon_path,
        pid_path=pid_path or (db_path.parent / "agent.pid"),
        metrics_path=db_path.parent / "metrics.md",
        spawn_fn=spawn_fn or SpawnRecorder(),
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _get(url: str) -> tuple[int, str]:
    with urlopen(url) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(url: str, data: dict) -> tuple[int, str]:
    body = urlencode(data).encode("utf-8")
    req = Request(url, data=body, method="POST")
    try:
        with urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_page_renders_from_temp_store(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")
    seed_store(db_path)

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _get(base_url + "/")

    assert status == 200
    assert "TSLA" in body
    assert "Tesla" in body
    assert "BA" in body
    assert "Boeing" in body
    assert "Huge news for Tesla today" in body


def test_ticker_filter_narrows_the_feed(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")
    seed_store(db_path)

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _get(base_url + "/api/state?ticker=TSLA")

    assert status == 200
    data = json.loads(body)
    tickers_seen = {t for m in data["mentions"] for t in m["tickers"]}
    assert tickers_seen == {"TSLA"}
    assert any("Huge news for Tesla today" in m["text"] for m in data["mentions"])
    assert not any("Boeing" in m["text"] for m in data["mentions"])
    # the ticker list offered for the filter still lists every ticker in the data
    assert set(data["tickers"]) == {"TSLA", "BA"}


def test_api_state_returns_valid_json_with_expected_keys(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")
    seed_store(db_path)

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _get(base_url + "/api/state")

    assert status == 200
    data = json.loads(body)
    expected_keys = {
        "status",
        "pid",
        "stats",
        "consecutive_errors",
        "last_poll_at",
        "last_successful_poll_at",
        "last_new_post_at",
        "poll_interval_seconds",
        "next_poll_at",
        "backfill_days",
        "alarms",
        "pipeline",
        "mentions",
        "tickers",
        "ticker_filter",
        "latency",
        "metrics",
        "server_time",
    }
    assert expected_keys.issubset(data.keys())
    assert data["status"] == "STOPPED"
    assert data["stats"]["posts"] == 2


def test_is_running_is_false_for_a_stale_pid_file(tmp_path):
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # process has exited and been reaped, the pid is now free to be stale

    assert dashboard.is_running(proc.pid) is False


def test_starting_twice_does_not_spawn_a_second_process(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")
    pid_path = tmp_path / "agent.pid"
    recorder = SpawnRecorder()

    with running_server(db_path, lexicon_path, pid_path=pid_path, spawn_fn=recorder) as base_url:
        status1, body1 = _post(base_url + "/start", {"interval": "60"})
        status2, body2 = _post(base_url + "/start", {"interval": "60"})

    assert status1 == 200 and json.loads(body1)["ok"] is True
    assert status2 == 200 and json.loads(body2)["ok"] is False
    assert len(recorder.calls) == 1


def test_malformed_lexicon_post_is_refused_and_leaves_file_unchanged(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _post(base_url + "/lexicon", {"csv": "not,a,valid,header\nrow,here\n"})

    assert status == 200
    data = json.loads(body)
    assert data["ok"] is False
    assert "Not saved" in data["message"]
    assert lexicon_path.read_text(encoding="utf-8") == _LEXICON_TEXT


def test_settings_written_through_the_dashboard_survive_being_read_back(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _post(base_url + "/settings", {"interval": "180", "backfill_days": "30"})
        assert status == 200
        assert json.loads(body)["ok"] is True

    # A fresh server against the same database simulates a dashboard restart.
    with running_server(db_path, lexicon_path) as base_url:
        status, body = _get(base_url + "/api/state")

    data = json.loads(body)
    assert data["poll_interval_seconds"] == 180
    assert data["backfill_days"] == 30


def test_channel_health_lists_every_channel_including_the_unconfigured(tmp_path, monkeypatch):
    """The page has to describe a channel that is switched off.

    build_channels deliberately leaves unconfigured channels out of its list,
    so the dashboard keeps its own roster. Otherwise Discord not being set up
    would look identical to Discord not existing.
    """
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    db = tmp_path / "channels.db"
    with Store(db) as store:
        store.init_schema()

    rows = dashboard.channel_health(str(db))
    names = [r["name"] for r in rows]
    assert names == ["file", "console", "discord", "telegram"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["file"]["configured"] is True
    assert by_name["console"]["configured"] is True
    assert by_name["discord"]["configured"] is False
    assert by_name["discord"]["needs"] == "DISCORD_WEBHOOK_URL"


def test_pausing_a_channel_round_trips_through_the_store(tmp_path):
    db = tmp_path / "pause.db"
    with Store(db) as store:
        store.init_schema()
        assert store.is_channel_paused("discord") is False
        store.set_channel_paused("discord", True)

    rows = dashboard.channel_health(str(db))
    assert [r["paused"] for r in rows if r["name"] == "discord"] == [True]

    with Store(db) as store:
        store.set_channel_paused("discord", False)
    rows = dashboard.channel_health(str(db))
    assert [r["paused"] for r in rows if r["name"] == "discord"] == [False]


def test_a_bad_setting_does_not_take_the_whole_page_down(tmp_path, monkeypatch):
    """channel_health reads config, and BaseHTTPRequestHandler answers an
    unhandled exception by sending nothing at all. One malformed value used
    to kill the page, the JSON and every control with it."""
    monkeypatch.setenv("MAX_ALERT_AGE_HOURS", "0")
    db = tmp_path / "badconfig.db"
    with Store(db) as store:
        store.init_schema()

    rows = dashboard.channel_health(str(db))
    assert len(rows) == 4
    assert all(r["configured"] is False for r in rows)
    assert "config unreadable" in (rows[0]["last_error"] or "")


def test_a_channel_dropping_every_alert_is_not_reported_as_healthy(tmp_path):
    """permanent_failure rows leave no queue and no failed rows behind them,
    so a deleted webhook rendered as live with zero of everything."""
    from tsalert.alerts.dispatcher import AlertDispatcher
    from tsalert.sources.base import PermanentSourceError
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_dispatcher import FakeChannel, make_detection, make_post, no_sleep

    db = tmp_path / "dropping.db"
    with Store(db) as store:
        store.init_schema()
        post = make_post()
        store.upsert_post(post)
        channel = FakeChannel("discord", outcomes=[PermanentSourceError("status 404")])
        AlertDispatcher([channel], store, sleep=no_sleep).dispatch(post, make_detection(post.id))

        stats = store.channel_stats()
        assert stats["discord"]["given_up"] == 1
        assert stats["discord"]["failed"] == 0
        assert stats["discord"]["delivered"] == 0

    rows = {r["name"]: r for r in dashboard.channel_health(str(db))}
    assert rows["discord"]["given_up"] == 1


def test_the_most_recent_error_is_the_one_reported(tmp_path):
    """first_attempt_at is the claim time, set once and never touched, so
    ordering by it could surface a stale error as the current problem."""
    import time
    from test_dispatcher import make_detection, make_post

    db = tmp_path / "errors.db"
    with Store(db) as store:
        store.init_schema()
        old_post, new_post = make_post("a"), make_post("b")
        for post in (old_post, new_post):
            store.upsert_post(post)
            store.save_detection(make_detection(post.id))

        store.claim_alert(new_post.id, "discord")
        store.record_alert_result(new_post.id, "discord", "failed", "STALE")
        time.sleep(0.01)
        store.claim_alert(old_post.id, "discord")
        store.record_alert_result(old_post.id, "discord", "failed", "CURRENT")

        assert store.channel_stats()["discord"]["last_error"] == "CURRENT"


def test_ingestion_state_is_empty_before_the_agent_has_run(tmp_path):
    """An agent that has never polled must not render as a healthy primary.

    The page describes both sources whatever happens, so the honest empty
    state matters: "not polling yet" rather than a green primary nobody has
    exercised.
    """
    db = tmp_path / "fresh.db"
    with Store(db) as store:
        store.init_schema()
        state = dashboard.ingestion_state(store)

    assert state == {"active": "", "detail": "", "ok": False, "last_transition": None}


def test_ingestion_state_reads_what_the_agent_recorded(tmp_path):
    """The agent and the dashboard are separate processes, so the only way
    the page can know which source is live is through the store."""
    import json as _json

    db = tmp_path / "recorded.db"
    with Store(db) as store:
        store.init_schema()
        store.set_state("active_source", "rss")
        store.set_state("source_detail", "ok")
        store.set_state("source_ok", "1")
        store.set_state("last_source_transition", _json.dumps({
            "at": "2026-08-29T12:00:00+00:00",
            "from": "truthsocial",
            "to": "rss",
            "reason": "primary failed: cloudflare 403",
        }))
        state = dashboard.ingestion_state(store)

    assert state["active"] == "rss"
    assert state["ok"] is True
    assert state["last_transition"]["from"] == "truthsocial"
    assert "cloudflare" in state["last_transition"]["reason"]


def test_a_corrupt_transition_record_does_not_break_the_page(tmp_path):
    db = tmp_path / "corrupt.db"
    with Store(db) as store:
        store.init_schema()
        store.set_state("active_source", "truthsocial")
        store.set_state("last_source_transition", "{not json")
        state = dashboard.ingestion_state(store)

    assert state["active"] == "truthsocial"
    assert state["last_transition"] is None
