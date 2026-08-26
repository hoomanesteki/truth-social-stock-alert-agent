from __future__ import annotations

import sys
import threading
import time
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


@contextmanager
def running_server(db_path: Path, lexicon_path: Path):
    server = dashboard.make_server("127.0.0.1", 0, str(db_path), lexicon_path)
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


def test_mentions_page_renders_from_temp_store(tmp_path):
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


def test_ticker_filter_narrows_the_list(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")
    seed_store(db_path)

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _get(base_url + "/?ticker=TSLA")

    assert status == 200
    assert "Huge news for Tesla today" in body
    assert "Boeing built a great plane" not in body


def test_malformed_lexicon_post_is_refused_and_leaves_file_unchanged(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _post(base_url + "/lexicon", {"csv": "not,a,valid,header\nrow,here\n"})

    assert status == 200
    assert "Not saved" in body
    assert lexicon_path.read_text(encoding="utf-8") == _LEXICON_TEXT


def test_valid_lexicon_post_saves_the_file(tmp_path):
    db_path = tmp_path / "agent.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")

    new_text = (
        "ticker,company,aliases,ambiguity,ambiguous_aliases,kind,notes\n"
        "AAPL,Apple,Apple,low,,equity,\n"
    )

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _post(base_url + "/lexicon", {"csv": new_text})

    assert status == 200
    assert "Saved" in body
    assert lexicon_path.read_text(encoding="utf-8") == new_text


def test_health_and_latency_sections_render_with_an_empty_database(tmp_path):
    db_path = tmp_path / "empty.db"
    lexicon_path = tmp_path / "tickers.csv"
    lexicon_path.write_text(_LEXICON_TEXT, encoding="utf-8")

    with running_server(db_path, lexicon_path) as base_url:
        status, body = _get(base_url + "/")

    assert status == 200
    assert "Health" in body
    assert "Latency" in body
    assert "No mentions yet." in body
    assert "none" in body  # no active alarms
