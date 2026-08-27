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
    if not isinstance(html_content, str):
        # A non string here means the endpoint changed shape. Raising the
        # malformed error keeps it inside the ratio check that decides
        # whether a page is a few bad items or a new schema. A TypeError
        # would escape that check and take the process down instead.
        raise MalformedStatusError(f"content is {type(html_content).__name__}, expected str")
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


def parse_status(status: object, source: str, fetched_at: datetime | None = None) -> Post:
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)

    if not isinstance(status, dict):
        # A 200 response can still contain nulls or scalars where objects are
        # expected. Letting that surface as AttributeError skips the ratio
        # check that decides schema change versus a few bad rows, and ends
        # the process instead.
        raise MalformedStatusError(f"status is {type(status).__name__}, expected an object")

    outer_id = status.get("id")
    if not isinstance(outer_id, str) or not outer_id:
        raise MalformedStatusError(f"status missing valid id: {outer_id!r}")

    created_at = _parse_created_at(status.get("created_at"))

    # Nested shapes need the same treatment as the top level. A string where
    # an object belongs raises AttributeError otherwise, which is not a
    # SourceError, so it slips past the ratio check and ends the process.
    reblog = status.get("reblog")
    if reblog is not None and not isinstance(reblog, dict):
        raise MalformedStatusError(f"reblog is {type(reblog).__name__}, expected an object")
    is_repost = reblog is not None
    # A repost's own content/url/media describe the wrapper, not the original
    # post, so pull those from the inner status while keeping the outer id
    # (dedup and the permalink key on the outer id).
    content_source = reblog if is_repost else status

    raw_html = content_source.get("content") or ""
    text = html_to_text(raw_html)
    url = content_source.get("url") or ""
    media = content_source.get("media_attachments") or []
    if not isinstance(media, list):
        raise MalformedStatusError(
            f"media_attachments is {type(media).__name__}, expected a list"
        )

    account = status.get("account") or {}
    if not isinstance(account, dict):
        raise MalformedStatusError(f"account is {type(account).__name__}, expected an object")
    account_name = account.get("username") or account.get("acct") or ""

    # A quote post's own content is just a "RT: <url>" stub, with the actual
    # quoted words living in a sibling `quote` dict. Without this, a quote
    # post about a stock is a silent false negative in detection.
    quote = status.get("quote")
    if quote is not None and not isinstance(quote, dict):
        raise MalformedStatusError(f"quote is {type(quote).__name__}, expected an object")
    quoted_text = html_to_text(quote.get("content") or "") if isinstance(quote, dict) else ""

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
        quoted_text=quoted_text,
    )
