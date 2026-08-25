from __future__ import annotations

from pathlib import Path

from tsalert.detect.lexicon import Lexicon

LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "lexicon" / "tickers.csv"


def load_lexicon() -> Lexicon:
    return Lexicon.load(LEXICON_PATH)


def test_loads_all_95_rows():
    lexicon = load_lexicon()
    assert len(lexicon.tickers) == 95


def test_known_tickers_present():
    lexicon = load_lexicon()
    for ticker in ("DJT", "TSLA", "AAPL", "BA", "ALL", "IT", "SPY"):
        assert ticker in lexicon.tickers
        entry = lexicon.get(ticker)
        assert entry is not None
        assert entry.ticker == ticker


def test_get_is_case_insensitive():
    lexicon = load_lexicon()
    assert lexicon.get("tsla") is not None
    assert lexicon.get("tsla").ticker == "TSLA"


def test_unknown_ticker_returns_none():
    lexicon = load_lexicon()
    assert lexicon.get("ZZZZ") is None
    assert lexicon.get("") is None


def test_alias_pattern_prefers_longest_match():
    lexicon = load_lexicon()
    pattern = lexicon.alias_pattern()
    # DJT's aliases include both "Trump Media" and the longer "Trump Media
    # and Technology". At the same starting position the longer one must win.
    match = pattern.search("Trump Media and Technology reported earnings")
    assert match is not None
    assert match.group(0) == "Trump Media and Technology"


def test_alias_pattern_is_word_bounded_and_case_insensitive():
    lexicon = load_lexicon()
    pattern = lexicon.alias_pattern()
    # "Teslas" should not match the "Tesla" alias since it is not a whole word.
    assert pattern.search("I bought some Teslas today") is None
    match = pattern.search("i love tesla cars")
    assert match is not None
    assert match.group(0).lower() == "tesla"


def test_entry_for_alias_resolves_ticker():
    lexicon = load_lexicon()
    entry = lexicon.entry_for_alias("Tesla")
    assert entry is not None
    assert entry.ticker == "TSLA"
    assert lexicon.entry_for_alias("not a real alias") is None
