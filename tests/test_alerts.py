from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from tsalert.alerts.base import format_alert, format_ops_alert
from tsalert.alerts.console import ConsoleChannel
from tsalert.alerts.telegram import TelegramChannel
from tsalert.models import Detection, Post, TickerMention
from tsalert.sources.base import PermanentSourceError, TransientSourceError


def make_post(text: str = "Big news for the company today.") -> Post:
    now = datetime(2026, 8, 25, 18, 32, 56, tzinfo=timezone.utc)
    return Post(
        id="123",
        account="realDonaldTrump",
        created_at=now,
        text=text,
        url="https://truthsocial.com/@realDonaldTrump/123",
        raw_html=f"<p>{text}</p>",
        is_reply=False,
        is_repost=False,
        is_quote=False,
        has_media=False,
        source="fixture",
        fetched_at=now,
    )


def make_detection() -> Detection:
    mentions = (
        TickerMention(
            ticker="DJT", company="Trump Media", matched_text="$DJT", method="cashtag", confidence=0.95
        ),
        TickerMention(
            ticker="TSLA", company="Tesla", matched_text="$TSLA", method="cashtag", confidence=0.90
        ),
    )
    return Detection(
        post_id="123", is_stock_related=True, mentions=mentions, detector="rules", latency_ms=12.5
    )


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class RecordingTransport:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, data):
        self.calls.append((url, dict(data)))
        return self.response


class RaisingTransport:
    def __init__(self, exc: Exception):
        self.exc = exc

    def __call__(self, url, data):
        raise self.exc


def test_format_alert_contains_required_fields():
    text = format_alert(make_post(), make_detection())
    assert "DJT" in text
    assert "TSLA" in text
    assert "Trump Media" in text
    assert "Tesla" in text
    assert "2026-08-25 18:32 UTC" in text
    assert "Big news for the company today." in text
    assert "https://truthsocial.com/@realDonaldTrump/123" in text


def test_format_alert_truncates_long_text():
    long_text = "x" * 3500
    text = format_alert(make_post(long_text), make_detection())
    assert "[truncated]" in text
    # 3000 chars of body plus the marker, not the full 3500.
    assert "x" * 3500 not in text
    assert "x" * 3000 in text


def test_format_ops_alert_layout():
    text = format_ops_alert("no_new_posts", "no posts in 12 hours")
    assert text.startswith("OPS ALARM: no_new_posts\n")
    assert "no posts in 12 hours" in text
    assert "time:" in text


def test_telegram_builds_url_and_body():
    transport = RecordingTransport(FakeResponse(200))
    channel = TelegramChannel("secret-token", "42", transport=transport)
    channel.send("hello world")
    url, data = transport.calls[0]
    assert url == "https://api.telegram.org/botsecret-token/sendMessage"
    assert data["chat_id"] == "42"
    assert data["text"] == "hello world"
    assert data["disable_web_page_preview"] is True


def test_telegram_429_raises_transient_with_retry_after():
    transport = RecordingTransport(FakeResponse(429, json_body={"retry_after": 30}))
    channel = TelegramChannel("secret-token", "42", transport=transport)
    with pytest.raises(TransientSourceError) as excinfo:
        channel.send("hello")
    assert excinfo.value.retry_after == 30.0


def test_telegram_401_raises_permanent():
    transport = RecordingTransport(FakeResponse(401, json_body={"description": "Unauthorized"}))
    channel = TelegramChannel("secret-token", "42", transport=transport)
    with pytest.raises(PermanentSourceError):
        channel.send("hello")


def test_telegram_token_never_leaks_into_exception_text():
    token = "123456:AAA-super-secret-token"

    transport_429 = RecordingTransport(FakeResponse(429, json_body={"retry_after": 5}))
    channel = TelegramChannel(token, "42", transport=transport_429)
    with pytest.raises(TransientSourceError) as excinfo:
        channel.send("hello")
    assert token not in str(excinfo.value)

    transport_401 = RecordingTransport(FakeResponse(401, json_body={}))
    channel = TelegramChannel(token, "42", transport=transport_401)
    with pytest.raises(PermanentSourceError) as excinfo:
        channel.send("hello")
    assert token not in str(excinfo.value)

    transport_error = RaisingTransport(ConnectionError(f"failed to connect to bot{token}"))
    channel = TelegramChannel(token, "42", transport=transport_error)
    with pytest.raises(TransientSourceError) as excinfo:
        channel.send("hello")
    assert token not in str(excinfo.value)


def test_telegram_is_configured_false_when_blank():
    assert TelegramChannel("", "42").is_configured() is False
    assert TelegramChannel("token", "").is_configured() is False
    assert TelegramChannel("token", "42").is_configured() is True


def test_console_channel_writes_to_stream():
    stream = io.StringIO()
    channel = ConsoleChannel(stream=stream)
    assert channel.is_configured() is True
    channel.send("test alert body")
    assert "test alert body" in stream.getvalue()
