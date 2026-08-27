from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from tsalert.models import Detection, Post
from tsalert.sentiment import Sentiment

_MAX_TEXT_CHARS = 3000
_TRUNCATED_MARKER = " [truncated]"


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    post_id: str
    ok: bool
    attempts: int
    error: str = ""
    delivered_at: datetime | None = None


class AlertChannel(Protocol):
    name: str

    def send(self, text: str) -> None: ...

    def is_configured(self) -> bool: ...


def _format_timestamp(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    return text[:_MAX_TEXT_CHARS] + _TRUNCATED_MARKER


def format_alert(post: Post, detection: Detection, sentiment: Sentiment | None = None) -> str:
    tickers = ", ".join(m.ticker for m in detection.mentions)
    companies = ", ".join(m.company for m in detection.mentions)
    # How each ticker was found, which is the part that varies and the part
    # worth knowing at a glance. A cashtag is near certain, a bare ticker that
    # only cleared the ambiguity gate much less so. The confidence number this
    # replaced was the same 0.85 on almost every alert, because that is what
    # an alias match scores, so it read like a measurement while telling you
    # nothing.
    methods = {m.method for m in detection.mentions}
    if len(methods) == 1:
        matched = methods.pop()
    else:
        matched = ", ".join(f"{m.ticker} by {m.method}" for m in detection.mentions)
    # detection_text (text plus quoted_text) is what the detector actually
    # ran on. A quote post can carry no words of its own, so using post.text
    # here would deliver an alert whose entire body is the bare RT link.
    body = _truncate(post.detection_text)
    # Empty string when there is no sentiment, so the layout below is byte
    # identical to before sentiment existed.
    sentiment_line = ""
    if sentiment is not None:
        sentiment_line = f"sentiment: {sentiment.label} ({sentiment.confidence:.2f}) {sentiment.rationale}\n"
    return (
        f"STOCK MENTION: {tickers}\n"
        f"companies: {companies}\n"
        f"posted: {_format_timestamp(post.created_at)}\n"
        f"matched: {matched} ({detection.detector})\n"
        f"{sentiment_line}"
        f"\n"
        f"{body}\n"
        f"\n"
        f"{post.url}"
    )


def format_ops_alert(alarm_name: str, detail: str) -> str:
    now = datetime.now(timezone.utc)
    return f"OPS ALARM: {alarm_name}\n{detail}\ntime: {_format_timestamp(now)}"
