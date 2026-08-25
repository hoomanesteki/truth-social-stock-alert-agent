#!/usr/bin/env python3
"""Build a stratified sample of history.jsonl for manual labeling.

    uv run python scripts/build_eval_set.py --history data/history.jsonl \
        --out data/eval/eval_sample.jsonl --seed 42

Why stratified at all: is_stock_related is true for a small slice of posts.
A uniform sample of 150 posts out of a corpus this size would contain almost
no positives, and recall would be unmeasurable from it. Stratifying lets us
guarantee enough positive-shaped and trap-shaped posts land in the sample to
actually measure precision and recall, while still keeping an unbiased
random slice to project results back onto the whole population.

Three strata, and each answers a different question. That is what decides
both the priority order below and how each stratum is used downstream, not
just what counts as belonging to it:

  Stratum A "candidate":     RuleDetector emits ANY mention before
                              thresholding, OR any STRONG_CONTEXT term is
                              present anywhere in the post.
                              Answers: when the system fires, how often is
                              it right? -> feeds PRECISION.
  Stratum B "random":        everything left over, sampled uniformly.
                              Answers: what does the system miss? ->
                              feeds RECALL and the base rate.
  Stratum C "hard_negative":  the post matches a known false-positive trap
                              (a DJT sign off, an all caps token that is a
                              high ambiguity ticker, "Apple" with no strong
                              context, or "stock market"/"the market" with
                              no company alias in the post).
                              Answers: does ambiguity suppression actually
                              work? -> ERROR ANALYSIS ONLY, see below.

PRIORITY is A, then C, then B, so a post lands in exactly one. A post that
both matches an alias and trips a trap (for example a Micron announcement
that also carries the "President DONALD J. TRUMP" signoff) is the single
most interesting post in the corpus for measuring precision, and since A is
a census, labeling it costs nothing extra. Checking A first means that post
is never diverted to C, where only 25 of a much larger pool get sampled and
most of the corpus's best evidence would otherwise be thrown away.

Stratum A is a CENSUS, not a sample: every post that is candidate-shaped
gets reviewed, so precision on the candidate population is measured EXACTLY
rather than estimated.

Stratum C is sampled (target 25), from the traps that were NOT already
claimed by A. The trap categories are numerous enough in the archive that a
census would be far larger than needed just to see how the detector handles
known false-positive shapes.

Stratum B is sized to fill the remainder up to 150: b_size = max(0, 150 -
len(A) - len(C_sample)), sampled uniformly from everything left after A and
C have claimed their posts. If A alone (plus the 25 from C) already reaches
or exceeds 150, stratum B is skipped entirely and the true, larger total is
reported rather than truncating the census to force the number back down.

STRATUM C IS EXCLUDED FROM THE WEIGHTED HEADLINE METRICS. Its weight field
is hardcoded to 0.0, and every row carries a boolean in_weighted_metrics
column, true for A and B, false for C. The reason: C is a PURPOSIVE sample,
not a probability sample. It was hand picked to be adversarial (every post
in it was chosen because it looks like a false positive shape), so it does
not represent the population the way a random draw does. Projecting it back
to the population with a weight of roughly 16 (413 candidates for C sampled
down to 25) would let a single labeling call on one post swing the estimate
by about 16 posts, and with something on the order of 10 true positives in
the entire corpus, that one post could dominate the result. Headline
precision and recall are computed from A and B only. C is reported
separately as a suppression stress test and feeds error analysis, not the
headline numbers. That is the honest way to use a purposive sample.

Weight, for A and B, is stratum_population / stratum_sample_size, per row.
For a census stratum (A, and B in the edge case where the whole remainder
was taken) population equals sample size, so weight is 1.0: that row does
not need to be reweighted to represent its stratum, because it already
contains 100% of it. This is what lets Break 4 report population level
precision and recall instead of numbers that only describe the 150 sampled
posts. For C, weight is always 0.0 regardless of population and sample
size, per the purposive-sample reasoning above; stratum_population and
stratum_sample_size are still recorded on C rows for reference.

Empty-text posts: a meaningful minority of the archive is media-only posts
with no text and no quoted text at all (reposts of images/video, blank
placeholder posts). A text detector cannot classify a post with nothing to
read, so these are EXCLUDED from all three strata rather than dumped into
stratum B, where they would just be free, uninformative "true negatives"
that inflate stratum B's apparent negative rate without telling us anything
about detector quality. The exact count excluded is printed in the summary
so it is visible as a stated limitation, not silently dropped.

Determinism: sampling uses random.Random(seed), called in a fixed order
(stratum C, then stratum B; stratum A has no randomness left in it), fed by
population lists built by a single, in-order pass over the history file. So
for a fixed input file and a fixed seed, output is byte identical run to
run.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tsalert.detect.lexicon import Lexicon
from tsalert.detect.rules import (
    BARE_TICKER_PATTERN,
    DJT_SIGNOFF_PATTERN,
    STRONG_PATTERN,
    URL_PATTERN,
    RuleDetector,
)
from tsalert.models import Post

LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "lexicon" / "tickers.csv"

TARGET_TOTAL = 150
TARGET_C = 25

# Stratum C trap patterns, one per trap named in the spec. TRAP_MARKET_PHRASE
# and TRAP_APPLE reuse the corpus-specific reasoning from rules.py: "stock
# market"/"the market" and a bare "Apple" are exactly the shapes that read
# as financial but are not, on their own, evidence of a specific company.
TRAP_MARKET_PHRASE = re.compile(r"\bstock market\b|\bthe market\b", re.IGNORECASE)
TRAP_APPLE = re.compile(r"\bapple\b", re.IGNORECASE)

# Which strata feed the weighted headline metrics (precision/recall) versus
# which are purposive and feed error analysis only. See module docstring.
IN_WEIGHTED_METRICS = {"candidate": True, "random": True, "hard_negative": False}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a stratified eval sample from history.jsonl.")
    parser.add_argument("--history", default="data/history.jsonl", help="input JSONL of Post records")
    parser.add_argument("--out", default="data/eval/eval_sample.jsonl", help="output JSONL path")
    parser.add_argument("--seed", type=int, default=42, help="random seed, for reproducible sampling")
    return parser.parse_args(argv)


def _is_hard_negative(clean_text: str, lexicon: Lexicon, high_ambiguity_tickers: frozenset[str]) -> bool:
    # Trap 1: a DJT sign off, his initials rather than a ticker mention.
    if DJT_SIGNOFF_PATTERN.search(clean_text):
        return True
    # Trap 2: an all caps token that happens to be a high ambiguity ticker
    # (ALL, IT, NOW, ...), the classic word-collision false positive shape.
    for m in BARE_TICKER_PATTERN.finditer(clean_text):
        if m.group(0) in high_ambiguity_tickers:
            return True
    # Trap 3: "Apple" with nothing nearby that reads as equity language, the
    # fruit/company ambiguity that AAPL is curated with.
    if TRAP_APPLE.search(clean_text) and not STRONG_PATTERN.search(clean_text):
        return True
    # Trap 4: generic market talk ("stock market", "the market") with no
    # company alias anywhere in the post, macro_market rather than a named
    # equity.
    if TRAP_MARKET_PHRASE.search(clean_text) and not lexicon.alias_pattern().search(clean_text):
        return True
    return False


def _is_candidate(mentions_present: bool, clean_text: str) -> bool:
    return mentions_present or bool(STRONG_PATTERN.search(clean_text))


def _weight(stratum_name: str, population: int, sample_size: int) -> float:
    # Stratum C is a purposive sample, not a probability sample, so it is
    # never reweighted back onto the population. See module docstring.
    if not IN_WEIGHTED_METRICS[stratum_name]:
        return 0.0
    return population / sample_size if sample_size else 0.0


def build_sample(history_path: Path, seed: int) -> tuple[list[dict], dict]:
    lexicon = Lexicon.load(LEXICON_PATH)
    detector = RuleDetector(lexicon)
    # lexicon.tickers only ever contains tickers the lexicon itself defines,
    # so get() is never None here.
    high_ambiguity_tickers = frozenset(t for t in lexicon.tickers if lexicon.get(t).ambiguity == "high")

    pool_a: list[Post] = []
    pool_c: list[Post] = []
    pool_b: list[Post] = []
    excluded_empty = 0
    posts_read = 0

    with history_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            posts_read += 1
            post = Post.from_dict(json.loads(line))
            text = post.detection_text
            if not text.strip():
                excluded_empty += 1
                continue

            clean_text = URL_PATTERN.sub(" ", text)
            detection = detector.detect(text, post_id=post.id)

            # Priority A, then C, then B: a post that is both candidate
            # shaped and trap shaped is our best precision evidence, and A
            # is a census, so it must not be diverted to the much more
            # sparsely sampled C.
            if _is_candidate(bool(detection.mentions), clean_text):
                pool_a.append(post)
            elif _is_hard_negative(clean_text, lexicon, high_ambiguity_tickers):
                pool_c.append(post)
            else:
                pool_b.append(post)

    rng = random.Random(seed)

    population_a = len(pool_a)
    sample_a = list(pool_a)
    size_a = population_a

    population_c = len(pool_c)
    size_c = min(TARGET_C, population_c)
    sample_c = rng.sample(pool_c, k=size_c) if size_c else []

    population_b = len(pool_b)
    target_b = max(0, TARGET_TOTAL - size_a - size_c)
    size_b = min(target_b, population_b)
    sample_b = rng.sample(pool_b, k=size_b) if size_b else []

    strata = [
        ("candidate", sample_a, population_a, size_a),
        ("hard_negative", sample_c, population_c, size_c),
        ("random", sample_b, population_b, size_b),
    ]

    rows: list[dict] = []
    for stratum_name, sample, population, sample_size in strata:
        weight = _weight(stratum_name, population, sample_size)
        in_weighted_metrics = IN_WEIGHTED_METRICS[stratum_name]
        for post in sample:
            rows.append(
                {
                    "post_id": post.id,
                    "text": post.detection_text,
                    "created_at": post.created_at.isoformat(),
                    "url": post.url,
                    "stratum": stratum_name,
                    "stratum_population": population,
                    "stratum_sample_size": sample_size,
                    "weight": weight,
                    "in_weighted_metrics": in_weighted_metrics,
                }
            )

    summary = {
        "posts_read": posts_read,
        "excluded_empty_text": excluded_empty,
        "strata": [
            {
                "name": name,
                "population": population,
                "sample_size": sample_size,
                "weight": _weight(name, population, sample_size),
                "in_weighted_metrics": IN_WEIGHTED_METRICS[name],
            }
            for name, _sample, population, sample_size in strata
        ],
        "total_sampled": size_a + size_c + size_b,
    }
    return rows, summary


def print_summary(summary: dict) -> None:
    print(f"posts read: {summary['posts_read']}")
    print(f"excluded (empty text): {summary['excluded_empty_text']}")
    print(f"{'stratum':<16}{'population':>12}{'sample_size':>14}{'weight':>10}{'in_weighted_metrics':>22}")
    for s in summary["strata"]:
        weight_str = f"{s['weight']:.4f}"
        print(f"{s['name']:<16}{s['population']:>12}{s['sample_size']:>14}{weight_str:>10}{str(s['in_weighted_metrics']):>22}")
    print(f"total sampled: {summary['total_sampled']}")

    weighted = [s["name"] for s in summary["strata"] if s["in_weighted_metrics"]]
    unweighted = [s["name"] for s in summary["strata"] if not s["in_weighted_metrics"]]
    print(
        "weighted headline metrics (precision/recall) are computed from: "
        + ", ".join(weighted)
        + ". "
        + ", ".join(unweighted)
        + " is purposive (weight 0.0, in_weighted_metrics false), used only for "
        "suppression error analysis, not the headline numbers."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    history_path = Path(args.history)
    out_path = Path(args.out)

    rows, summary = build_sample(history_path, args.seed)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
