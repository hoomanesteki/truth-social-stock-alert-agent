from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Post:
    id: str
    account: str
    created_at: datetime
    text: str
    url: str
    raw_html: str
    is_reply: bool
    is_repost: bool
    is_quote: bool
    has_media: bool
    source: str
    fetched_at: datetime
    quoted_text: str = ""

    @property
    def detection_text(self) -> str:
        # Keep text and quoted_text separate on the object (sentiment needs to
        # tell "what he said" apart from "what he amplified"), but detection
        # over a quote post needs both, since the post itself carries no words.
        if not self.quoted_text:
            return self.text
        return f"{self.text}\n\n{self.quoted_text}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["fetched_at"] = self.fetched_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Post":
        kwargs = dict(d)
        kwargs["created_at"] = parse_iso_datetime(d["created_at"])
        kwargs["fetched_at"] = parse_iso_datetime(d["fetched_at"])
        return cls(**kwargs)


def parse_iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class TickerMention:
    ticker: str
    company: str
    matched_text: str
    method: str
    confidence: float


@dataclass(frozen=True)
class Detection:
    post_id: str
    is_stock_related: bool
    mentions: tuple[TickerMention, ...]
    detector: str
    latency_ms: float
