from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from tsalert.models import Post

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_CLOSE_P_RE = re.compile(r"</p\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


class MalformedStatusError(ValueError):
    pass


def html_to_text(html_content: str) -> str:
    if not html_content:
        return ""
    text = _BR_RE.sub("\n", html_content)
    text = _CLOSE_P_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _parse_created_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise MalformedStatusError(f"missing or invalid created_at: {value!r}")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MalformedStatusError(f"unparseable created_at: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_status(status: dict, source: str, fetched_at: datetime | None = None) -> Post:
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)

    outer_id = status.get("id")
    if not isinstance(outer_id, str) or not outer_id:
        raise MalformedStatusError(f"status missing valid id: {outer_id!r}")

    created_at = _parse_created_at(status.get("created_at"))

    reblog = status.get("reblog")
    is_repost = reblog is not None
    # A repost's own content/url/media describe the wrapper, not the original
    # post, so pull those from the inner status while keeping the outer id
    # (dedup and the permalink key on the outer id).
    content_source = reblog if is_repost else status

    raw_html = content_source.get("content") or ""
    text = html_to_text(raw_html)
    url = content_source.get("url") or ""
    media = content_source.get("media_attachments") or []

    account = status.get("account") or {}
    account_name = account.get("username") or account.get("acct") or ""

    return Post(
        id=outer_id,
        account=account_name,
        created_at=created_at,
        text=text,
        url=url,
        raw_html=raw_html,
        is_reply=status.get("in_reply_to_id") is not None,
        is_repost=is_repost,
        is_quote=status.get("quote_id") is not None,
        has_media=len(media) > 0,
        source=source,
        fetched_at=fetched_at,
    )
