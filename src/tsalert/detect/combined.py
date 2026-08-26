from __future__ import annotations

import logging
import time

from tsalert.detect.llm_detector import LlmDetector
from tsalert.detect.rules import RuleDetector
from tsalert.llm import GroqError
from tsalert.models import Detection, TickerMention
from tsalert.sources.base import TransientSourceError

logger = logging.getLogger(__name__)


class CombinedDetector:
    """Cascades the rule detector into the LLM detector.

    Rules run first because they are free and instant. The LLM is only
    consulted when the rules produce at least one candidate mention, which
    bounds both cost and latency, since only about 4 percent of posts have
    any candidate at all. When the LLM is unavailable, the agent falls back
    to the rule verdict rather than going silent.
    """

    name = "combined"

    def __init__(
        self,
        rules: RuleDetector,
        llm: LlmDetector | None,
        confirm_threshold: float = 0.0,
    ) -> None:
        self.rules = rules
        self.llm = llm
        # Gates which rule candidates are worth spending an LLM call on. The
        # default of 0.0 reproduces "consult the LLM whenever the rules
        # produce any candidate at all", since every rule confidence is a
        # positive float. Raising it would let a caller reserve LLM calls
        # for only the rules' stronger candidates.
        self.confirm_threshold = confirm_threshold

    def detect(self, text: str, post_id: str = "") -> Detection:
        start_time = time.perf_counter()
        rule_detection = self.rules.detect(text, post_id)

        has_candidate = any(m.confidence >= self.confirm_threshold for m in rule_detection.mentions)
        if not has_candidate or self.llm is None:
            return self._as_combined(rule_detection, start_time)

        try:
            llm_detection = self.llm.detect(text, post_id)
        except (GroqError, TransientSourceError) as exc:
            logger.warning("llm detector unavailable for post %s, falling back to rules: %s", post_id, exc)
            return self._as_combined(rule_detection, start_time)

        # The LLM decides the final verdict on candidates, since resolving
        # the ambiguity the rules cannot is its whole purpose. The ticker
        # sets are unioned so a ticker the rules found but the LLM omitted
        # is still reported.
        mentions_by_ticker: dict[str, TickerMention] = {m.ticker: m for m in rule_detection.mentions}
        for m in llm_detection.mentions:
            mentions_by_ticker[m.ticker] = m

        latency_ms = (time.perf_counter() - start_time) * 1000
        return Detection(
            post_id=post_id,
            is_stock_related=llm_detection.is_stock_related,
            mentions=tuple(mentions_by_ticker.values()),
            detector=self.name,
            latency_ms=latency_ms,
        )

    def _as_combined(self, rule_detection: Detection, start_time: float) -> Detection:
        latency_ms = (time.perf_counter() - start_time) * 1000
        return Detection(
            post_id=rule_detection.post_id,
            is_stock_related=rule_detection.is_stock_related,
            mentions=rule_detection.mentions,
            detector=self.name,
            latency_ms=latency_ms,
        )
