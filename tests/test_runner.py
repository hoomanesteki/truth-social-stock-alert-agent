from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from tsalert.alerts.dispatcher import AlertDispatcher
from tsalert.detect.lexicon import Lexicon
from tsalert.detect.rules import RuleDetector
from tsalert.models import Detection, Post, TickerMention
from tsalert.monitor import HealthMonitor
from tsalert.reliability import AdaptiveInterval
from tsalert.runner import AgentRunner
from tsalert.sources.base import PermanentSourceError, TransientSourceError
from tsalert.sources.fixture import FixtureSource
from tsalert.store import Store

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DEMO_FIXTURE = FIXTURES_DIR / "demo_statuses.json"
PAGE1_FIXTURE = FIXTURES_DIR / "statuses_page1.json"
LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "lexicon" / "tickers.csv"

# Every post in demo_statuses.json trips the real rule detector (confirmed by
# running RuleDetector over the fixture); every post in statuses_page1.json
# does not. That split is exactly what the "only stock related posts are
# dispatched" test needs, using the real detector rather than a stub.
DEMO_IDS = {status["id"] for status in json.loads(DEMO_FIXTURE.read_text())}

# Four of the eight demo posts name a company the detector should report. The other
# four mention a company word in a non company sense (Intel as intelligence, ABC News
# and Fox News as broadcasters he is appearing on or criticising, New York Times as a
# bestseller list) and are deliberately suppressed. Listing the ids here rather than
# asking the detector keeps the assertion meaningful instead of tautological.
DEMO_STOCK_IDS = {
    "116994500400281844",  # NVIDIA building in America
    "117031897808226413",  # Chevron CEO interview
    "117074526504264990",  # a list of corporate donors
    "117085013643913800",  # Walmart named as a former employer
}


def no_sleep(_seconds: float) -> None:
    pass


class FakeDispatcher:
    """Records dispatch and dispatch_ops calls. Never sends anything for real."""

    def __init__(self) -> None:
        self.dispatched: list[tuple] = []
        self.ops: list[tuple[str, str]] = []
        self.retry_failed_calls = 0
        self.recover_undelivered_calls = 0

    def retry_failed(self):
        self.retry_failed_calls += 1
        return []

    def recover_undelivered(self):
        self.recover_undelivered_calls += 1
        return []

    def dispatch(self, post, detection):
        self.dispatched.append((post, detection))
        return []

    def dispatch_ops(self, name: str, detail: str):
        self.ops.append((name, detail))
        return []


class RaisingSource:
    """A source whose fetch_latest always raises a scripted exception."""

    def __init__(self, exc: Exception) -> None:
        self.name = "raising"
        self._exc = exc

    def fetch_latest(self, since_id=None, limit: int = 20):
        raise self._exc

    def fetch_history(self, before_id=None, limit: int = 20):
        return []


class MergingSource:
    """Merges fetch_latest results from several sources with no page cap.

    FixtureSource.fetch_latest defaults to a 20 post page, same as the real
    endpoint's pagination. Combining two fixture files while keeping every
    post (not just the newest 20 across the pair) means asking each
    underlying source for everything (limit=None) and merging ourselves.
    """

    def __init__(self, sources) -> None:
        self.name = "merging"
        self._sources = sources

    def fetch_latest(self, since_id=None, limit: int = 20):
        combined = []
        for source in self._sources:
            combined.extend(source.fetch_latest(since_id=since_id, limit=None))
        combined.sort(key=lambda p: int(p.id))
        return combined

    def fetch_history(self, before_id=None, limit: int = 20):
        return []


class EmptySource:
    """A source with nothing new to fetch, ever. Isolates the recovery pass
    from any fresh-post handling in poll_once.
    """

    def __init__(self) -> None:
        self.name = "empty"

    def fetch_latest(self, since_id=None, limit: int = 20):
        return []

    def fetch_history(self, before_id=None, limit: int = 20):
        return []


class FixedPagesSource:
    """Returns one scripted page of posts per fetch_latest call, ignoring
    since_id entirely. Used to feed poll_once a non numeric id without a
    real source's own id handling getting in the way.
    """

    def __init__(self, pages: list[list[Post]]) -> None:
        self.name = "fixed"
        self._pages = list(pages)

    def fetch_latest(self, since_id=None, limit: int = 20):
        return self._pages.pop(0) if self._pages else []

    def fetch_history(self, before_id=None, limit: int = 20):
        return []


class RecordingChannel:
    """A real AlertChannel that always succeeds and records what it sent."""

    def __init__(self, name: str = "console") -> None:
        self.name = name
        self.sent: list[str] = []

    def is_configured(self) -> bool:
        return True

    def send(self, text: str) -> None:
        self.sent.append(text)


def make_plain_post(post_id: str, text: str = "some post text") -> Post:
    now = datetime(2026, 8, 25, 18, 0, 0, tzinfo=timezone.utc)
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


class CountingSource:
    """Wraps a real source and counts fetch_latest calls."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.name = inner.name
        self.calls = 0

    def fetch_latest(self, since_id=None, limit: int = 20):
        self.calls += 1
        return self.inner.fetch_latest(since_id=since_id, limit=limit)

    def fetch_history(self, before_id=None, limit: int = 20):
        return self.inner.fetch_history(before_id=before_id, limit=limit)


def make_store(tmp_path) -> Store:
    store = Store(tmp_path / "agent.db")
    store.init_schema()
    return store


def make_detector() -> RuleDetector:
    return RuleDetector(Lexicon.load(LEXICON_PATH))


def make_runner(source, dispatcher, store, sleep=no_sleep) -> AgentRunner:
    return AgentRunner(
        source=source,
        detector=make_detector(),
        dispatcher=dispatcher,
        store=store,
        monitor=HealthMonitor(store),
        interval=AdaptiveInterval(),
        sleep=sleep,
    )


def test_poll_once_processes_only_new_posts(tmp_path):
    store = make_store(tmp_path)
    source = FixtureSource([DEMO_FIXTURE])
    dispatcher = FakeDispatcher()
    runner = make_runner(source, dispatcher, store)

    new_count = runner.poll_once()

    assert new_count == len(DEMO_IDS)
    assert len(dispatcher.dispatched) == len(DEMO_STOCK_IDS)
    store.close()


def test_poll_once_twice_over_same_data_dispatches_nothing_second_time(tmp_path):
    store = make_store(tmp_path)
    source = FixtureSource([DEMO_FIXTURE])
    dispatcher = FakeDispatcher()
    runner = make_runner(source, dispatcher, store)

    first_count = runner.poll_once()
    second_count = runner.poll_once()

    assert first_count == len(DEMO_IDS)
    assert second_count == 0
    assert len(dispatcher.dispatched) == len(DEMO_STOCK_IDS)  # unchanged by the second poll
    store.close()


def test_only_stock_related_posts_are_dispatched(tmp_path):
    store = make_store(tmp_path)
    # Mix stock related posts (demo) with plainly unrelated ones (page1).
    source = MergingSource([FixtureSource([DEMO_FIXTURE]), FixtureSource([PAGE1_FIXTURE])])
    dispatcher = FakeDispatcher()
    runner = make_runner(source, dispatcher, store)

    total_pages = len(DEMO_IDS) + len(json.loads(PAGE1_FIXTURE.read_text()))
    new_count = runner.poll_once()

    assert new_count == total_pages  # every post is new and gets processed
    # The demo posts that name a company, plus one page1 post naming S&P Global.
    # Expanding the lexicon to the S&P 500 is what made that one reachable.
    assert len(dispatcher.dispatched) == len(DEMO_STOCK_IDS) + 1

    dispatched_ids = {post.id for post, _detection in dispatcher.dispatched}
    assert DEMO_STOCK_IDS <= dispatched_ids
    assert dispatched_ids - DEMO_STOCK_IDS == {"117156902282061140"}  # S&P Global
    assert all(detection.is_stock_related for _post, detection in dispatcher.dispatched)
    store.close()


def test_transient_source_error_does_not_propagate_and_records_failed_poll(tmp_path):
    store = make_store(tmp_path)
    dispatcher = FakeDispatcher()
    monitor = HealthMonitor(store)
    runner = AgentRunner(
        source=RaisingSource(TransientSourceError("temporary glitch")),
        detector=make_detector(),
        dispatcher=dispatcher,
        store=store,
        monitor=monitor,
        interval=AdaptiveInterval(),
        sleep=no_sleep,
    )

    result = runner.poll_once()

    assert result == 0
    assert dispatcher.dispatched == []
    assert dispatcher.ops == []  # a transient error does not raise an ops alarm by itself
    assert monitor.status()["consecutive_errors"] == 1
    store.close()


def test_permanent_source_error_does_not_kill_loop_and_produces_ops_alarm(tmp_path):
    store = make_store(tmp_path)
    dispatcher = FakeDispatcher()
    monitor = HealthMonitor(store)
    runner = AgentRunner(
        source=RaisingSource(PermanentSourceError("endpoint gone")),
        detector=make_detector(),
        dispatcher=dispatcher,
        store=store,
        monitor=monitor,
        interval=AdaptiveInterval(),
        sleep=no_sleep,
    )

    # run() over several iterations proves a permanent error does not stop
    # the loop, it just keeps reporting the same failure every poll.
    runner.run(max_iterations=2)

    assert dispatcher.dispatched == []
    assert dispatcher.ops == [
        ("permanent_source_error", "endpoint gone"),
        ("permanent_source_error", "endpoint gone"),
    ]
    assert monitor.status()["consecutive_errors"] == 2
    store.close()


def test_last_seen_post_id_advances_and_is_persisted(tmp_path):
    db_path = tmp_path / "agent.db"
    store = Store(db_path)
    store.init_schema()
    source = FixtureSource([PAGE1_FIXTURE])
    dispatcher = FakeDispatcher()
    runner = make_runner(source, dispatcher, store)

    runner.poll_once()

    max_id = str(max(int(status["id"]) for status in json.loads(PAGE1_FIXTURE.read_text())))
    assert store.get_state("last_seen_post_id") == max_id
    store.close()

    # Simulate a process restart: brand new Store object, same file on disk.
    reopened_store = Store(db_path)
    reopened_store.init_schema()
    assert reopened_store.get_state("last_seen_post_id") == max_id
    reopened_store.close()


def test_alert_with_no_alerts_row_is_recovered_and_delivered_on_next_poll(tmp_path):
    """Reproduces defect 1 variant b: a crash before dispatch is ever
    reached. The post is stored and detected as stock related, but no
    alerts row exists at all, because nothing revisits it once upsert_post
    stops returning is_new. Recovery must find and deliver it on the very
    next poll_once, with no fresh post arriving from the source.
    """
    store = make_store(tmp_path)
    post = make_plain_post("999")
    store.upsert_post(post)
    mention = TickerMention(
        ticker="DJT", company="Trump Media", matched_text="$DJT", method="cashtag", confidence=0.9
    )
    store.save_detection(
        Detection(post_id=post.id, is_stock_related=True, mentions=(mention,), detector="rules", latency_ms=1.0)
    )
    row = store._conn.execute("SELECT COUNT(*) FROM alerts WHERE post_id=?", (post.id,)).fetchone()
    assert row[0] == 0  # simulating the crash: dedup knows the post, delivery has no record of it

    channel = RecordingChannel("console")
    dispatcher = AlertDispatcher([channel], store, sleep=no_sleep)
    runner = make_runner(EmptySource(), dispatcher, store)

    new_count = runner.poll_once()

    assert new_count == 0  # the source had nothing new, recovery is the only thing that ran
    assert len(channel.sent) == 1
    delivered = store._conn.execute(
        "SELECT status FROM alerts WHERE post_id=? AND channel='console'", (post.id,)
    ).fetchone()
    assert delivered["status"] == "delivered"
    store.close()


def test_non_numeric_post_id_does_not_crash_and_does_not_poison_next_poll(tmp_path):
    store = make_store(tmp_path)
    source = FixedPagesSource(
        [
            [make_plain_post("abcguid-not-numeric")],
            [make_plain_post("500")],
        ]
    )
    dispatcher = FakeDispatcher()
    runner = make_runner(source, dispatcher, store)

    first = runner.poll_once()
    assert first == 1
    assert store.get_state("last_seen_post_id") == "abcguid-not-numeric"

    # The original defect: the second poll's `int(post.id) > int(last_id)`
    # comparison raised ValueError on the non numeric last_id and killed
    # the process. It must not raise here, and the poll must complete.
    second = runner.poll_once()
    assert second == 1
    store.close()


def test_run_with_max_iterations_performs_exactly_that_many_polls(tmp_path):
    store = make_store(tmp_path)
    source = CountingSource(FixtureSource([PAGE1_FIXTURE]))
    dispatcher = FakeDispatcher()
    sleep_calls: list[float] = []
    runner = make_runner(source, dispatcher, store, sleep=sleep_calls.append)

    runner.run(max_iterations=3)

    assert source.calls == 3
    # No sleep after the final bounded iteration, so 3 polls means 2 sleeps.
    assert len(sleep_calls) == 2
    store.close()


def test_polling_cursor_is_kept_per_account(tmp_path):
    """Two accounts in one store must not overwrite each other's high water mark."""
    from tsalert.runner import _last_seen_key

    assert _last_seen_key("") == "last_seen_post_id"
    assert _last_seen_key("alice") != _last_seen_key("bob")

    with Store(tmp_path / "multi.db") as store:
        store.set_state(_last_seen_key("alice"), "1000")
        store.set_state(_last_seen_key("bob"), "9999")
        assert store.get_state(_last_seen_key("alice")) == "1000"
        assert store.get_state(_last_seen_key("bob")) == "9999"



def test_backlog_recovery_does_not_stamp_latency(tmp_path):
    """Archive posts recovered later must stay out of the latency table.

    They were ingested by an earlier run or by the backfill script, so
    publish to fetch on them measures how old the archive is. A few month
    old posts bury every real reading.
    """
    from datetime import timedelta

    old = datetime.now(timezone.utc) - timedelta(days=44)
    post = make_plain_post("7001", "just some words")
    post = replace(post, created_at=old, fetched_at=old)

    store = make_store(tmp_path)
    store.upsert_post(post)
    runner = make_runner(EmptySource(), FakeDispatcher(), store)
    runner.poll_once()

    rows = store._conn.execute("SELECT COUNT(*) FROM latency").fetchone()[0]
    store.close()
    assert rows == 0



def test_cursor_written_before_namespacing_is_carried_forward(tmp_path):
    """Upgrading must not orphan the polling cursor.

    Namespacing the key per account left older databases pointing at a key
    nothing reads, so the agent restarted from the top of the timeline and
    re-fetched weeks of posts. Found by upgrading a live database.
    """
    from tsalert.runner import _LAST_SEEN_KEY

    class RecordingSource(EmptySource):
        def __init__(self):
            self.since_ids = []

        def fetch_latest(self, since_id=None, limit: int = 20):
            self.since_ids.append(since_id)
            return []

    store = make_store(tmp_path)
    store.set_state(_LAST_SEEN_KEY, "117157578446470768")

    source = RecordingSource()
    runner = make_runner(source, FakeDispatcher(), store)
    runner.account = "realDonaldTrump"
    runner.poll_once()

    assert source.since_ids[-1] == "117157578446470768"
    assert store.get_state("last_seen_post_id:realDonaldTrump") == "117157578446470768"
    store.close()


def test_retry_after_overrides_the_adaptive_interval(tmp_path):
    """A 429 that outlasts the retry budget must set the next poll delay.

    with_retries honours Retry-After inside one poll. This covers the other
    half: when the wait is longer than the budget, the error escapes and the
    loop has to wait rather than going straight back at the usual interval.
    """
    class RateLimited:
        name = "rate-limited"

        def fetch_latest(self, since_id=None, limit: int = 20):
            err = TransientSourceError("rate limited (429)")
            err.retry_after = 900
            raise err

        def fetch_history(self, before_id=None, limit: int = 20):
            return []

        def health(self):
            return SourceHealth(ok=True, last_success=None, detail="")

    slept: list[float] = []
    store = make_store(tmp_path)
    runner = make_runner(RateLimited(), FakeDispatcher(), store, sleep=slept.append)
    runner.run(max_iterations=2)
    store.close()

    assert max(slept) >= 900
