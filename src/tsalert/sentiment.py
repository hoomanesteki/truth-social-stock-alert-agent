from __future__ import annotations

from dataclasses import dataclass

from tsalert.llm import GroqClient

_DEFAULT_MODEL = "qwen/qwen3.6-27b"

_SYSTEM_PROMPT = """You score the sentiment of a social media post toward the stock tickers it mentions.

label must be exactly one of: bullish, bearish, neutral.
confidence is a number from 0.0 to 1.0.
rationale is one short sentence explaining the label.

Respond with strict JSON only, no other text:
{"label": "bullish", "confidence": 0.8, "rationale": "one short sentence"}
"""

_VALID_LABELS = {"bullish", "bearish", "neutral"}


@dataclass(frozen=True)
class Sentiment:
    label: str
    confidence: float
    rationale: str


class SentimentScorer:
    """Scores the tone of a post already found to mention a stock.

    Only ever called on posts a detector has already flagged as stock
    related, so the extra Groq call this makes is rare and stays cheap.
    """

    def __init__(self, client: GroqClient, model: str = _DEFAULT_MODEL) -> None:
        self.client = client
        # The actual completion model is whatever `client` was built with;
        # this is kept only as a record of which model the caller intended,
        # since GroqClient.complete_json has no per-call model override.
        self.model = model

    def score(self, text: str, tickers: list[str]) -> Sentiment:
        user_message = f"Tickers: {', '.join(tickers)}\n\nPost:\n{text}"
        # max_tokens is generous on purpose: the gpt-oss family are
        # reasoning models, and a small budget gets spent entirely on
        # reasoning tokens, coming back as json_validate_failed with no
        # usable content at all.
        label = self.client.complete_json(_SYSTEM_PROMPT, user_message, max_tokens=4000)
        return self._parse(label)

    @staticmethod
    def _parse(label: dict) -> Sentiment:
        raw_label = str(label.get("label", "")).strip().lower()
        if raw_label not in _VALID_LABELS:
            raise ValueError(f"sentiment label not one of {sorted(_VALID_LABELS)}: {raw_label!r}")
        try:
            confidence = float(label["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"sentiment confidence missing or not a number: {exc}") from exc
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(label.get("rationale", "")).strip()
        return Sentiment(label=raw_label, confidence=confidence, rationale=rationale)
