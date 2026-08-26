from __future__ import annotations

import json
from pathlib import Path

import pytest

from tsalert.detect.combined import CombinedDetector
from tsalert.detect.lexicon import Lexicon
from tsalert.detect.llm_detector import LlmDetector
from tsalert.detect.rules import RuleDetector
from tsalert.llm import GroqClient, GroqError

# Load the real lexicon rather than hand building LexiconEntry objects. The
# entry schema changes as tickers get added, but Lexicon.load() and
# Lexicon.get() stay put, so tests depend on those instead. TSLA is present per
# lexicon.py's own docstring ("TSLA lists 'Tesla' as one of its own
# aliases").
_LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "lexicon" / "tickers.csv"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = {}

    def json(self):
        return self._json_body


def _good_body(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def _client(transport, **kwargs) -> GroqClient:
    return GroqClient(
        api_key="fake-key",
        model="qwen/qwen3.6-27b",
        transport=transport,
        sleep=lambda s: None,
        **kwargs,
    )


def _lexicon() -> Lexicon:
    return Lexicon.load(_LEXICON_PATH)


def _rule_detector() -> RuleDetector:
    return RuleDetector(_lexicon())


# ---------------------------------------------------------------------------
# LlmDetector
# ---------------------------------------------------------------------------


def test_detect_maps_known_json_label_to_detection():
    def transport(url, payload):
        return FakeResponse(
            200,
            json_body=_good_body(
                {
                    "is_stock_related": True,
                    "category": "specific_equity",
                    "tickers": ["tsla"],
                    "companies": ["Tesla"],
                    "reasoning": "mentions Tesla",
                }
            ),
        )

    lexicon = _lexicon()
    client = _client(transport)
    detector = LlmDetector(client, lexicon=lexicon)

    detection = detector.detect("Tesla stock is way up today", post_id="1")

    assert detection.post_id == "1"
    assert detection.detector == "llm"
    assert detection.is_stock_related is True
    assert len(detection.mentions) == 1
    mention = detection.mentions[0]
    assert mention.ticker == "TSLA"
    # Company comes from the curated lexicon, not the model's own text.
    assert mention.company == lexicon.get("TSLA").company
    assert mention.method == "llm"
    assert mention.confidence == 0.9


def test_detect_negative_label_has_no_mentions():
    def transport(url, payload):
        return FakeResponse(
            200,
            json_body=_good_body(
                {
                    "is_stock_related": False,
                    "category": "not_financial",
                    "tickers": [],
                    "companies": [],
                    "reasoning": "no company named",
                }
            ),
        )

    client = _client(transport)
    detector = LlmDetector(client)

    detection = detector.detect("Great weather today", post_id="2")

    assert detection.is_stock_related is False
    assert detection.mentions == ()


def test_groq_error_propagates_and_is_not_swallowed():
    def transport(url, payload):
        return FakeResponse(200, json_body={"choices": [{"message": {"content": "not json"}}]})

    client = _client(transport)
    detector = LlmDetector(client)

    with pytest.raises(GroqError):
        detector.detect("some post", post_id="3")


def test_malformed_label_missing_required_key_raises_groq_error():
    def transport(url, payload):
        return FakeResponse(200, json_body=_good_body({"category": "not_financial"}))

    client = _client(transport)
    detector = LlmDetector(client)

    with pytest.raises(GroqError):
        detector.detect("some post", post_id="4")


# ---------------------------------------------------------------------------
# CombinedDetector
#
# These use a $CASHTAG in the post text so the rule detector always emits a
# candidate regardless of a ticker's ambiguity tier or curated context words,
# neither of which these tests want to depend on.
# ---------------------------------------------------------------------------


def test_combined_does_not_call_llm_when_rules_find_no_candidate():
    calls = []

    def transport(url, payload):
        calls.append(payload)
        return FakeResponse(
            200,
            json_body=_good_body({"is_stock_related": True, "tickers": ["TSLA"], "companies": ["Tesla"]}),
        )

    llm = LlmDetector(_client(transport))
    combined = CombinedDetector(_rule_detector(), llm)

    detection = combined.detect("Just a normal political post about the economy", post_id="5")

    assert calls == []
    assert detection.detector == "combined"
    assert detection.is_stock_related is False


def test_combined_calls_llm_when_rules_find_a_candidate():
    calls = []

    def transport(url, payload):
        calls.append(payload)
        return FakeResponse(
            200,
            json_body=_good_body({"is_stock_related": True, "tickers": ["TSLA"], "companies": ["Tesla"]}),
        )

    llm = LlmDetector(_client(transport))
    combined = CombinedDetector(_rule_detector(), llm)

    detection = combined.detect("$TSLA is booming today", post_id="6")

    assert len(calls) == 1
    assert detection.detector == "combined"
    assert detection.is_stock_related is True


def test_combined_falls_back_to_rule_verdict_when_llm_raises():
    def transport(url, payload):
        return FakeResponse(200, json_body={"choices": [{"message": {"content": "not json"}}]})

    llm = LlmDetector(_client(transport))
    combined = CombinedDetector(_rule_detector(), llm)

    detection = combined.detect("$TSLA is booming today", post_id="7")

    # Falls back to whatever the rule detector alone decided, not a crash
    # and not a silent negative.
    rule_only = _rule_detector().detect("$TSLA is booming today", post_id="7")
    assert detection.is_stock_related == rule_only.is_stock_related
    assert {m.ticker for m in detection.mentions} == {m.ticker for m in rule_only.mentions}
    assert detection.detector == "combined"


def test_combined_unions_ticker_sets():
    # Rules find TSLA via the cashtag. The LLM's verdict names a different,
    # made-up ticker that is not in the lexicon at all, so the only way it
    # can appear in the final mentions is through the union.
    def transport(url, payload):
        return FakeResponse(
            200,
            json_body=_good_body(
                {"is_stock_related": True, "tickers": ["ZQXY"], "companies": ["Zqxy Corp"]}
            ),
        )

    llm = LlmDetector(_client(transport))
    combined = CombinedDetector(_rule_detector(), llm)

    detection = combined.detect("$TSLA and some other company both up today", post_id="8")

    tickers = {m.ticker for m in detection.mentions}
    assert "TSLA" in tickers
    assert "ZQXY" in tickers


def test_combined_with_no_llm_configured_uses_rules_only():
    combined = CombinedDetector(_rule_detector(), llm=None)

    detection = combined.detect("$TSLA is booming today", post_id="9")

    assert detection.detector == "combined"
    assert detection.is_stock_related is True
    assert any(m.ticker == "TSLA" for m in detection.mentions)
