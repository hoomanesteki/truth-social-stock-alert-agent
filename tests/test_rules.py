from __future__ import annotations

from pathlib import Path

import pytest

from tsalert.detect.lexicon import Lexicon
from tsalert.detect.rules import RuleDetector

LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "lexicon" / "tickers.csv"


@pytest.fixture(scope="module")
def detector() -> RuleDetector:
    lexicon = Lexicon.load(LEXICON_PATH)
    return RuleDetector(lexicon)


def _tickers(detection):
    return {m.ticker for m in detection.mentions}


# The worked examples from the spec, used verbatim as test cases.


def test_cashtag_known_ticker(detector: RuleDetector):
    d = detector.detect("$DJT is going to the moon")
    assert d.is_stock_related is True
    assert "DJT" in _tickers(d)


def test_cashtag_unknown_ticker_still_emitted(detector: RuleDetector):
    d = detector.detect("$ZZZZ soaring")
    assert d.is_stock_related is True
    assert "ZZZZ" in _tickers(d)
    mention = next(m for m in d.mentions if m.ticker == "ZZZZ")
    assert mention.company == ""
    assert mention.method == "cashtag"


def test_alias_low_ambiguity_company_name(detector: RuleDetector):
    d = detector.detect("Tesla is a great American company")
    assert d.is_stock_related is True
    assert "TSLA" in _tickers(d)


def test_alias_medium_ambiguity_company_name(detector: RuleDetector):
    d = detector.detect("Boeing is building the new Air Force One")
    assert d.is_stock_related is True
    assert "BA" in _tickers(d)


def test_president_djt_signoff_suppressed(detector: RuleDetector):
    d = detector.detect("President DJT")
    assert d.is_stock_related is False


def test_president_donald_j_trump_signoff_suppressed(detector: RuleDetector):
    d = detector.detect("Thank you. President DONALD J. TRUMP")
    assert d.is_stock_related is False


def test_apple_food_cue_suppressed(detector: RuleDetector):
    d = detector.detect("I had an Apple pie")
    assert d.is_stock_related is False


def test_apple_with_strong_context_not_suppressed(detector: RuleDetector):
    d = detector.detect("Apple stock is way up")
    assert d.is_stock_related is True
    assert "AAPL" in _tickers(d)


def test_high_ambiguity_bare_ticker_without_context_dropped(detector: RuleDetector):
    d = detector.detect("GET OUT AND VOTE, ALL OF YOU!")
    assert d.is_stock_related is False


def test_two_high_ambiguity_bare_tickers_without_context_dropped(detector: RuleDetector):
    d = detector.detect("IT IS TIME TO ACT NOW")
    assert d.is_stock_related is False


def test_high_ambiguity_alias_with_strong_context_kept(detector: RuleDetector):
    d = detector.detect("Allstate shares fell")
    assert d.is_stock_related is True
    assert "ALL" in _tickers(d)


def test_stock_market_phrase_does_not_promote_bare_ticker(detector: RuleDetector):
    d = detector.detect("the stock market is at an ALL TIME HIGH")
    assert d.is_stock_related is False


def test_url_ticker_lookalike_not_matched(detector: RuleDetector):
    d = detector.detect("Check this out: https://truthsocial.com/@user/statuses/TSLA")
    assert d.is_stock_related is False


# A few additional cases covering behavior not in the worked examples.


def test_detection_carries_post_id_and_detector_name(detector: RuleDetector):
    d = detector.detect("$DJT is going to the moon", post_id="abc123")
    assert d.post_id == "abc123"
    assert d.detector == "rules"
    assert d.latency_ms >= 0


def test_duplicate_mentions_deduplicated_by_highest_confidence(detector: RuleDetector):
    # "$AAPL" is both a cashtag (0.95) and, via the bare ticker path, would
    # also read as the token AAPL, but AAPL is low ambiguity so it is picked
    # up by alias matching too ("Apple"). Only one AAPL mention should survive.
    d = detector.detect("$AAPL Apple stock is way up")
    aapl_mentions = [m for m in d.mentions if m.ticker == "AAPL"]
    assert len(aapl_mentions) == 1
    assert aapl_mentions[0].confidence == 0.95
    assert aapl_mentions[0].method == "cashtag"


def test_no_mentions_for_plain_text(detector: RuleDetector):
    d = detector.detect("Have a wonderful day everyone")
    assert d.is_stock_related is False
    assert d.mentions == ()
