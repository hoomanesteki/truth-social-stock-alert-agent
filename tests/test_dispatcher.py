from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tsalert.alerts.dispatcher import AlertDispatcher
from tsalert.models import Detection, Post, TickerMention
from tsalert.sources.base import PermanentSourceError, TransientSourceError
from tsalert.store import Store


def make_post(post_id: str = "123") -> Post:
    now = datetime(2026, 8, 25, 18, 32, 56, tzinfo=timezone.utc)
    return Post(
        id=post_id,
        account="realDonaldTrump",
        created_at=now,
        text="Big news for the company today.",
        url=f"https://truthsocial.com/@realDonaldTrump/{post_id}",
        raw_html="<p>Big news for the company today.</p>",
        is_reply=False,
        is_repost=False,
        is_quote=False,
        has_media=False,
        source="fixture",
        fetched_at=now,
    )


def make_detection(post_id: str = "123") -> Detection:
    mention = TickerMention(
        ticker="DJT", company="Trump Media", matched_text="$DJT", method="cashtag", confidence=0.95
    )
    return Detection(
        post_id=post_id, is_stock_related=True, mentions=(mention,), detector="rules", latency_ms=1.0
    )


def no_sleep(_seconds: float) -> None:
    pass


class FakeChannel:
    """Scripted channel: pops one outcome per send() call.

    An outcome is None (success), an exception instance to raise, or the
    string 'raise-last' meaning re-raise the final scripted exception on
    every call after the script is exhausted.
    """

    def __init__(self, name: str, outcomes=None, configured: bool = True):
        self.name = name
        self.outcomes = list(outcomes) if outcomes is not None else []
        self.configured = configured
        self.sent: list[str] = []

    def is_configured(self) -> bool:
        return self.configured

    def send(self, text: str) -> None:
        self.sent.append(text)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = None
        if outcome is not None:
            raise outcome


def make_store(tmp_path) -> Store:
    store = Store(tmp_path / "agent.db")
    store.init_schema()
    return store


def test_successful_dispatch_records_delivered_and_latency(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel("console")
    dispatcher = AlertDispatcher([channel], store, sleep=no_sleep)
    post = make_post()
    store.upsert_post(post)

    results = dispatcher.dispatch(post, make_detection())

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].attempts == 1
    assert len(channel.sent) == 1

    row = store._conn.execute(
        "SELECT status FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "delivered"

    latency_row = store._conn.execute(
        "SELECT * FROM latency WHERE post_id=?", (post.id,)
    ).fetchone()
    assert latency_row is not None
    assert latency_row["delivered_at"] is not None
    store.close()


def test_dispatching_same_post_twice_sends_only_once(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel("console")
    dispatcher = AlertDispatcher([channel], store, sleep=no_sleep)
    post = make_post()
    store.upsert_post(post)

    first = dispatcher.dispatch(post, make_detection())
    second = dispatcher.dispatch(post, make_detection())

    assert len(first) == 1
    assert len(second) == 0
    assert len(channel.sent) == 1
    store.close()


def test_idempotency_holds_across_new_store_on_same_file(tmp_path):
    db_path = tmp_path / "agent.db"
    post = make_post()

    store = Store(db_path)
    store.init_schema()
    channel = FakeChannel("console")
    dispatcher = AlertDispatcher([channel], store, sleep=no_sleep)
    store.upsert_post(post)
    first = dispatcher.dispatch(post, make_detection())
    assert len(first) == 1
    store.close()

    # Simulate a process restart: brand new Store object, same file, brand
    # new dispatcher and channel instance too.
    reopened_store = Store(db_path)
    reopened_store.init_schema()
    reopened_channel = FakeChannel("console")
    reopened_dispatcher = AlertDispatcher([reopened_channel], reopened_store, sleep=no_sleep)

    second = reopened_dispatcher.dispatch(post, make_detection())

    assert len(second) == 0
    assert reopened_channel.sent == []
    reopened_store.close()


def test_channel_fails_twice_then_succeeds_delivers_once(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel(
        "console",
        outcomes=[TransientSourceError("temporary"), TransientSourceError("temporary"), None],
    )
    dispatcher = AlertDispatcher([channel], store, max_attempts=4, sleep=no_sleep)
    post = make_post()
    store.upsert_post(post)

    results = dispatcher.dispatch(post, make_detection())

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].attempts == 3
    assert len(channel.sent) == 3

    delivered_count = store._conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE post_id=? AND channel=? AND status='delivered'",
        (post.id, "console"),
    ).fetchone()[0]
    assert delivered_count == 1
    store.close()


def test_permanent_error_is_not_retried(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel("console", outcomes=[PermanentSourceError("bad request")])
    dispatcher = AlertDispatcher([channel], store, max_attempts=4, sleep=no_sleep)
    post = make_post()
    store.upsert_post(post)

    results = dispatcher.dispatch(post, make_detection())

    assert results[0].ok is False
    assert results[0].attempts == 1
    assert len(channel.sent) == 1

    row = store._conn.execute(
        "SELECT status, attempts FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "failed"
    store.close()


def test_retry_failed_recovers_a_stuck_alert(tmp_path):
    db_path = tmp_path / "agent.db"
    store = Store(db_path)
    store.init_schema()
    post = make_post()
    store.upsert_post(post)
    detection = make_detection()
    store.save_detection(detection)

    # Every attempt in the original dispatch fails, so the claim row is
    # left behind with status='failed'. Without retry_failed nothing would
    # ever revisit it, since claim_alert would refuse a second dispatch.
    failing_channel = FakeChannel(
        "console",
        outcomes=[TransientSourceError("down")] * 4,
    )
    dispatcher = AlertDispatcher([failing_channel], store, max_attempts=4, sleep=no_sleep)
    first = dispatcher.dispatch(post, detection)
    assert first[0].ok is False

    second_attempt = dispatcher.dispatch(post, detection)
    assert second_attempt == []  # claim_alert blocks it, proving the stuck state

    # Swap in a channel with the same name that now succeeds, exactly what
    # "the outage is over" looks like.
    dispatcher.channels = [FakeChannel("console")]
    retried = dispatcher.retry_failed()

    assert len(retried) == 1
    assert retried[0].ok is True

    row = store._conn.execute(
        "SELECT status FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "delivered"
    store.close()


def test_retry_failed_skips_alert_already_at_max_attempts(tmp_path):
    store = make_store(tmp_path)
    post = make_post()
    store.upsert_post(post)
    store.save_detection(make_detection())

    store.claim_alert(post.id, "console")
    store.record_alert_result(post.id, "console", "failed", "boom")
    store.record_alert_result(post.id, "console", "failed", "boom")

    channel = FakeChannel("console")
    dispatcher = AlertDispatcher([channel], store, max_attempts=2, sleep=no_sleep)

    results = dispatcher.retry_failed()

    assert results == []
    assert channel.sent == []
    store.close()


def test_unconfigured_channel_is_skipped_entirely(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel("telegram", configured=False)
    dispatcher = AlertDispatcher([channel], store, sleep=no_sleep)
    post = make_post()
    store.upsert_post(post)

    results = dispatcher.dispatch(post, make_detection())

    assert results == []
    assert channel.sent == []
    row = store._conn.execute(
        "SELECT * FROM alerts WHERE post_id=? AND channel=?", (post.id, "telegram")
    ).fetchone()
    assert row is None
    store.close()


def test_dispatch_ops_does_not_consume_a_claim(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel("console")
    dispatcher = AlertDispatcher([channel], store, sleep=no_sleep)

    first = dispatcher.dispatch_ops("no_new_posts", "quiet for 12 hours")
    second = dispatcher.dispatch_ops("no_new_posts", "quiet for 12 hours")

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].ok is True
    assert second[0].ok is True
    assert len(channel.sent) == 2

    count = store._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert count == 0
    store.close()
