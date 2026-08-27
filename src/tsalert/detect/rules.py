from __future__ import annotations

import re
import time
from dataclasses import dataclass

from tsalert.detect.lexicon import Lexicon
from tsalert.models import Detection, TickerMention

URL_PATTERN = re.compile(r"https?://\S+")
# Share class suffixes are part of the symbol. Matching only letters turned
# $BRK.B into BRK, which is not a real symbol, so the alert named a company
# nobody mentioned instead of simply missing one.
CASHTAG_PATTERN = re.compile(r"\$([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")
BARE_TICKER_PATTERN = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")

# ---------------------------------------------------------------------------
# Two context vocabularies. This split is the central idea of the detector.
#
# The lexicon deliberately carries word-collision tickers (ALL, IT, NOW, GO,
# TRUE, GOOD, ...). Trump's posts constantly mix financial and political
# vocabulary: "market", "trade", "deal", "tariff", "jobs" show up in ordinary
# political sentences about the economy in general, naming no company at
# all. If a high ambiguity ticker match only had to clear that bar, almost
# any campaign style post would trip it.
#
# So STRONG_CONTEXT holds words that, on this corpus, essentially only show
# up when someone is actually talking about a security: "shares", "earnings",
# "ticker", "NASDAQ". A high ambiguity match requires one of these nearby or
# it is dropped outright.
#
# WEAK_CONTEXT holds words that sound financial but that Trump uses in a
# political register just as often as a financial one ("the market is up",
# "a great trade deal", "tariffs on Canada"). They are good enough to back a
# medium ambiguity bare ticker, but not enough on their own for a high
# ambiguity one, because gating high ambiguity matches on WEAK_CONTEXT alone
# is exactly what would turn ALL, IT, NOW and friends into false positives
# on this corpus.
# ---------------------------------------------------------------------------

STRONG_CONTEXT = (
    "stock",
    "stocks",
    "shares",
    "shareholder",
    "shareholders",
    "ticker",
    "NASDAQ",
    "NYSE",
    "IPO",
    "earnings",
    "market cap",
    "share price",
    "valuation",
    "dividend",
    "trading at",
    "premarket",
    "after hours",
    "short seller",
    "buy the dip",
    "all time high",
    "S&P",
    "Dow Jones",
)

WEAK_CONTEXT = (
    "market",
    "markets",
    "trade",
    "economy",
    "economic",
    "business",
    "businesses",
    "growth",
    "deal",
    "tariff",
    "tariffs",
    "jobs",
    "money",
    "dollars",
    "billion",
    "trillion",
)


def _phrase_pattern(terms) -> re.Pattern:
    escaped = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


# "the stock market" is macro language. It says nothing about a specific
# company (per the label definition, "the stock market is at an all time
# high" is macro_market, not specific_equity). Without this exclusion,
# "stock" being a STRONG_CONTEXT word would let "stock market" rubber stamp
# an unrelated high ambiguity match nearby, for example ALL in "the stock
# market is at an ALL TIME HIGH". "stock"/"stocks" keep counting as strong
# context everywhere else ("Apple stock is way up").
_STRONG_TERMS_EXCEPT_STOCK = tuple(t for t in STRONG_CONTEXT if t not in ("stock", "stocks"))
_stock_alt = r"stocks?(?!\s+markets?\b)"
_other_strong = sorted((re.escape(t) for t in _STRONG_TERMS_EXCEPT_STOCK), key=len, reverse=True)
STRONG_PATTERN = re.compile(r"\b(?:" + "|".join([_stock_alt] + _other_strong) + r")\b", re.IGNORECASE)

WEAK_PATTERN = _phrase_pattern(WEAK_CONTEXT)

FOOD_CUES = ("pie", "tree", "orchard", "sauce", "juice")
FOOD_PATTERN = _phrase_pattern(FOOD_CUES)

# Trump signs off a large share of posts "President DJT" or the fuller
# "President DONALD J. TRUMP". DJT is also his own initials, so either form
# right after "President" is how he signs off. Nothing to do with Trump Media &
# Technology Group stock. This is a real, frequent pattern in the archive.
DJT_SIGNOFF_PATTERN = re.compile(r"\bPresident\s+(?:DJT|DONALD\s+J\.?\s+TRUMP)\b", re.IGNORECASE)

# Same idea as the Apple food cue above, generalized to a handful of tickers
# whose company alias is also an everyday word for the news or intelligence
# senses of that word rather than the company. Suppressed only when a cue is
# nearby and no STRONG_CONTEXT term backs up an actual equity mention, so
# "ABC News tonight" stays quiet while "Disney stock is up" still fires.
SUPPRESSION_CUES: dict[str, tuple[str, ...]] = {
    "INTC": ("intelligence", "agencies", "agency", "memo", "briefing", "classified", "sources"),
    "NYT": ("bestselling", "bestseller", "best selling", "columnist", "op-ed", "reporter", "wrote"),
    "FOXA": ("sunday", "host", "anchor", "show", "interview", "segment", "ratings",
             "first on fox", "watching", "coverage", "reported"),
    "FOX": ("first on fox", "watching", "coverage", "host", "anchor", "show"),
    "DIS": ("news", "tonight", "hosting", "broadcast", "air", "watch", "tune in"),
    "CMCSA": ("news", "anchor", "host", "broadcast", "segment"),
}
SUPPRESSION_PATTERNS = {ticker: _phrase_pattern(cues) for ticker, cues in SUPPRESSION_CUES.items()}


@dataclass
class _Candidate:
    ticker: str
    company: str
    matched_text: str
    method: str
    confidence: float
    start: int
    end: int


class RuleDetector:
    """Lexicon and context baseline detector.

    This is the baseline every ML arm gets compared against later, so it is
    given the full lexicon rather than a cut-down one: it uses the
    full lexicon, matches cashtags, aliases, company names and bare tickers,
    and only requires context where the ambiguity of the match actually
    calls for it.
    """

    def __init__(self, lexicon: Lexicon, context_window: int = 8, threshold: float = 0.5):
        self.lexicon = lexicon
        self.context_window = context_window
        self.threshold = threshold

    def detect(self, text: str, post_id: str = "") -> Detection:
        start_time = time.perf_counter()

        # Step 0: strip URLs first so nothing matches inside a link, for
        # example a ticker-looking path segment like /statuses/TSLA.
        clean_text = URL_PATTERN.sub(" ", text)
        tokens = list(TOKEN_PATTERN.finditer(clean_text))

        candidates: list[_Candidate] = []
        candidates.extend(self._cashtag_candidates(clean_text))
        candidates.extend(self._alias_candidates(clean_text, tokens))
        candidates.extend(self._bare_ticker_candidates(clean_text, tokens))

        candidates = self._apply_hard_suppressions(candidates, clean_text, tokens)
        candidates = self._mark_index_candidates(candidates)

        best: dict[str, _Candidate] = {}
        for c in candidates:
            existing = best.get(c.ticker)
            if existing is None or c.confidence > existing.confidence:
                best[c.ticker] = c

        ordered = sorted(best.values(), key=lambda c: c.start)
        mentions = tuple(
            TickerMention(
                ticker=c.ticker,
                company=c.company,
                matched_text=c.matched_text,
                method=c.method,
                confidence=c.confidence,
            )
            for c in ordered
        )

        # Step 6: is_stock_related if any surviving mention clears threshold,
        # indices and ETFs included. The Dow and the S&P are tradeable
        # instruments with tickers, so "the Dow just crossed 50,000" names
        # something a reader can act on. They keep method="index" so error
        # analysis can still separate them from single company mentions.
        is_stock_related = any(m.confidence >= self.threshold for m in mentions)
        latency_ms = (time.perf_counter() - start_time) * 1000

        return Detection(
            post_id=post_id,
            is_stock_related=is_stock_related,
            mentions=mentions,
            detector="rules",
            latency_ms=latency_ms,
        )


    def _cashtag_candidates(self, clean_text: str) -> list[_Candidate]:
        out: list[_Candidate] = []
        for m in CASHTAG_PATTERN.finditer(clean_text):
            ticker = m.group(1).upper()
            entry = self.lexicon.get(ticker)
            if entry is not None:
                out.append(
                    _Candidate(ticker, entry.company, m.group(0), "cashtag", 0.95, m.start(), m.end())
                )
            else:
                # A cashtag naming a ticker outside our lexicon is still a
                # stock mention. Dropping it would be a recall bug, so it is
                # still emitted, just with an empty company and lower
                # confidence since we cannot confirm it against anything.
                out.append(_Candidate(ticker, "", m.group(0), "cashtag", 0.80, m.start(), m.end()))
        return out


    def _alias_candidates(self, clean_text: str, tokens: list[re.Match]) -> list[_Candidate]:
        out: list[_Candidate] = []
        pattern = self.lexicon.alias_pattern()
        for m in pattern.finditer(clean_text):
            entry = self.lexicon.entry_for_alias(m.group(0))
            if entry is None:
                continue
            # Confidence here keys off whether THIS alias is ambiguous, not
            # off entry.ambiguity, which rates the bare ticker symbol. A
            # company alias ("Truth Social") and its symbol (DJT, which
            # collides with Trump's own initials) can have opposite
            # ambiguity profiles.
            if self.lexicon.is_alias_ambiguous(entry.ticker, m.group(0)):
                window = self._context_window_text(clean_text, tokens, m.start(), m.end())
                if not STRONG_PATTERN.search(window):
                    continue
                conf = 0.65
            else:
                conf = 0.85
            out.append(
                _Candidate(entry.ticker, entry.company, m.group(0), "alias", conf, m.start(), m.end())
            )
        return out


    def _bare_ticker_candidates(self, clean_text: str, tokens: list[re.Match]) -> list[_Candidate]:
        out: list[_Candidate] = []
        for m in BARE_TICKER_PATTERN.finditer(clean_text):
            token = m.group(0)
            if token not in self.lexicon.tickers:
                continue
            entry = self.lexicon.get(token)
            window = self._context_window_text(clean_text, tokens, m.start(), m.end())
            if entry.ambiguity == "low":
                conf = 0.70
            elif entry.ambiguity == "medium":
                if not (STRONG_PATTERN.search(window) or WEAK_PATTERN.search(window)):
                    continue
                conf = 0.60
            else:  # high
                if not STRONG_PATTERN.search(window):
                    continue
                conf = 0.50
            out.append(_Candidate(entry.ticker, entry.company, token, "bare_ticker", conf, m.start(), m.end()))
        return out


    def _apply_hard_suppressions(
        self, candidates: list[_Candidate], clean_text: str, tokens: list[re.Match]
    ) -> list[_Candidate]:
        out: list[_Candidate] = []
        for c in candidates:
            if c.ticker == "DJT" and self._is_djt_signoff(clean_text, c.start, c.end):
                continue
            if c.ticker == "AAPL" and c.matched_text.strip().lower() == "apple":
                if self._is_apple_food_cue(clean_text, tokens, c.start, c.end):
                    continue
            if c.ticker in SUPPRESSION_PATTERNS and self._is_news_or_word_sense_cue(
                clean_text, tokens, c.ticker, c.start, c.end
            ):
                continue
            out.append(c)
        return out

    def _mark_index_candidates(self, candidates: list[_Candidate]) -> list[_Candidate]:
        # Relabel any candidate whose ticker is an index or ETF (SPY, QQQ,
        # DIA) as method="index" regardless of which step matched it, so
        # step 6 can exclude it from is_stock_related while still emitting it.
        out: list[_Candidate] = []
        for c in candidates:
            entry = self.lexicon.get(c.ticker)
            if entry is not None and entry.kind == "index_etf":
                c = _Candidate(c.ticker, c.company, c.matched_text, "index", c.confidence, c.start, c.end)
            out.append(c)
        return out

    def _is_djt_signoff(self, clean_text: str, start: int, end: int) -> bool:
        for m in DJT_SIGNOFF_PATTERN.finditer(clean_text):
            if m.start() <= start and end <= m.end():
                return True
        return False

    def _is_apple_food_cue(self, clean_text: str, tokens: list[re.Match], start: int, end: int) -> bool:
        # "Apple" next to a food word (pie, tree, orchard, sauce, juice) with
        # no strong equity context nearby is the fruit. AAPL is
        # curated as low ambiguity, so without this rule "I had an Apple
        # pie" would otherwise pass straight through.
        window = self._context_window_text(clean_text, tokens, start, end)
        if not FOOD_PATTERN.search(window):
            return False
        return not STRONG_PATTERN.search(window)

    def _is_news_or_word_sense_cue(
        self, clean_text: str, tokens: list[re.Match], ticker: str, start: int, end: int
    ) -> bool:
        window = self._context_window_text(clean_text, tokens, start, end)
        if not SUPPRESSION_PATTERNS[ticker].search(window):
            return False
        return not STRONG_PATTERN.search(window)


    def _context_window_text(
        self, clean_text: str, tokens: list[re.Match], match_start: int, match_end: int
    ) -> str:
        # Tokens overlapping the match are dropped from the window, so a
        # match can never be its own supporting context.
        overlap = [i for i, tok in enumerate(tokens) if tok.start() < match_end and tok.end() > match_start]
        if not overlap:
            return ""
        first_idx, last_idx = overlap[0], overlap[-1]
        lo = max(0, first_idx - self.context_window)
        hi = min(len(tokens) - 1, last_idx + self.context_window)
        before = clean_text[tokens[lo].start() : match_start]
        after = clean_text[match_end : tokens[hi].end()]
        return f"{before} {after}"
