from __future__ import annotations

from typing import Any, Callable

from tsalert.sources.base import PermanentSourceError, TransientSourceError

Transport = Callable[[str, dict], Any]

# Discord rejects a message body over 2000 characters outright. Alerts carry
# the full post text and his posts run long, so this is a limit that gets hit
# in normal use rather than an edge case.
_MAX_MESSAGE_CHARS = 2000
_TRUNCATED_MARKER = "\n[truncated]"


class DiscordChannel:
    """Delivers alerts through a Discord incoming webhook.

    The webhook URL is the entire credential, which is the reason this is the
    primary channel. A Telegram bot needs a token plus a chat id discovered by
    messaging the bot first, and the token is revoked the moment you
    regenerate it in BotFather, which is a failure that looks exactly like
    the network being down. A webhook URL has no such lifecycle.

    Sends plain content with no embeds. Trump's posts are full of characters
    that Discord's markdown will happily reinterpret, and an alert that
    arrives looking odd beats one that arrives mangled or not at all, so the
    text goes through as is.
    """

    name = "discord"

    def __init__(
        self,
        webhook_url: str,
        timeout: int = 20,
        transport: Transport | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout
        self._transport = transport or self._build_default_transport()

    def _build_default_transport(self) -> Transport:
        def transport(url: str, payload: dict) -> Any:
            from curl_cffi import requests as curl_requests

            return curl_requests.post(url, json=payload, timeout=self.timeout)

        return transport

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, text: str) -> None:
        payload = {"content": _fit(text), "allowed_mentions": {"parse": []}}
        try:
            response = self._transport(self.webhook_url, payload)
        except Exception as exc:
            # The URL contains the webhook secret and a connection error from
            # the HTTP client can echo the request URL back, so only the
            # exception type goes into the message. Same reasoning as the
            # Telegram channel.
            raise TransientSourceError(
                f"discord transport error: {type(exc).__name__}"
            ) from None
        self._handle_response(response)

    def _handle_response(self, response: Any) -> None:
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            # A transport that returned something unexpected is a bug on our
            # side, not a verdict from Discord. Falling through to the
            # permanent branch discarded the alert forever on the strength of
            # a status nobody sent.
            raise TransientSourceError(
                f"discord response had no usable status ({type(response).__name__})"
            )
        # 204 is the documented success for a webhook with no wait parameter.
        # 200 shows up when Discord decides to return the created message.
        if status in (200, 204):
            return
        if status == 429:
            retry_after = self._parse_retry_after(response)
            err = TransientSourceError(
                f"discord rate limited (429), retry_after={retry_after}"
            )
            err.retry_after = retry_after
            raise err
        if 500 <= status < 600:
            raise TransientSourceError(f"discord server error (status {status})")
        # 401 and 404 are what a deleted or mistyped webhook returns, and no
        # amount of retrying fixes either.
        raise PermanentSourceError(f"discord request rejected (status {status})")

    @staticmethod
    def _parse_retry_after(response: Any) -> float | None:
        """Discord sends the wait in a JSON body, in seconds, as a float."""
        try:
            body = response.json()
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        value = body.get("retry_after")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _utf16_length(text: str) -> int:
    """Length the way Discord counts it.

    The limit is on UTF-16 code units, not code points, so anything outside
    the basic multilingual plane counts twice. Emoji are the common case and
    his posts are full of them, so trimming to exactly 2000 code points could
    still produce a 2001 unit payload. Discord answers that with a 400, which
    this channel classifies as permanent, so the alert was dropped for good
    rather than retried.
    """
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _fit(text: str) -> str:
    """Trim to Discord's hard limit, keeping the head of the alert.

    The ticker, companies and timestamp are the first three lines, so cutting
    from the end loses post text rather than the part that says what the
    alert is about.
    """
    if _utf16_length(text) <= _MAX_MESSAGE_CHARS:
        return text
    budget = _MAX_MESSAGE_CHARS - _utf16_length(_TRUNCATED_MARKER)
    used = 0
    cut = 0
    for index, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        cut = index + 1
    return text[:cut] + _TRUNCATED_MARKER
