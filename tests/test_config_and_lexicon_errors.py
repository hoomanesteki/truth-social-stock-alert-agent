"""Bad input should fail with something you can act on.

These came out of feeding the loaders junk by hand. A negative poll interval
turned every sleep into zero, and a lexicon with a missing column raised a
KeyError from inside a loop that named neither the file nor the column.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tsalert.config import Config, ConfigError
from tsalert.detect.lexicon import Lexicon, LexiconError

HEADER = "ticker,company,aliases,ambiguity,ambiguous_aliases,kind,notes\n"


@pytest.mark.parametrize("value", ["-5", "0", "abc", ""])
def test_bad_poll_interval_is_rejected(monkeypatch, value):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", value)
    with pytest.raises(ConfigError):
        Config.from_env(None)


def test_negative_alarm_hours_rejected(monkeypatch):
    monkeypatch.setenv("NO_POSTS_ALARM_HOURS", "-1")
    with pytest.raises(ConfigError):
        Config.from_env(None)


def test_good_values_still_load(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "90")
    assert Config.from_env(None).poll_interval_seconds == 90


@pytest.mark.parametrize(
    "body",
    ["", "ticker,company\nAAPL,Apple\n", "!!!not,a,csv\n"],
    ids=["empty", "missing_columns", "junk"],
)
def test_malformed_lexicon_names_the_problem(tmp_path, body):
    path = tmp_path / "lx.csv"
    path.write_text(body)
    with pytest.raises(LexiconError) as exc:
        Lexicon.load(path)
    assert str(path) in str(exc.value)


def test_duplicate_ticker_keeps_the_last_row(tmp_path, caplog):
    path = tmp_path / "lx.csv"
    path.write_text(HEADER + "AAPL,Apple,Apple,low,,equity,\nAAPL,Apple Two,Two,low,,equity,\n")
    lex = Lexicon.load(path)
    assert lex.get("AAPL").company == "Apple Two"
    assert "more than once" in caplog.text


def test_blank_ticker_row_is_skipped(tmp_path):
    path = tmp_path / "lx.csv"
    path.write_text(HEADER + ",,,,,,\nAAPL,Apple,Apple,low,,equity,\n")
    assert Lexicon.load(path).tickers == frozenset({"AAPL"})
