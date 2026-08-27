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


# Defect 1: an unambiguous company alias must not inherit the ambiguity
# rating of its ticker symbol. "Truth Social" and "Trump Media" are not
# ambiguous English even though DJT the symbol collides with Trump's
# initials.


def test_truth_social_alias_fires_djt(detector: RuleDetector):
    d = detector.detect("Truth Social is doing incredibly well")
    assert d.is_stock_related is True
    assert "DJT" in _tickers(d)


def test_trump_media_alias_fires_djt(detector: RuleDetector):
    d = detector.detect("Trump Media had a huge day")
    assert d.is_stock_related is True
    assert "DJT" in _tickers(d)


def test_president_djt_signoff_still_suppressed(detector: RuleDetector):
    # Regression guard: must still pass after the alias ambiguity fix.
    d = detector.detect("Thank you. President DJT")
    assert d.is_stock_related is False


def test_djt_cashtag_still_fires(detector: RuleDetector):
    # Regression guard: must still pass after the alias ambiguity fix.
    d = detector.detect("$DJT is going higher")
    assert d.is_stock_related is True
    assert "DJT" in _tickers(d)


def test_apple_food_cue_still_suppressed(detector: RuleDetector):
    # Regression guard: must still pass after the alias ambiguity fix.
    d = detector.detect("I had an Apple pie yesterday")
    assert d.is_stock_related is False


def test_apple_stock_still_fires(detector: RuleDetector):
    # Regression guard: must still pass after the alias ambiguity fix.
    d = detector.detect("Apple stock is way up")
    assert d.is_stock_related is True
    assert "AAPL" in _tickers(d)


def test_doug_ford_surname_not_ford_motor(detector: RuleDetector):
    d = detector.detect("Lots of bluster from Doug Ford, the Premier")
    assert d.is_stock_related is False


def test_intel_politics_headline_not_intc(detector: RuleDetector):
    d = detector.detect("Intel Politics: NSA sat on election threat reporting")
    assert d.is_stock_related is False


# Defect 2: indices and ETFs are index_or_etf in the label definition, not
# specific_equity, so a mention of one alone must not flip is_stock_related.


def test_dow_jones_counts_as_a_stock_mention(detector: RuleDetector):
    """Indices and ETFs count. They are tradeable and they have tickers."""
    d = detector.detect("The Dow Jones just crossed 50,000")
    assert d.is_stock_related is True
    assert "DIA" in {m.ticker for m in d.mentions}


def test_nasdaq_counts_as_a_stock_mention(detector: RuleDetector):
    d = detector.detect("The Nasdaq hit a record high")
    assert d.is_stock_related is True
    assert "QQQ" in {m.ticker for m in d.mentions}


def test_index_mention_alongside_company_still_stock_related(detector: RuleDetector):
    d = detector.detect("Tesla is up and so is the Dow Jones")
    assert d.is_stock_related is True
    assert "TSLA" in _tickers(d)


def test_micron_new_fab_fires_mu(detector: RuleDetector):
    d = detector.detect("Micron announced a new fab")
    assert d.is_stock_related is True
    assert "MU" in _tickers(d)


# Defect 5: the alias arm needs a context gate for word sense collisions
# that are not in the ambiguous_aliases list, using the same suppression
# cue idea as the Apple food cue.


def test_intel_politics_headline_suppressed_by_cue(detector: RuleDetector):
    d = detector.detect("Intel Politics: NSA sat on election threat reporting")
    assert d.is_stock_related is False


def test_bad_intel_word_sense_suppressed(detector: RuleDetector):
    d = detector.detect("our intelligence agencies had bad intel")
    assert d.is_stock_related is False


def test_intel_earnings_context_still_fires(detector: RuleDetector):
    d = detector.detect("Intel earnings beat expectations this quarter")
    assert d.is_stock_related is True
    assert "INTC" in _tickers(d)


def test_nyt_bestselling_author_suppressed(detector: RuleDetector):
    d = detector.detect("New York Times Bestselling Author")
    assert d.is_stock_related is False


def test_nyt_stock_context_still_fires(detector: RuleDetector):
    d = detector.detect("NYT stock jumped after earnings")
    assert d.is_stock_related is True
    assert "NYT" in _tickers(d)


def test_fox_news_sunday_suppressed(detector: RuleDetector):
    d = detector.detect("Fox News Sunday is so biased")
    assert d.is_stock_related is False


def test_fox_corporation_shares_still_fires(detector: RuleDetector):
    d = detector.detect("Fox Corporation shares are up today")
    assert d.is_stock_related is True
    assert "FOXA" in _tickers(d)


def test_abc_news_tonight_suppressed(detector: RuleDetector):
    d = detector.detect("Tonight at 9 PM ET, on ABC News")
    assert d.is_stock_related is False


def test_disney_stock_still_fires(detector: RuleDetector):
    d = detector.detect("Disney stock is up")
    assert d.is_stock_related is True
    assert "DIS" in _tickers(d)
