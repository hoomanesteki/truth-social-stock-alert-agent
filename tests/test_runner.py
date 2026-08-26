from __future__ import annotations

import json
from pathlib import Path

from tsalert.detect.lexicon import Lexicon
from tsalert.detect.rules import RuleDetector
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


def no_sleep(_seconds: float) -> None:
    pass


class FakeDispatcher:
    """Records dispatch and dispatch_ops calls. Never sends anything for real."""

    def __init__(self) -> None:
        self.dispatched: list[tuple] = []
        self.ops: list[tuple[str, str]] = []
        self.retry_failed_calls = 0

    def retry_failed(self):
        self.retry_failed_calls += 1
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
    assert len(dispatcher.dispatched) == len(DEMO_IDS)
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
    assert len(dispatcher.dispatched) == len(DEMO_IDS)  # unchanged by the second poll
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
    assert len(dispatcher.dispatched) == len(DEMO_IDS)  # only the stock related ones dispatch

    dispatched_ids = {post.id for post, _detection in dispatcher.dispatched}
    assert dispatched_ids == DEMO_IDS
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
