from __future__ import annotations

from typing import Any, Callable

from tsalert.sources.base import PermanentSourceError, TransientSourceError

Transport = Callable[[str, dict], Any]


class TelegramChannel:
    """Delivers alerts through the Telegram Bot API.

    Sends plain text with no parse_mode. Trump's posts routinely contain
    characters (stray asterisks, underscores, angle brackets) that trip
    Telegram's Markdown and HTML parsers, and a formatting error that drops
    the whole message is worse than one that arrives unstyled.
    """

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: int = 20,
        transport: Transport | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._transport = transport or self._build_default_transport()

    def _build_default_transport(self) -> Transport:
        def transport(url: str, data: dict) -> Any:
            from curl_cffi import requests as curl_requests

            return curl_requests.post(url, data=data, timeout=self.timeout)

        return transport

    def is_configured(self) -> bool:
        return bool(self.bot_token) and bool(self.chat_id)

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            response = self._transport(url, data)
        except Exception as exc:
            # Do not fold str(exc) into the message: a connection error from
            # the underlying HTTP client can echo the request URL back,
            # which would put the bot token into an exception someone might
            # later log or print.
            raise TransientSourceError(
                f"telegram transport error: {type(exc).__name__}"
            ) from None
        self._handle_response(response)

    def _handle_response(self, response: Any) -> None:
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            retry_after = self._parse_retry_after(response)
            err = TransientSourceError(
                f"telegram rate limited (429), retry_after={retry_after}"
            )
            err.retry_after = retry_after
            raise err
        if 500 <= status < 600:
            raise TransientSourceError(f"telegram server error (status {status})")
        # Any other 4xx (bad token, bad chat id, malformed request) will
        # never succeed by retrying.
        raise PermanentSourceError(f"telegram request rejected (status {status})")

    @staticmethod
    def _parse_retry_after(response: Any) -> float | None:
        try:
            body = response.json()
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        value = body.get("retry_after")
        if value is None:
            params = body.get("parameters")
            if isinstance(params, dict):
                value = params.get("retry_after")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
