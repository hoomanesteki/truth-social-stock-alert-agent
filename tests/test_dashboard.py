from __future__ import annotations

import json
import os
import sqlite3
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

    assert state["active"] == ""
    assert state["last_transition"] is None
    assert state["ok"] is False
    # Both real sources are still described, none of them marked live.
    assert [src["key"] for src in state["sources"]] == ["truthsocial_api", "trumpstruth_rss"]
    assert not any(src["active"] for src in state["sources"])


def test_ingestion_state_reads_what_the_agent_recorded(tmp_path):
    """The agent and the dashboard are separate processes, so the only way
    the page can know which source is live is through the store."""
    import json as _json

    db = tmp_path / "recorded.db"
    with Store(db) as store:
        store.init_schema()
        store.set_state("active_source", "trumpstruth_rss")
        store.set_state("source_detail", "ok")
        store.set_state("source_ok", "1")
        store.set_state("last_source_transition", _json.dumps({
            "at": "2026-08-29T12:00:00+00:00",
            "from": "truthsocial_api",
            "to": "trumpstruth_rss",
            "reason": "primary failed: cloudflare 403",
        }))
        state = dashboard.ingestion_state(store)

    assert state["active"] == "trumpstruth_rss"
    assert state["ok"] is True
    assert state["last_transition"]["from"] == "truthsocial_api"
    assert "cloudflare" in state["last_transition"]["reason"]


def test_a_corrupt_transition_record_does_not_break_the_page(tmp_path):
    db = tmp_path / "corrupt.db"
    with Store(db) as store:
        store.init_schema()
        store.set_state("active_source", "truthsocial_api")
        store.set_state("last_source_transition", "{not json")
        state = dashboard.ingestion_state(store)

    assert state["active"] == "truthsocial_api"
    assert state["last_transition"] is None


def test_the_source_roster_uses_the_names_the_code_really_uses():
    """The page had its own copy of the source names and they were wrong.

    The classes call themselves truthsocial_api and trumpstruth_rss; the
    JavaScript looked for "truthsocial" and "rss". No card ever matched, so
    nothing showed as live, the live error detail was unreachable exactly when
    it mattered, and a real mirror run rendered as a replay. The roster is
    built from the classes' own name attributes now, so this cannot drift
    again without the import failing.
    """
    from tsalert.sources.rss_mirror import TrumpsTruthRssSource
    from tsalert.sources.truthsocial import TruthSocialApiSource

    assert TruthSocialApiSource.name == "truthsocial_api"
    assert TrumpsTruthRssSource.name == "trumpstruth_rss"


def test_a_live_mirror_run_marks_the_mirror_not_a_replay(tmp_path):
    db = tmp_path / "mirror.db"
    with Store(db) as store:
        store.init_schema()
        store.set_state("active_source", "trumpstruth_rss")
        store.set_state("source_detail", "ok")
        state = dashboard.ingestion_state(store)

    keys = [src["key"] for src in state["sources"]]
    assert keys == ["truthsocial_api", "trumpstruth_rss"]  # no replay card
    live = [src for src in state["sources"] if src["active"]]
    assert len(live) == 1
    assert live[0]["key"] == "trumpstruth_rss"
    assert live[0]["role"] == "Fallback"


def test_a_replay_run_gets_its_own_card(tmp_path):
    db = tmp_path / "replay.db"
    with Store(db) as store:
        store.init_schema()
        store.set_state("active_source", "demo")
        state = dashboard.ingestion_state(store)

    assert [src["key"] for src in state["sources"]][0] == "demo"
    assert [src["active"] for src in state["sources"]] == [True, False, False]


def test_is_running_refuses_pids_that_signal_more_than_one_process():
    """0 means "my whole process group" and -1 means "everything I may
    signal". Both answer os.kill(pid, 0) with success, so a pid file holding
    0 made the page report RUNNING and then made Stop SIGTERM the dashboard
    itself.
    """
    assert dashboard.is_running(0) is False
    assert dashboard.is_running(-1) is False
    assert dashboard.is_running(-4242) is False
    assert dashboard.is_running(10 ** 19) is False  # larger than pid_t


def test_stop_will_not_signal_a_process_that_is_not_the_agent():
    """A pid file outlives the process it names and pids get reused."""
    import os
    import subprocess

    stranger = subprocess.Popen(["/bin/sleep", "30"])
    try:
        assert dashboard.looks_like_our_agent(stranger.pid) is False
        assert dashboard.looks_like_our_agent(os.getpid()) is False
    finally:
        stranger.kill()
        stranger.wait()


def test_an_interval_too_large_for_a_timedelta_is_refused(tmp_path):
    """build_state puts this into timedelta() and the agent into time.sleep,
    and both raise OverflowError. The page died with no response at all, the
    value was already persisted, and Stop was unreachable."""
    assert dashboard._clamp_interval("100000000000000000000", 90) == dashboard._MAX_INTERVAL_SECONDS
    assert dashboard._clamp_interval("-5", 90) == dashboard._MIN_INTERVAL_SECONDS
    assert dashboard._clamp_interval("abc", 90) == 90
    assert dashboard._clamp_interval("120", 90) == 120


def test_a_lexicon_with_no_rows_is_refused():
    """A correct header with nothing under it saved cleanly and left the
    detector with an empty lexicon: every alias and bare ticker match gone,
    and nothing said so."""
    header = ",".join(dashboard._LEXICON_HEADER)
    assert dashboard.validate_lexicon_csv(header) is not None
    assert dashboard.validate_lexicon_csv(header + "\nAAPL,Apple Inc.,Apple,low,,equity,") is None
    assert dashboard.validate_lexicon_csv(header + "\nAAPL,Apple Inc.") is not None
    assert dashboard.validate_lexicon_csv(header + "\nAAPL,,Apple,low,,equity,") is not None


def test_the_real_lexicon_still_passes_validation():
    """The guard must not reject the file the project ships with."""
    from pathlib import Path as _Path
    text = (_Path(__file__).parent.parent / "data" / "lexicon" / "tickers.csv").read_text()
    assert dashboard.validate_lexicon_csv(text) is None


def test_a_failing_request_still_gets_a_response(tmp_path, monkeypatch):
    """BaseHTTPRequestHandler answers an unhandled exception by closing the
    connection with nothing at all, so the browser sees the whole dashboard
    as dead rather than one control as broken. A bad config value, an out of
    range number and a locked database each took the entire page down that
    way. A 500 with the reason is recoverable; silence is not.
    """
    import json as _json
    import urllib.error
    import urllib.request

    db = tmp_path / "guard.db"
    server = dashboard.make_server("127.0.0.1", 0, str(db), tmp_path / "lex.csv")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Force the state builder to blow up the way a locked database would.
        monkeypatch.setattr(
            dashboard, "build_state",
            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
        )
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5)
            raise AssertionError("expected a 500")
        except urllib.error.HTTPError as exc:
            assert exc.code == 500
            body = _json.loads(exc.read())
            assert body["ok"] is False
            assert "database is locked" in body["message"]
    finally:
        server.shutdown()
        server.server_close()


def test_the_dashboard_creates_the_schema_it_then_reads(tmp_path):
    """Reads open the database with migrate=False, so pointing the dashboard
    at a path that has never been used has to still work."""
    db = tmp_path / "brandnew.db"
    assert not db.exists()

    server = dashboard.make_server("127.0.0.1", 0, str(db), tmp_path / "lex.csv")
    try:
        assert db.exists()
        with Store(db, migrate=False) as store:
            tables = {r[0] for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"posts", "alerts", "agent_state", "latency"} <= tables
    finally:
        server.server_close()
