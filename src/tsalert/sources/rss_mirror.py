"""Fallback source: the trumpstruth.org RSS mirror.

Treat this as a fallback only. It is not interchangeable with the API. The
feed gives us post text and the truth-social id, but it does not carry
repost/quote/media structure (those fields are always reported False here),
and it lags behind the live source by some amount. truth:originalId is in
the same id space as the primary API's status ids, which is what keeps
dedup consistent across a failover to this source and back.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from tsalert.models import Post
from tsalert.sources.base import (
    PermanentSourceError,
    SourceError,
    SourceHealth,
    TransientSourceError,
    id_sort_key,
)
from tsalert.sources.parse import html_to_text

logger = logging.getLogger(__name__)

Transport = Callable[[str, dict], Any]

_TRUTH_NS = {"truth": "https://truthsocial.com/ns"}


class TrumpsTruthRssSource:
    name = "trumpstruth_rss"

    def __init__(
        self,
        url: str = "https://trumpstruth.org/feed",
        account: str = "realDonaldTrump",
        timeout: int = 20,
        transport: Transport | None = None,
    ) -> None:
        self.url = url
        self.account = account
        self.timeout = timeout
        self._transport = transport or self._build_default_transport()
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self.skipped_missing_id_count = 0

    def _build_default_transport(self) -> Transport:
        def transport(url: str, params: dict) -> Any:
            from curl_cffi import requests as curl_requests

            return curl_requests.get(url, params=params, timeout=self.timeout)

        return transport

    def fetch_latest(self, since_id: str | None = None, limit: int = 20) -> list[Post]:
        posts = self._fetch_all()
        if since_id is not None:
            threshold = id_sort_key(since_id)
            posts = [p for p in posts if id_sort_key(p.id) > threshold]
        posts.sort(key=lambda p: id_sort_key(p.id))
        return posts[-limit:] if limit else posts

    def fetch_history(self, before_id: str | None = None, limit: int = 20) -> list[Post]:
        posts = self._fetch_all()
        if before_id is not None:
            threshold = id_sort_key(before_id)
            posts = [p for p in posts if id_sort_key(p.id) < threshold]
        posts.sort(key=lambda p: id_sort_key(p.id), reverse=True)
        return posts[:limit] if limit else posts

    def health(self) -> SourceHealth:
        return SourceHealth(
            ok=self._last_success is not None,
            last_success=self._last_success,
            detail=self._last_error or ("ok" if self._last_success else "no fetch performed yet"),
        )

    def _fetch_all(self) -> list[Post]:
        try:
            response = self._transport(self.url, {})
        except Exception as exc:
            self._last_error = str(exc)
            raise TransientSourceError(f"transport error: {exc}") from exc

        status = getattr(response, "status_code", 200)
        if status != 200:
            self._last_error = f"RSS feed returned status {status}"
            raise TransientSourceError(self._last_error)

        try:
            posts = self._parse_feed(response.text)
        except SourceError as exc:
            self._last_error = str(exc)
            raise

        self._last_success = datetime.now(timezone.utc)
        self._last_error = None
        return posts

    def _parse_feed(self, body: str) -> list[Post]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise PermanentSourceError(f"RSS feed is not valid XML: {exc}") from exc

        items = root.findall("./channel/item")
        fetched_at = datetime.now(timezone.utc)
        posts = []
        for item in items:
            post = self._parse_item(item, fetched_at)
            if post is not None:
                posts.append(post)
        return posts

    def _parse_item(self, item: ET.Element, fetched_at: datetime) -> Post | None:
        original_id = item.findtext("truth:originalId", namespaces=_TRUTH_NS)
        if not original_id or not original_id.strip():
            # No id means dedup and last_seen_post_id have nothing safe to
            # key on. Skipping and counting beats inventing an id, which
            # would either collide with a real post or poison the id space.
            self.skipped_missing_id_count += 1
            logger.warning("RSS item missing truth:originalId, skipping")
            return None
        original_url = item.findtext("truth:originalUrl", namespaces=_TRUTH_NS) or ""
        description = item.findtext("description") or ""
        created_at = self._parse_pub_date(item.findtext("pubDate"))

        return Post(
            id=original_id.strip(),
            account=self.account,
            created_at=created_at,
            text=html_to_text(description),
            url=original_url.strip(),
            raw_html=description,
            is_reply=False,
            is_repost=False,
            is_quote=False,
            has_media=False,
            source=self.name,
            fetched_at=fetched_at,
        )

    @staticmethod
    def _parse_pub_date(value: str | None) -> datetime:
        if not value:
            raise PermanentSourceError("RSS item missing pubDate")
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise PermanentSourceError(f"unparseable pubDate: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
