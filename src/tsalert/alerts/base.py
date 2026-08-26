from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from tsalert.models import Detection, Post

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


def format_alert(post: Post, detection: Detection) -> str:
    tickers = ", ".join(m.ticker for m in detection.mentions)
    companies = ", ".join(m.company for m in detection.mentions)
    # Report the strongest match's confidence, since that is the one that
    # actually decided whether this alert fired.
    confidence = max((m.confidence for m in detection.mentions), default=0.0)
    body = _truncate(post.text)
    return (
        f"STOCK MENTION: {tickers}\n"
        f"companies: {companies}\n"
        f"posted: {_format_timestamp(post.created_at)}\n"
        f"detected: {detection.detector} (confidence {confidence:.2f})\n"
        f"\n"
        f"{body}\n"
        f"\n"
        f"{post.url}"
    )


def format_ops_alert(alarm_name: str, detail: str) -> str:
    now = datetime.now(timezone.utc)
    return f"OPS ALARM: {alarm_name}\n{detail}\ntime: {_format_timestamp(now)}"
