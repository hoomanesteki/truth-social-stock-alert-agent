from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tsalert.monitor import HealthMonitor
from tsalert.store import Store


def make_store(tmp_path) -> Store:
    store = Store(tmp_path / "agent.db")
    store.init_schema()
    return store


class FakeClock:
    """Manually advanced clock so staleness tests never depend on real time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)

    def __call__(self) -> datetime:
        return self._now


def alarm_names(alarms) -> set[str]:
    return {a.name for a in alarms}


def test_no_successful_poll_fires_once_stale_but_not_before(tmp_path):
    store = make_store(tmp_path)
    clock = FakeClock(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    monitor = HealthMonitor(store, stale_minutes=15, clock=clock)

    monitor.record_poll(ok=True, new_posts=1)

    clock.advance(minutes=14)
    assert "no_successful_poll" not in alarm_names(monitor.check())

    clock.advance(minutes=2)  # 16 minutes total, past the 15 minute threshold
    assert "no_successful_poll" in alarm_names(monitor.check())
    store.close()


def test_no_new_posts_fires_once_stale_but_not_before(tmp_path):
    """The silent breakage signal: polls and parsing can keep succeeding while
    no new post ever shows up, for example because the endpoint quietly
    changed shape. This alarm is what notices that, driven only by the
    injected clock, never by wall clock time.
    """
    store = make_store(tmp_path)
    clock = FakeClock(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    monitor = HealthMonitor(store, no_posts_hours=1, clock=clock)

    monitor.record_poll(ok=True, new_posts=1)

    clock.advance(minutes=59)
    monitor.record_poll(ok=True, new_posts=0)
    assert "no_new_posts" not in alarm_names(monitor.check())

    clock.advance(minutes=2)  # 61 minutes since the last new post
    monitor.record_poll(ok=True, new_posts=0)
    assert "no_new_posts" in alarm_names(monitor.check())
    store.close()


def test_no_new_posts_fires_on_cold_start_with_only_empty_polls(tmp_path):
    """The exact case the alarm exists for: a fresh store, ingestion broken
    from the very first poll (HTTP 200 with an empty list). No post has
    ever been seen, so last_new_post is unset forever unless the staleness
    clock falls back to the first successful poll instead of staying blind.
    """
    store = make_store(tmp_path)
    clock = FakeClock(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    monitor = HealthMonitor(store, no_posts_hours=12, clock=clock)

    monitor.record_poll(ok=True, new_posts=0)
    assert "no_new_posts" not in alarm_names(monitor.check())

    for _ in range(16):
        clock.advance(hours=1)
        monitor.record_poll(ok=True, new_posts=0)

    assert "no_new_posts" in alarm_names(monitor.check())
    store.close()


def test_repeated_errors_fires_at_threshold_not_one_below(tmp_path):
    store = make_store(tmp_path)
    clock = FakeClock(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    monitor = HealthMonitor(store, error_threshold=3, clock=clock)

    monitor.record_poll(ok=False, new_posts=0)
    monitor.record_poll(ok=False, new_posts=0)
    assert "repeated_errors" not in alarm_names(monitor.check())

    monitor.record_poll(ok=False, new_posts=0)
    assert "repeated_errors" in alarm_names(monitor.check())
    store.close()


def test_alarm_does_not_refire_within_min_repeat_minutes(tmp_path):
    store = make_store(tmp_path)
    clock = FakeClock(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    monitor = HealthMonitor(store, error_threshold=1, min_repeat_minutes=60, clock=clock)

    monitor.record_poll(ok=False, new_posts=0)
    assert "repeated_errors" in alarm_names(monitor.check())

    clock.advance(minutes=30)
    monitor.record_poll(ok=False, new_posts=0)
    assert "repeated_errors" not in alarm_names(monitor.check())  # still suppressed

    clock.advance(minutes=31)  # 61 minutes since the alarm first fired
    monitor.record_poll(ok=False, new_posts=0)
    assert "repeated_errors" in alarm_names(monitor.check())  # allowed to fire again
    store.close()


def test_alarm_state_survives_new_monitor_on_same_store(tmp_path):
    """A restart must not hide an ongoing outage: neither the staleness clock
    nor alarm suppression may reset just because a new HealthMonitor object
    was built on top of the same store.
    """
    db_path = tmp_path / "agent.db"
    store = Store(db_path)
    store.init_schema()

    clock = FakeClock(datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc))
    monitor = HealthMonitor(store, stale_minutes=15, min_repeat_minutes=60, clock=clock)
    monitor.record_poll(ok=True, new_posts=1)

    clock.advance(minutes=20)  # past the 15 minute staleness threshold
    assert "no_successful_poll" in alarm_names(monitor.check())

    clock.advance(minutes=10)  # 30 minutes since the outage started

    # Simulate a process restart: a brand new HealthMonitor on the same
    # store. The clock keeps moving, since real time did not stop.
    restarted_monitor = HealthMonitor(store, stale_minutes=15, min_repeat_minutes=60, clock=clock)

    # The staleness clock was not reset by building a new HealthMonitor.
    assert (
        restarted_monitor.status()["last_successful_poll_at"]
        == monitor.status()["last_successful_poll_at"]
    )

    # Still within the suppression window from the first fire, so no repeat.
    assert "no_successful_poll" not in alarm_names(restarted_monitor.check())

    clock.advance(minutes=55)  # 85 minutes since the outage started
    assert "no_successful_poll" in alarm_names(restarted_monitor.check())
    store.close()
