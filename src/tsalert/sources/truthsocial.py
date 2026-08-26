from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from tsalert.models import Post
from tsalert.sources.base import (
    BlockedSourceError,
    PermanentSourceError,
    SourceError,
    SourceHealth,
    TransientSourceError,
)
from tsalert.sources.parse import MalformedStatusError, parse_status

Transport = Callable[[str, dict], Any]

_CLOUDFLARE_MARKERS = ("Just a moment", "cf-", "Attention Required")


class TruthSocialApiSource:
    """Primary source: the truthsocial.com statuses API.

    chrome124 impersonation gets a Cloudflare 403 against this endpoint,
    safari17_0 does not. Do not change the default impersonate value.
    """

    name = "truthsocial_api"

    _URL_TEMPLATE = "https://truthsocial.com/api/v1/accounts/{account_id}/statuses"

    def __init__(
        self,
        account_id: str,
        impersonate: str = "safari17_0",
        timeout: int = 20,
        min_request_interval: float = 2.5,
        max_requests_per_hour: int = 600,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.account_id = account_id
        self.impersonate = impersonate
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self.max_requests_per_hour = max_requests_per_hour
        self._transport = transport or self._build_default_transport()
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._request_times: deque[float] = deque()
        self._last_success: datetime | None = None
        self._last_error: str | None = None

    def _build_default_transport(self) -> Transport:
        def transport(url: str, params: dict) -> Any:
            from curl_cffi import requests as curl_requests

            return curl_requests.get(
                url, params=params, impersonate=self.impersonate, timeout=self.timeout
            )

        return transport

    def fetch_latest(self, since_id: str | None = None, limit: int = 20) -> list[Post]:
        """Return posts newer than since_id, oldest-first."""
        params: dict[str, Any] = {"limit": limit}
        if since_id is not None:
            params["min_id"] = since_id
        data = self._request(params)
        posts = self._parse_page(data)
        posts.sort(key=lambda p: int(p.id))
        self._record_success()
        return posts

    def fetch_history(self, before_id: str | None = None, limit: int = 20) -> list[Post]:
        """Return posts older than before_id, newest-first, one page at a time."""
        params: dict[str, Any] = {"limit": limit}
        if before_id is not None:
            params["max_id"] = before_id
        data = self._request(params)
        posts = self._parse_page(data)
        posts.sort(key=lambda p: int(p.id), reverse=True)
        self._record_success()
        return posts

    def health(self) -> SourceHealth:
        return SourceHealth(
            ok=self._last_success is not None,
            last_success=self._last_success,
            detail=self._last_error or ("ok" if self._last_success else "no fetch performed yet"),
        )

    # -- request plumbing ---------------------------------------------------

    def _request(self, params: dict) -> list[dict]:
        self._check_rate_limit()
        self._throttle()
        url = self._URL_TEMPLATE.format(account_id=self.account_id)
        try:
            response = self._transport(url, params)
        except Exception as exc:
            self._last_error = str(exc)
            raise TransientSourceError(f"transport error: {exc}") from exc
        self._request_times.append(self._clock())
        try:
            return self._handle_response(response)
        except SourceError as exc:
            self._last_error = str(exc)
            raise

    def _throttle(self) -> None:
        # Monotonic-clock gap enforcement: guarantee at least
        # min_request_interval between any two requests this instance sends,
        # regardless of what triggered them (poll loop, retry, backfill).
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.min_request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def _check_rate_limit(self) -> None:
        # Rolling one hour window, evaluated before sending so a runaway
        # caller gets stopped rather than hammering the server one more time.
        now = self._clock()
        while self._request_times and now - self._request_times[0] > 3600:
            self._request_times.popleft()
        if len(self._request_times) >= self.max_requests_per_hour:
            raise TransientSourceError(
                f"hourly request cap of {self.max_requests_per_hour} reached, refusing to send"
            )

    def _handle_response(self, response: Any) -> list[dict]:
        status = response.status_code

        if status == 429:
            retry_after = self._parse_retry_after(response)
            err = TransientSourceError(f"rate limited (429), retry_after={retry_after}")
            err.retry_after = retry_after
            raise err

        if status in (403, 503) and self._looks_like_cloudflare(response):
            raise BlockedSourceError(f"blocked by Cloudflare challenge (status {status})")

        if status == 404:
            raise PermanentSourceError("account not found (404)")

        if 500 <= status < 600:
            raise TransientSourceError(f"server error (status {status})")

        if status == 403:
            # A 403 without a Cloudflare challenge in the body looks like a
            # real access denial rather than a transient block, so treat it
            # as permanent instead of retrying forever against a closed door.
            raise PermanentSourceError("403 forbidden without a Cloudflare challenge")

        if status != 200:
            raise TransientSourceError(f"unexpected status {status}")

        try:
            data = response.json()
        except Exception as exc:
            raise PermanentSourceError(f"response body is not valid JSON: {exc}") from exc

        if not isinstance(data, list):
            raise PermanentSourceError("response body is not a JSON list, schema changed")

        return data

    @staticmethod
    def _parse_retry_after(response: Any) -> float | None:
        headers = getattr(response, "headers", None) or {}
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _looks_like_cloudflare(response: Any) -> bool:
        body = getattr(response, "text", "") or ""
        return any(marker in body for marker in _CLOUDFLARE_MARKERS)

    def _parse_page(self, data: list[dict]) -> list[Post]:
        # If most of a page fails to parse that is not a couple of bad posts,
        # it is the response format changing on us. One status missing a
        # field is normal; a majority means raise rather than return nothing.
        fetched_at = datetime.now(timezone.utc)
        posts = []
        malformed = 0
        for item in data:
            try:
                posts.append(parse_status(item, source=self.name, fetched_at=fetched_at))
            except MalformedStatusError:
                malformed += 1
        if data and malformed / len(data) > 0.5:
            raise PermanentSourceError(
                f"{malformed}/{len(data)} items on page failed to parse, "
                "likely a layout or schema change"
            )
        return posts

    def _record_success(self) -> None:
        self._last_success = datetime.now(timezone.utc)
        self._last_error = None
