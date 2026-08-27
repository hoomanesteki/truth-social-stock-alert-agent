from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


class LexiconError(ValueError):
    """The lexicon file is missing or malformed."""


@dataclass(frozen=True)
class LexiconEntry:
    ticker: str
    company: str
    aliases: tuple[str, ...]
    ambiguity: str
    ambiguous_aliases: tuple[str, ...]
    kind: str
    notes: str


class Lexicon:
    """Ticker vocabulary loaded from the hand curated tickers.csv.

    For most rows the aliases column already spells out the company name
    (TSLA lists "Tesla" as one of its own aliases, AAPL lists "Apple"), but a
    few rows list only alternate names and skip the bare company name (LCID
    lists "Lucid Motors" and "Lucid Group" but not plain "Lucid"). The rule
    detector needs to match against "aliases and company names" as one pool,
    so on load, each entry's own company name is folded into its aliases
    tuple when it is not already present. This only changes what is held in
    memory. The csv on disk is never written to.
    """

    def __init__(self, entries: dict[str, LexiconEntry]) -> None:
        self._entries = entries
        # Lowercased alias/company text -> ticker, for turning a regex match
        # back into the entry that produced it. Built once at load time.
        self._match_index: dict[str, str] = {}
        for entry in entries.values():
            for term in entry.aliases:
                key = term.lower()
                self._match_index.setdefault(key, entry.ticker)
        self._pattern: re.Pattern | None = None

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        entries: dict[str, LexiconEntry] = {}
        with Path(path).open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            required = {"ticker", "company", "aliases", "ambiguity"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                # A KeyError halfway through the file tells you nothing about
                # which file or which column, and detection silently loses
                # whatever did not load.
                raise LexiconError(
                    f"{path} is missing required column(s): {', '.join(sorted(missing))}"
                )
            for row in reader:
                ticker = (row.get("ticker") or "").strip()
                if not ticker:
                    continue
                if ticker in entries:
                    logger.warning("%s lists %s more than once, keeping the last row", path, ticker)
                company = (row.get("company") or "").strip()
                raw_aliases = [a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()]
                seen_lower = {a.lower() for a in raw_aliases}
                if company and company.lower() not in seen_lower:
                    raw_aliases.append(company)
                # Older csv files may not have these columns yet, so default
                # to empty and "equity" rather than requiring a migration.
                ambiguous_raw = row.get("ambiguous_aliases", "") or ""
                ambiguous_aliases = tuple(a.strip() for a in ambiguous_raw.split("|") if a.strip())
                kind = (row.get("kind", "") or "equity").strip() or "equity"
                entries[ticker] = LexiconEntry(
                    ticker=ticker,
                    company=company,
                    aliases=tuple(raw_aliases),
                    ambiguity=(row.get("ambiguity") or "").strip(),
                    ambiguous_aliases=ambiguous_aliases,
                    kind=kind,
                    # .get with a default, like kind and ambiguous_aliases
                    # above: notes is not in `required`, so a CSV without the
                    # column was raising a bare KeyError past LexiconError and
                    # out of startup. A short row leaves the value None rather
                    # than absent, so the `or ""` covers that too.
                    notes=(row.get("notes") or "").strip(),
                )
        return cls(entries)

    def get(self, ticker: str) -> LexiconEntry | None:
        return self._entries.get(ticker.strip().upper())

    @property
    def tickers(self) -> frozenset[str]:
        return frozenset(self._entries)

    def is_alias_ambiguous(self, ticker: str, alias: str) -> bool:
        """Whether a specific alias of a ticker needs strong context to fire.

        This is separate from entry.ambiguity, which rates the bare symbol.
        An alias like "Truth Social" is not ambiguous even when its ticker's
        symbol is (DJT collides with Trump's initials).
        """
        entry = self.get(ticker)
        if entry is None:
            return False
        alias_lower = alias.lower()
        return any(a.lower() == alias_lower for a in entry.ambiguous_aliases)

    def entry_for_alias(self, matched_text: str) -> LexiconEntry | None:
        """Look up the entry that owns an alias or company name match.

        matched_text is whatever alias_pattern() matched, so lookup is case
        insensitive by construction.
        """
        ticker = self._match_index.get(matched_text.lower())
        return self._entries.get(ticker) if ticker else None

    def alias_pattern(self) -> re.Pattern:
        """One alternation regex over every alias and company name.

        Sorted longest first so a longer alias always wins over a shorter
        one that happens to start at the same position ("Trump Media and
        Technology" over "Trump Media", "Trump Media" over "TMTG" if they
        ever overlapped). Each term is word bounded so "AT&T" cannot match
        inside a longer token, and the whole thing is case insensitive.
        """
        if self._pattern is None:
            terms = sorted(self._match_index, key=len, reverse=True)
            escaped = [re.escape(t) for t in terms]
            self._pattern = re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)
        return self._pattern
