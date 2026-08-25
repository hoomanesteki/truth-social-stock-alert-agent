from __future__ import annotations

import time

import pytest

from tsalert.sources.base import BlockedSourceError, PermanentSourceError, TransientSourceError
from tsalert.sources.truthsocial import TruthSocialApiSource


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_body is None:
            raise ValueError("response has no json body")
        return self._json_body


class FilteringTransport:
    """Replays the combined fixture pages, filtering like the real endpoint does."""

    def __init__(self, statuses):
        self.statuses = sorted(statuses, key=lambda s: int(s["id"]), reverse=True)
        self.calls: list[dict] = []

    def __call__(self, url, params):
        self.calls.append(dict(params))
        items = self.statuses
        if "max_id" in params:
            threshold = int(params["max_id"])
            items = [s for s in items if int(s["id"]) < threshold]
        if "min_id" in params:
            threshold = int(params["min_id"])
            items = [s for s in items if int(s["id"]) > threshold]
        limit = params.get("limit", 20)
        return FakeResponse(200, json_body=items[:limit])


class SequencedPageTransport:
    """Replays the two real recorded fixture pages in the order the API would."""

    def __init__(self, page1, page2):
        self.page1 = page1
        self.page2 = page2
        self.calls: list[dict] = []

    def __call__(self, url, params):
        self.calls.append(dict(params))
        if "max_id" not in params:
            return FakeResponse(200, json_body=self.page1)
        return FakeResponse(200, json_body=self.page2)


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _source(transport, **kwargs):
    return TruthSocialApiSource(account_id="107780257626128497", transport=transport, **kwargs)


def test_fetch_latest_is_oldest_first(all_statuses):
    source = _source(FilteringTransport(all_statuses))
    posts = source.fetch_latest(limit=40)
    ids = [int(p.id) for p in posts]
    assert ids == sorted(ids)
    assert len(posts) == 40


def test_fetch_latest_since_id_returns_strictly_newer(all_statuses):
    source = _source(FilteringTransport(all_statuses))
    baseline = source.fetch_latest(limit=40)
    midpoint = baseline[20].id

    newer = source.fetch_latest(since_id=midpoint, limit=100)

    assert all(int(p.id) > int(midpoint) for p in newer)
    assert len(newer) == 19


def test_fetch_history_paginates_via_max_id(page1_statuses, page2_statuses):
    transport = SequencedPageTransport(page1_statuses, page2_statuses)
    source = _source(transport, min_request_interval=0)

    first_page = source.fetch_history(limit=20)
    first_ids = [int(p.id) for p in first_page]
    assert first_ids == sorted(first_ids, reverse=True)
    assert "max_id" not in transport.calls[0]

    oldest_id = first_page[-1].id
    second_page = source.fetch_history(before_id=oldest_id, limit=20)

    assert transport.calls[1]["max_id"] == oldest_id
    assert {p.id for p in first_page}.isdisjoint({p.id for p in second_page})
    assert all(int(p.id) < int(oldest_id) for p in second_page)


def test_throttle_enforces_min_interval_without_real_sleep(page1_statuses):
    clock = FakeClock()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    transport = FilteringTransport(page1_statuses)
    source = _source(
        transport, min_request_interval=2.5, clock=clock, sleep=fake_sleep
    )

    wall_start = time.perf_counter()
    source.fetch_latest(limit=5)
    source.fetch_latest(limit=5)
    wall_elapsed = time.perf_counter() - wall_start

    assert sleeps == [2.5]
    assert wall_elapsed < 0.5


def test_429_raises_transient_with_retry_after(page1_statuses):
    transport = lambda url, params: FakeResponse(429, headers={"Retry-After": "7"})
    source = _source(transport)
    with pytest.raises(TransientSourceError) as excinfo:
        source.fetch_latest()
    assert excinfo.value.retry_after == 7.0


def test_5xx_raises_transient(page1_statuses):
    transport = lambda url, params: FakeResponse(500)
    source = _source(transport)
    with pytest.raises(TransientSourceError):
        source.fetch_latest()


def test_404_raises_permanent():
    transport = lambda url, params: FakeResponse(404)
    source = _source(transport)
    with pytest.raises(PermanentSourceError):
        source.fetch_latest()


def test_cloudflare_body_raises_blocked():
    transport = lambda url, params: FakeResponse(
        403, text="<html>Just a moment...</html>"
    )
    source = _source(transport)
    with pytest.raises(BlockedSourceError):
        source.fetch_latest()


def test_non_list_json_raises_permanent():
    transport = lambda url, params: FakeResponse(200, json_body={"error": "nope"})
    source = _source(transport)
    with pytest.raises(PermanentSourceError):
        source.fetch_latest()


def test_majority_malformed_page_raises_permanent(page1_statuses):
    bad_items = [{"not": "a status"} for _ in range(3)]
    data = page1_statuses[:1] + bad_items
    transport = lambda url, params: FakeResponse(200, json_body=data)
    source = _source(transport)
    with pytest.raises(PermanentSourceError):
        source.fetch_latest()


def test_single_malformed_item_is_skipped(page1_statuses):
    good = page1_statuses[:5]
    data = good + [{"not": "a status"}]
    transport = lambda url, params: FakeResponse(200, json_body=data)
    source = _source(transport)
    posts = source.fetch_latest(limit=10)
    assert len(posts) == 5
    assert {p.id for p in posts} == {s["id"] for s in good}


def test_hourly_cap_raises_before_sending(page1_statuses):
    transport = FilteringTransport(page1_statuses)
    source = _source(transport, max_requests_per_hour=1, min_request_interval=0)
    source.fetch_latest(limit=1)
    assert len(transport.calls) == 1
    with pytest.raises(TransientSourceError):
        source.fetch_latest(limit=1)
    assert len(transport.calls) == 1
