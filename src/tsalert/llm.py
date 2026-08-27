from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from tsalert.reliability import with_retries
from tsalert.sources.base import TransientSourceError

Transport = Callable[[str, dict], Any]

_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqError(Exception):
    """Raised for a non-JSON body, a malformed schema, or a non-retryable API error."""


class GroqClient:
    """Thin client for the Groq chat completions API.

    Mirrors the injected transport and monotonic throttle pattern used by
    TruthSocialApiSource: the transport is a plain callable so tests never
    touch the network, and calls are spaced out with the same clock/sleep
    seam.

    A cache keyed on sha256(model, system, user, temperature) means a
    re-run of the labeling step never re-spends tokens on a post it has
    already labeled: a cache hit returns the stored result and makes no
    transport call at all.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: int = 90,
        cache_path: str | Path | None = None,
        impersonate: str = "safari17_0",
        min_request_interval: float = 0.5,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.impersonate = impersonate
        self.min_request_interval = min_request_interval
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._transport = transport or self._build_default_transport()
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self._cache: dict[str, dict] = {}
        self.last_cache_hit = False
        if self.cache_path is not None:
            self._load_cache()

    def _build_default_transport(self) -> Transport:
        def transport(url: str, payload: dict) -> Any:
            from curl_cffi import requests as curl_requests

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            return curl_requests.post(
                url,
                json=payload,
                headers=headers,
                impersonate=self.impersonate,
                timeout=self.timeout,
            )

        return transport

    def complete_json(
        self, system: str, user: str, temperature: float = 0.0, max_tokens: int = 4000
    ) -> dict:
        """Send a chat completion request and return the parsed JSON content.

        On a cache hit, this makes zero transport calls.
        """
        cache_key = self._cache_key(system, user, temperature)
        if cache_key in self._cache:
            self.last_cache_hit = True
            return dict(self._cache[cache_key])

        self.last_cache_hit = False
        self._throttle()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        data = with_retries(
            lambda: self._send(payload),
            sleep=self._sleep,
        )
        parsed = self._extract_content(data)

        self._store_in_cache(cache_key, parsed)
        return dict(parsed)

    # -- cache ---------------------------------------------------------

    def _cache_key(self, system: str, user: str, temperature: float) -> str:
        raw = "\x1f".join([self.model, system, user, repr(temperature)])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        with self.cache_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = record.get("key")
                value = record.get("value")
                if isinstance(key, str) and isinstance(value, dict):
                    self._cache[key] = value

    def _store_in_cache(self, key: str, value: dict) -> None:
        self._cache[key] = dict(value)
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "value": value}) + "\n")


    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.min_request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def _send(self, payload: dict) -> dict:
        try:
            response = self._transport(_CHAT_COMPLETIONS_URL, payload)
        except Exception as exc:
            raise TransientSourceError(f"transport error: {exc}") from exc
        return self._handle_response(response)

    def _handle_response(self, response: Any) -> dict:
        status = response.status_code

        if status == 429:
            retry_after = self._parse_retry_after(response)
            err = TransientSourceError(f"rate limited (429), retry_after={retry_after}")
            err.retry_after = retry_after
            raise err

        if 500 <= status < 600:
            raise TransientSourceError(f"server error (status {status})")

        if status != 200:
            # Include the body. Groq explains real causes there, for example
            # json_validate_failed when a reasoning model spends the whole
            # max_tokens budget on reasoning and never closes the JSON.
            detail = (getattr(response, "text", "") or "")[:400]
            raise GroqError(f"groq api error: status {status}: {detail}")

        try:
            data = response.json()
        except Exception as exc:
            raise GroqError(f"response body is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise GroqError("response body is not a JSON object")

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
    def _extract_content(data: dict) -> dict:
        # This is the outer chat-completion envelope, not the model's answer.
        # Missing structure here means the API contract changed rather than
        # the model replying badly, so it is raised straight out of
        # complete_json and never retried.
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GroqError("response missing required key: choices")

        first = choices[0]
        if not isinstance(first, dict):
            raise GroqError("response 'choices[0]' is not an object")

        message = first.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise GroqError("response missing required key: message.content")

        content = message["content"]
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GroqError(f"model content is not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise GroqError("model content is not a JSON object")

        return parsed
