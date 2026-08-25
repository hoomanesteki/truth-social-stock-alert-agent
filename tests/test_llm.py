from __future__ import annotations

import json
import time

import pytest

from tsalert.llm import GroqClient, GroqError


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", headers=None, json_error=False):
        self.status_code = status_code
        self._json_body = json_body
        self._json_error = json_error
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_error:
            raise ValueError("body is not valid json")
        return self._json_body


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _good_body(payload: dict) -> dict:
    return {
        "choices": [
            {"message": {"content": json.dumps(payload)}}
        ]
    }


def _client(transport, **kwargs):
    return GroqClient(
        api_key="fake-key-do-not-log",
        model="openai/gpt-oss-120b",
        transport=transport,
        sleep=lambda s: None,
        **kwargs,
    )


def test_complete_json_parses_good_response():
    calls = []

    def transport(url, payload):
        calls.append((url, payload))
        return FakeResponse(200, json_body=_good_body({"is_stock_related": True, "category": "specific_equity"}))

    client = _client(transport)
    result = client.complete_json("system prompt", "user prompt")

    assert result == {"is_stock_related": True, "category": "specific_equity"}
    assert len(calls) == 1
    assert calls[0][0] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[0][1]["model"] == "openai/gpt-oss-120b"
    assert calls[0][1]["messages"][0] == {"role": "system", "content": "system prompt"}
    assert calls[0][1]["messages"][1] == {"role": "user", "content": "user prompt"}


def test_complete_json_does_not_leak_api_key_to_transport_payload():
    def transport(url, payload):
        assert "fake-key-do-not-log" not in json.dumps(payload)
        return FakeResponse(200, json_body=_good_body({"ok": True}))

    client = _client(transport)
    client.complete_json("sys", "user")


def test_cache_hit_makes_zero_transport_calls(tmp_path):
    calls = {"n": 0}

    def transport(url, payload):
        calls["n"] += 1
        return FakeResponse(200, json_body=_good_body({"is_stock_related": False, "category": "not_financial"}))

    cache_path = tmp_path / "cache.jsonl"
    client = _client(transport, cache_path=cache_path)

    first = client.complete_json("system prompt", "user prompt")
    assert calls["n"] == 1
    assert client.last_cache_hit is False

    second = client.complete_json("system prompt", "user prompt")
    assert calls["n"] == 1  # no additional transport call on the cache hit
    assert second == first
    assert client.last_cache_hit is True


def test_cache_persists_across_client_instances(tmp_path):
    calls = {"n": 0}

    def transport(url, payload):
        calls["n"] += 1
        return FakeResponse(200, json_body=_good_body({"is_stock_related": True, "category": "specific_equity"}))

    cache_path = tmp_path / "cache.jsonl"

    client_one = _client(transport, cache_path=cache_path)
    client_one.complete_json("system prompt", "user prompt")
    assert calls["n"] == 1

    client_two = _client(transport, cache_path=cache_path)
    result = client_two.complete_json("system prompt", "user prompt")
    assert calls["n"] == 1
    assert client_two.last_cache_hit is True
    assert result == {"is_stock_related": True, "category": "specific_equity"}


def test_cache_key_distinguishes_different_prompts(tmp_path):
    calls = {"n": 0}

    def transport(url, payload):
        calls["n"] += 1
        return FakeResponse(200, json_body=_good_body({"ok": True}))

    cache_path = tmp_path / "cache.jsonl"
    client = _client(transport, cache_path=cache_path)

    client.complete_json("system prompt", "user prompt A")
    client.complete_json("system prompt", "user prompt B")

    assert calls["n"] == 2


def test_429_is_retried_and_then_succeeds():
    responses = [
        FakeResponse(429, headers={"Retry-After": "1"}),
        FakeResponse(200, json_body=_good_body({"is_stock_related": True, "category": "specific_equity"})),
    ]
    calls = {"n": 0}
    sleeps = []

    def transport(url, payload):
        response = responses[calls["n"]]
        calls["n"] += 1
        return response

    client = GroqClient(
        api_key="fake-key",
        model="openai/gpt-oss-120b",
        transport=transport,
        sleep=sleeps.append,
    )

    result = client.complete_json("sys", "user")

    assert calls["n"] == 2
    assert result == {"is_stock_related": True, "category": "specific_equity"}
    assert sleeps == [1.0]  # honored Retry-After exactly, no computed backoff


def test_5xx_is_retried_and_then_succeeds():
    responses = [
        FakeResponse(503),
        FakeResponse(200, json_body=_good_body({"ok": True})),
    ]
    calls = {"n": 0}

    def transport(url, payload):
        response = responses[calls["n"]]
        calls["n"] += 1
        return response

    client = _client(transport)
    result = client.complete_json("sys", "user")

    assert calls["n"] == 2
    assert result == {"ok": True}


def test_non_json_body_raises_groq_error():
    def transport(url, payload):
        return FakeResponse(200, json_error=True)

    client = _client(transport)
    with pytest.raises(GroqError):
        client.complete_json("sys", "user")


def test_response_missing_choices_raises_groq_error():
    def transport(url, payload):
        return FakeResponse(200, json_body={"unexpected": "shape"})

    client = _client(transport)
    with pytest.raises(GroqError):
        client.complete_json("sys", "user")


def test_response_missing_message_content_raises_groq_error():
    def transport(url, payload):
        return FakeResponse(200, json_body={"choices": [{"message": {}}]})

    client = _client(transport)
    with pytest.raises(GroqError):
        client.complete_json("sys", "user")


def test_model_content_not_json_raises_groq_error():
    def transport(url, payload):
        return FakeResponse(200, json_body={"choices": [{"message": {"content": "not json"}}]})

    client = _client(transport)
    with pytest.raises(GroqError):
        client.complete_json("sys", "user")


def test_a_failed_call_is_not_cached(tmp_path):
    calls = {"n": 0}

    def transport(url, payload):
        calls["n"] += 1
        return FakeResponse(200, json_body={"choices": [{"message": {"content": "not json"}}]})

    cache_path = tmp_path / "cache.jsonl"
    client = _client(transport, cache_path=cache_path)

    with pytest.raises(GroqError):
        client.complete_json("sys", "user")
    with pytest.raises(GroqError):
        client.complete_json("sys", "user")

    assert calls["n"] == 2  # the failed attempt was never cached


def test_throttle_waits_between_calls_without_real_sleep():
    clock = FakeClock()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    def transport(url, payload):
        return FakeResponse(200, json_body=_good_body({"ok": True}))

    client = GroqClient(
        api_key="fake-key",
        model="openai/gpt-oss-120b",
        transport=transport,
        min_request_interval=2.5,
        clock=clock,
        sleep=fake_sleep,
    )

    wall_start = time.perf_counter()
    client.complete_json("sys", "user 1")
    client.complete_json("sys", "user 2")
    wall_elapsed = time.perf_counter() - wall_start

    assert sleeps == [2.5]
    assert wall_elapsed < 0.5


def test_throttle_is_skipped_on_cache_hit(tmp_path):
    clock = FakeClock()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    calls = {"n": 0}

    def transport(url, payload):
        calls["n"] += 1
        return FakeResponse(200, json_body=_good_body({"ok": True}))

    cache_path = tmp_path / "cache.jsonl"
    client = GroqClient(
        api_key="fake-key",
        model="openai/gpt-oss-120b",
        transport=transport,
        min_request_interval=2.5,
        clock=clock,
        sleep=fake_sleep,
        cache_path=cache_path,
    )

    client.complete_json("sys", "user")
    sleeps.clear()
    client.complete_json("sys", "user")  # cache hit

    assert calls["n"] == 1
    assert sleeps == []
