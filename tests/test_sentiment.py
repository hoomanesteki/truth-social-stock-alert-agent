from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tsalert.alerts.base import format_alert
from tsalert.alerts.dispatcher import AlertDispatcher
from tsalert.store import Store
from tsalert.llm import GroqClient
from tsalert.models import Detection, Post, TickerMention
from tsalert.sentiment import Sentiment, SentimentScorer


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
        self.headers = {}
        self.text = ""

    def json(self):
        return self._json_body


def _good_body(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


class RecordingTransport:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload):
        self.calls.append((url, dict(payload)))
        return self.response


class RaisingTransport:
    def __call__(self, url, payload):
        raise ConnectionError("network is down")


def make_client(transport) -> GroqClient:
    return GroqClient(api_key="test-key", model="qwen/qwen3.6-27b", transport=transport)


def test_score_maps_fake_transport_response_to_sentiment():
    payload = {"label": "bullish", "confidence": 0.82, "rationale": "his own company beating expectations"}
    transport = RecordingTransport(FakeResponse(200, _good_body(payload)))
    scorer = SentimentScorer(make_client(transport))

    result = scorer.score("Great earnings for our company", ["DJT"])

    assert result == Sentiment(
        label="bullish", confidence=0.82, rationale="his own company beating expectations"
    )
    # One real call, since this is a cache miss on a fresh client.
    assert len(transport.calls) == 1


def test_score_clamps_confidence_and_lowercases_label():
    payload = {"label": "BEARISH", "confidence": 5, "rationale": "overheated"}
    transport = RecordingTransport(FakeResponse(200, _good_body(payload)))
    scorer = SentimentScorer(make_client(transport))

    result = scorer.score("text", ["TSLA"])

    assert result.label == "bearish"
    assert result.confidence == 1.0


def test_score_rejects_invalid_label():
    payload = {"label": "very bullish", "confidence": 0.5, "rationale": "x"}
    transport = RecordingTransport(FakeResponse(200, _good_body(payload)))
    scorer = SentimentScorer(make_client(transport))

    try:
        scorer.score("text", ["TSLA"])
        assert False, "expected a ValueError"
    except ValueError:
        pass


def test_format_alert_without_sentiment_has_no_sentiment_line():
    post = make_post()
    detection = make_detection()

    expected = (
        "STOCK MENTION: DJT, TSLA\n"
        "companies: Trump Media, Tesla\n"
        "posted: 2026-08-25 18:32 UTC\n"
        "matched: cashtag (rules)\n"
        "\n"
        "Big news for the company today.\n"
        "\n"
        "https://truthsocial.com/@realDonaldTrump/123"
    )

    assert format_alert(post, detection) == expected
    assert format_alert(post, detection, sentiment=None) == expected


def test_format_alert_with_sentiment_adds_exactly_one_line():
    post = make_post()
    detection = make_detection()
    sentiment = Sentiment(label="bullish", confidence=0.82, rationale="his own company beating expectations")

    without = format_alert(post, detection)
    with_sentiment = format_alert(post, detection, sentiment=sentiment)

    without_lines = without.split("\n")
    with_lines = with_sentiment.split("\n")
    assert len(with_lines) == len(without_lines) + 1
    assert "sentiment: bullish (0.82) his own company beating expectations" in with_lines
    # Everything else stays in place around the inserted line.
    assert with_lines[:4] == without_lines[:4]
    assert with_lines[5:] == without_lines[4:]


def test_scorer_failure_does_not_stop_the_alert(tmp_path):
    """A dead sentiment model must cost the annotation, never the alert."""
    scorer = SentimentScorer(make_client(RaisingTransport()))
    post = make_post()
    detection = make_detection()

    channel = RecordingChannel()
    with Store(tmp_path / "s.db") as store:
        store.upsert_post(post)
        dispatcher = AlertDispatcher([channel], store, sentiment_scorer=scorer)
        results = dispatcher.dispatch(post, detection)

    assert [r.ok for r in results] == [True]
    assert len(channel.sent) == 1
    assert "sentiment:" not in channel.sent[0]
    assert post.text in channel.sent[0]




class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def is_configured(self) -> bool:
        return True
