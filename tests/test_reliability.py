from __future__ import annotations

import random

import pytest

from tsalert.reliability import AdaptiveInterval, CircuitBreaker, with_retries
from tsalert.sources.base import PermanentSourceError, TransientSourceError


def test_with_retries_retries_up_to_attempts_then_raises():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        raise TransientSourceError("boom")

    with pytest.raises(TransientSourceError):
        with_retries(flaky, attempts=3, sleep=sleeps.append, rng=random.Random(0))

    assert calls["n"] == 3
    assert len(sleeps) == 2  # sleeps between attempts, not after the last one


def test_with_retries_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransientSourceError("boom")
        return "ok"

    result = with_retries(flaky, attempts=3, sleep=lambda s: None, rng=random.Random(0))
    assert result == "ok"
    assert calls["n"] == 2


def test_with_retries_never_retries_permanent():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise PermanentSourceError("nope")

    with pytest.raises(PermanentSourceError):
        with_retries(fn, attempts=5, sleep=lambda s: None)

    assert calls["n"] == 1


def test_with_retries_honors_retry_after():
    sleeps: list[float] = []

    def fn():
        exc = TransientSourceError("rate limited")
        exc.retry_after = 12.5
        raise exc

    with pytest.raises(TransientSourceError):
        with_retries(fn, attempts=2, sleep=sleeps.append)

    assert sleeps == [12.5]


def test_adaptive_interval_resets_on_new_posts_and_grows_when_quiet():
    interval = AdaptiveInterval(base=60, max_interval=300, growth=1.5, jitter=0, rng=random.Random(1))

    first_quiet = interval.next_delay(got_new_posts=False)
    assert first_quiet == pytest.approx(90.0)

    second_quiet = interval.next_delay(got_new_posts=False)
    assert second_quiet == pytest.approx(135.0)

    after_new_post = interval.next_delay(got_new_posts=True)
    assert after_new_post == pytest.approx(60.0)


def test_adaptive_interval_caps_at_max():
    interval = AdaptiveInterval(base=200, max_interval=300, growth=2.0, jitter=0, rng=random.Random(2))
    for _ in range(10):
        delay = interval.next_delay(got_new_posts=False)
    assert delay == pytest.approx(300.0)


def test_adaptive_interval_applies_seeded_jitter():
    interval = AdaptiveInterval(base=100, max_interval=300, growth=1.5, jitter=0.2, rng=random.Random(42))
    delay = interval.next_delay(got_new_posts=False)
    # current becomes 150 before jitter; jitter is +/- 20 percent of that.
    assert 120.0 <= delay <= 180.0


def test_breaker_opens_after_threshold_consecutive_failures():
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=300, clock=lambda: clock["t"])

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True


def test_breaker_half_opens_after_cooldown():
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=100, clock=lambda: clock["t"])

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is True

    clock["t"] = 99.0
    assert breaker.is_open is True

    clock["t"] = 100.5
    assert breaker.is_open is False


def test_breaker_success_resets_failure_count():
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=300, clock=lambda: 0.0)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.is_open is False
