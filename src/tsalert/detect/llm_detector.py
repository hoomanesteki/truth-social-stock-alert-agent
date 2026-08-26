from __future__ import annotations

import sys
import time
from itertools import zip_longest
from pathlib import Path

from tsalert.detect.lexicon import Lexicon
from tsalert.llm import GroqClient, GroqError
from tsalert.models import Detection, TickerMention

# scripts/ is not a package, so it is added to sys.path the same way
# tests/test_prelabel.py already does it. SYSTEM_PROMPT is imported rather
# than copied so the detector and the prelabeling step can never drift apart.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from prelabel import SYSTEM_PROMPT  # noqa: E402

_REQUIRED_KEYS = ("is_stock_related", "tickers")


class LlmDetector:
    """Stock mention detector backed by a Groq chat completion.

    Same detect() signature as RuleDetector, so it drops into AgentRunner
    (or CombinedDetector) unchanged.
    """

    name = "llm"

    def __init__(self, client: GroqClient, lexicon: Lexicon | None = None) -> None:
        self.client = client
        self.lexicon = lexicon

    def detect(self, text: str, post_id: str = "") -> Detection:
        start_time = time.perf_counter()
        user_message = f"Post:\n{text}"

        # A GroqError here (bad schema, non-retryable API error, or a
        # TransientSourceError that survived every retry) is intentionally
        # left to propagate. Turning a network failure into a quiet
        # negative Detection would look identical to "no stock mentioned",
        # and an alerting agent that reads a failure that way goes silent
        # without anyone noticing. CombinedDetector is the
        # layer that decides what to do when the LLM is unavailable.
        label = self.client.complete_json(SYSTEM_PROMPT, user_message, max_tokens=4000)
        self._validate(label)

        is_stock_related = bool(label["is_stock_related"])
        mentions = self._build_mentions(label) if is_stock_related else ()

        latency_ms = (time.perf_counter() - start_time) * 1000
        return Detection(
            post_id=post_id,
            is_stock_related=is_stock_related,
            mentions=mentions,
            detector=self.name,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _validate(label: dict) -> None:
        missing = [key for key in _REQUIRED_KEYS if key not in label]
        if missing:
            raise GroqError(f"llm label missing required keys: {missing}")

    def _build_mentions(self, label: dict) -> tuple[TickerMention, ...]:
        tickers = label.get("tickers") or []
        companies = label.get("companies") or []

        mentions = []
        for ticker_raw, company_fallback in zip_longest(tickers, companies, fillvalue=""):
            if not ticker_raw:
                continue
            ticker = str(ticker_raw).strip().upper()
            if not ticker:
                continue
            # The curated lexicon's company name wins when we have it, so the
            # LLM arm reports the same canonical name as the rule arm. The
            # model's own "companies" entry is only a fallback for tickers
            # outside the lexicon.
            company = company_fallback
            if self.lexicon is not None:
                entry = self.lexicon.get(ticker)
                if entry is not None:
                    company = entry.company
            mentions.append(
                TickerMention(
                    ticker=ticker,
                    company=company,
                    matched_text=ticker,
                    method="llm",
                    # The prompt asks for a yes/no verdict and never for a
                    # probability, so every mention the model reports for a
                    # stock-related post gets the same fixed confidence.
                    confidence=0.9,
                )
            )
        return tuple(mentions)
