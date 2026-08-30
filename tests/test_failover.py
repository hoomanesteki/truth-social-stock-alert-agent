from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from tsalert.models import Post
from tsalert.reliability import CircuitBreaker
from tsalert.sources.base import SourceHealth, TransientSourceError
from tsalert.sources.failover import FailoverSource


def no_sleep(_seconds: float) -> None:
    """The failover retries the primary internally now, so without this the
    suite spends real seconds backing off."""



def _post(post_id: str) -> Post:
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    return Post(
        id=post_id,
        account="realDonaldTrump",
        created_at=now,
        text="hello",
        url="https://truthsocial.com/x",
        raw_html="<p>hello</p>",
        is_reply=False,
        is_repost=False,
        is_quote=False,
        has_media=False,
        source="fake",
        fetched_at=now,
    )


class FakeSource:
    def __init__(self, name, posts=None, raises=None):
        self.name = name
        self.posts = posts or []
        self.raises = raises
        self.calls = 0

    def fetch_latest(self, since_id=None, limit=20):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.posts

    def fetch_history(self, before_id=None, limit=20):
        return self.fetch_latest(since_id=before_id, limit=limit)

    def health(self):
        return SourceHealth(ok=self.raises is None, last_success=None, detail="fake")


def test_primary_success_stays_on_primary():
    primary = FakeSource("primary", posts=[_post("1")])
    fallback = FakeSource("fallback", posts=[_post("2")])
    failover = FailoverSource(primary, fallback, sleep=no_sleep)

    posts = failover.fetch_latest()

    assert posts == primary.posts
    assert failover.active_source_name == "primary"
    assert fallback.calls == 0


def test_single_failure_below_threshold_propagates_and_stays_on_primary():
    primary = FakeSource("primary", raises=TransientSourceError("boom"))
    fallback = FakeSource("fallback", posts=[_post("2")])
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=300, clock=lambda: 0.0)
    failover = FailoverSource(primary, fallback, breaker=breaker, sleep=no_sleep)

    with pytest.raises(TransientSourceError):
        failover.fetch_latest()

    assert failover.active_source_name == "primary"
    assert fallback.calls == 0


def test_primary_failure_falls_through_to_fallback_once_breaker_opens(caplog):
    primary = FakeSource("primary", raises=TransientSourceError("boom"))
    fallback = FakeSource("fallback", posts=[_post("2")])
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=300, clock=lambda: 0.0)
    failover = FailoverSource(primary, fallback, breaker=breaker, sleep=no_sleep)

    with pytest.raises(TransientSourceError):
        failover.fetch_latest()
    assert failover.active_source_name == "primary"

    with caplog.at_level(logging.WARNING):
        posts = failover.fetch_latest()

    assert posts == fallback.posts
    assert failover.active_source_name == "fallback"
    assert failover.last_transition is not None
    assert failover.last_transition.from_source == "primary"
    assert failover.last_transition.to_source == "fallback"
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("primary" in msg and "fallback" in msg for msg in warning_messages)


def test_primary_retried_and_recovers_after_cooldown():
    primary = FakeSource("primary", raises=TransientSourceError("boom"))
    fallback = FakeSource("fallback", posts=[_post("2")])
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=100, clock=lambda: clock["t"])
    failover = FailoverSource(primary, fallback, breaker=breaker, sleep=no_sleep)

    # threshold=1: the first failure trips the breaker open immediately, and
    # this same call is served from fallback rather than raised to the caller.
    posts = failover.fetch_latest()
    assert posts == fallback.posts
    assert failover.active_source_name == "fallback"

    # Still within cooldown: served from fallback, primary not retried.
    clock["t"] = 50.0
    calls_before = primary.calls
    posts = failover.fetch_latest()
    assert posts == fallback.posts
    assert primary.calls == calls_before
    assert failover.active_source_name == "fallback"

    # Cooldown elapsed: primary gets a fresh probe and recovers.
    clock["t"] = 200.0
    primary.raises = None
    primary.posts = [_post("3")]
    posts = failover.fetch_latest()

    assert posts == primary.posts
    assert failover.active_source_name == "primary"
    assert failover.last_transition.to_source == "primary"


def test_the_breaker_counts_polls_not_retry_attempts():
    """One blip must not demote ingestion to the mirror.

    The runner used to wrap this source in with_retries(attempts=3), and
    every attempt re-entered _call and ticked the breaker, so a single poll
    spent the whole threshold and failed over inside it. A six second network
    hiccup then cost the full cooldown on the slower mirror, and the
    documented "three consecutive failures" meant three retries rather than
    the three polls it reads as. Retrying inside this class restores that.
    """
    primary = FakeSource("truthsocial_api", raises=TransientSourceError("blip"))
    fallback = FakeSource("trumpstruth_rss", posts=[_post("1")])
    failover = FailoverSource(primary, fallback, sleep=no_sleep)

    for _ in range(2):
        with pytest.raises(TransientSourceError):
            failover.fetch_latest()
        assert failover.active_source_name == primary.name

    assert failover.fetch_latest()[0].id == "1"
    assert failover.active_source_name == fallback.name
