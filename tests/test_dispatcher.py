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
    # 'permanent_failure' is a distinct terminal status from 'failed': a 4xx
    # cannot succeed by retrying, so retry_failed must never select this row.
    assert row["status"] == "permanent_failure"
    store.close()


def test_retry_failed_recovers_a_stuck_alert(tmp_path):
    db_path = tmp_path / "agent.db"
    store = Store(db_path)
    store.init_schema()
    post = make_post()
    store.upsert_post(post)
    detection = make_detection()
    store.save_detection(detection)

    # The original dispatch burns the entire per-call attempts budget on a
    # channel that is transiently down the whole time, no permanent error
    # anywhere in the script. This is exactly the case retry_failed exists
    # for: the channel could simply come back later, so the failure must
    # stay eligible for retry rather than being written off.
    failing_channel = FakeChannel(
        "console",
        outcomes=[
            TransientSourceError("down"),
            TransientSourceError("down"),
            TransientSourceError("down"),
            TransientSourceError("down"),
        ],
    )
    dispatcher = AlertDispatcher([failing_channel], store, max_attempts=4, sleep=no_sleep)
    first = dispatcher.dispatch(post, detection)
    assert first[0].ok is False
    assert first[0].attempts == 4

    row = store._conn.execute(
        "SELECT status, cycles FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "failed"  # transient, so still eligible for retry_failed
    assert row["cycles"] == 0  # burning the per-call attempts budget is not a retry_failed pass

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


def test_retry_failed_skips_a_permanent_failure(tmp_path):
    store = make_store(tmp_path)
    post = make_post()
    store.upsert_post(post)
    store.save_detection(make_detection())

    channel = FakeChannel("console", outcomes=[PermanentSourceError("bad chat id")])
    dispatcher = AlertDispatcher([channel], store, max_attempts=4, sleep=no_sleep)
    dispatcher.dispatch(post, make_detection())

    row = store._conn.execute(
        "SELECT status FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "permanent_failure"

    results = dispatcher.retry_failed()

    assert results == []
    assert len(channel.sent) == 1  # only the original send, retry_failed sent nothing
    store.close()


def test_permanent_failure_is_never_retried_however_many_cycles_pass(tmp_path):
    store = make_store(tmp_path)
    post = make_post()
    store.upsert_post(post)
    store.save_detection(make_detection())

    channel = FakeChannel("console", outcomes=[PermanentSourceError("bad chat id")])
    dispatcher = AlertDispatcher([channel], store, max_attempts=4, max_cycles=3, sleep=no_sleep)
    dispatcher.dispatch(post, make_detection())

    # max_cycles is only 3 here, well under the 10 passes run below. A row
    # excluded only because it happened to still be under its cycle budget
    # would eventually get picked up; this proves the exclusion is by
    # status ('permanent_failure' is simply never selected), not by a
    # cycle count that would run out sooner or later.
    for _ in range(10):
        assert dispatcher.retry_failed() == []
    assert len(channel.sent) == 1
    store.close()


def test_retry_failed_picks_up_a_row_stuck_at_pending(tmp_path):
    store = make_store(tmp_path)
    post = make_post()
    store.upsert_post(post)
    store.save_detection(make_detection())

    # claim_alert with nothing following it is exactly what a crash between
    # claim and record_alert_result leaves behind: a row that is claimed
    # but never resolved either way.
    store.claim_alert(post.id, "console")
    row = store._conn.execute(
        "SELECT status FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "pending"

    channel = FakeChannel("console")
    dispatcher = AlertDispatcher([channel], store, max_attempts=4, sleep=no_sleep)

    results = dispatcher.retry_failed()

    assert len(results) == 1
    assert results[0].ok is True
    assert len(channel.sent) == 1

    row = store._conn.execute(
        "SELECT status FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["status"] == "delivered"
    store.close()


def test_record_alert_result_stores_the_real_number_of_sends_attempted(tmp_path):
    store = make_store(tmp_path)
    channel = FakeChannel(
        "console",
        outcomes=[TransientSourceError("temporary"), TransientSourceError("temporary"), None],
    )
    dispatcher = AlertDispatcher([channel], store, max_attempts=4, sleep=no_sleep)
    post = make_post()
    store.upsert_post(post)

    dispatcher.dispatch(post, make_detection())

    # Three real sends happened (two failures then a success), so the
    # stored counter must read 3, not 1. Storing 1 per call is what let
    # attempts < max_attempts bound poll cycles instead of real sends.
    row = store._conn.execute(
        "SELECT attempts FROM alerts WHERE post_id=? AND channel=?", (post.id, "console")
    ).fetchone()
    assert row["attempts"] == 3
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



def test_one_failing_channel_does_not_stop_the_other(tmp_path):
    """A dead Telegram must not cost you the console alert.

    Every other dispatcher test here uses a single channel, so the per
    channel isolation the README promises was never actually exercised.
    """
    post = make_post()
    detection = make_detection()
    dead = FakeChannel("telegram", outcomes=[PermanentSourceError("bad chat id")])
    good = FakeChannel("console")

    with Store(tmp_path / "two.db") as store:
        store.upsert_post(post)
        dispatcher = AlertDispatcher([dead, good], store, sleep=lambda _s: None)
        results = dispatcher.dispatch(post, detection)

        by_channel = {r.channel: r for r in results}
        assert by_channel["telegram"].ok is False
        assert by_channel["console"].ok is True
        assert len(good.sent) == 1

        rows = dict(
            store._conn.execute(
                "SELECT channel, status FROM alerts WHERE post_id=?", (post.id,)
            ).fetchall()
        )
        assert rows["console"] == "delivered"
        assert rows["telegram"] != "delivered"
