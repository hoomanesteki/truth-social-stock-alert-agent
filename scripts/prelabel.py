#!/usr/bin/env python3
"""Pre-label eval posts with the Groq LLM, independently of the rule detector.

    uv run python scripts/prelabel.py --in data/eval/eval_sample.jsonl \
        --out data/eval/prelabels.jsonl --model openai/gpt-oss-120b

Resumable: post_ids already present in --out are skipped on restart. The
GroqClient's own on-disk cache (--cache) means even a from-scratch rerun of
a post that was already labeled costs zero tokens, since the request/model
pair hashes to the same cache key.

The rule detector's output is never included in the prompt. The LLM must
label each post independently of the rule-based detector, or the precision
and recall comparison would end up measuring the LLM against a
detector it had already seen the answer from.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tsalert.config import Config
from tsalert.llm import GroqClient, GroqError

SYSTEM_PROMPT = """You label social media posts for a stock-mention detector. Follow the definition exactly.

is_stock_related is TRUE only when the post refers to a SPECIFIC, identifiable, publicly
traded company, either by name or by ticker symbol.

category must be exactly one of:
  specific_equity  a named public company or its ticker            -> is_stock_related true
  index_or_etf     S&P 500, Nasdaq, the Dow, SPY, QQQ              -> is_stock_related false
  macro_market     generic market or economy talk, no named entity -> is_stock_related false
  not_financial    everything else                                 -> is_stock_related false

Rules that decide the hard cases:
- A company counts even without investment framing. "Boeing is building Air Force One"
  is specific_equity.
- A word that merely looks like a company does not count. Judge the actual meaning in
  context: "Doug Ford" is a person, "intel" can mean intelligence, "target" can be a verb,
  "New York Times Bestseller" refers to a list, "US steel production" refers to the material.
- "DJT" is the ticker for Trump Media, but Trump signs posts "President DONALD J. TRUMP"
  and "President DJT" using his own initials. A signature is not_financial.
- A bare URL to a site like youtube.com or instagram.com is not a mention of that company.
- Media brands map to their listed parent only when the post is about the company itself,
  not when it is merely citing or appearing on that outlet.

Return ONLY JSON: {"is_stock_related": bool, "category": str, "tickers": [str],
"companies": [str], "reasoning": "one short sentence"}"""

REQUIRED_LABEL_KEYS = ("is_stock_related", "category", "tickers", "companies", "reasoning")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-label eval posts with the Groq LLM, independently of the rule detector."
    )
    parser.add_argument("--in", dest="in_path", required=True, help="input JSONL, one post per line")
    parser.add_argument("--out", dest="out_path", required=True, help="output JSONL of labels")
    parser.add_argument("--model", default="openai/gpt-oss-120b", help="Groq model id")
    parser.add_argument("--cache", default="data/eval/llm_cache.jsonl", help="GroqClient cache file")
    parser.add_argument("--timeout", type=int, default=90, help="per-request timeout in seconds")
    parser.add_argument(
        "--min-request-interval",
        type=float,
        default=0.5,
        dest="min_request_interval",
        help="minimum seconds between requests",
    )
    return parser.parse_args(argv)


def read_posts(in_path: Path) -> list[dict]:
    posts = []
    with in_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            posts.append(json.loads(line))
    return posts


def already_labeled_ids(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    done = set()
    with out_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            post_id = record.get("post_id")
            if post_id is not None:
                done.add(str(post_id))
    return done


def validate_label(label: dict) -> None:
    missing = [key for key in REQUIRED_LABEL_KEYS if key not in label]
    if missing:
        raise GroqError(f"label missing required keys: {missing}")


def label_post(client: GroqClient, post: dict) -> dict:
    text = post.get("text", "")
    user_message = f"Post:\n{text}"
    # gpt-oss-120b is a reasoning model and its reasoning tokens come out of the
    # same max_tokens budget as the answer. A small budget gets spent thinking and
    # the JSON never closes, which Groq rejects with json_validate_failed.
    label = client.complete_json(SYSTEM_PROMPT, user_message, max_tokens=4000)
    validate_label(label)
    return {
        "post_id": post["post_id"],
        "is_stock_related": label["is_stock_related"],
        "category": label["category"],
        "tickers": label["tickers"],
        "companies": label["companies"],
        "reasoning": label["reasoning"],
        "model": client.model,
        "cached": client.last_cache_hit,
    }


def run(client: GroqClient, in_path: Path, out_path: Path) -> dict:
    posts = read_posts(in_path)
    skip_ids = already_labeled_ids(out_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    labeled = 0
    skipped = 0
    cache_hits = 0
    category_counts: Counter[str] = Counter()

    with out_path.open("a", encoding="utf-8") as out_f:
        for post in posts:
            post_id = str(post["post_id"])
            if post_id in skip_ids:
                skipped += 1
                continue

            row = label_post(client, post)
            out_f.write(json.dumps(row) + "\n")
            out_f.flush()

            labeled += 1
            if row["cached"]:
                cache_hits += 1
            category_counts[row["category"]] += 1

            if labeled % 10 == 0:
                print(f"progress: {labeled} labeled, {cache_hits} cache hits")

    summary = {
        "labeled": labeled,
        "skipped": skipped,
        "cache_hits": cache_hits,
        "category_counts": dict(category_counts),
    }
    return summary


def print_summary(summary: dict) -> None:
    print(f"labeled: {summary['labeled']}")
    print(f"skipped (already present): {summary['skipped']}")
    print(f"cache hits: {summary['cache_hits']}")
    print("category distribution:")
    for category, count in sorted(summary["category_counts"].items()):
        print(f"  {category}: {count}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    config = Config.from_env()
    api_key = config.groq_api_key
    if not api_key:
        print("GROQ_API_KEY is not set in the environment", file=sys.stderr)
        return 1

    client = GroqClient(
        api_key=api_key,
        model=args.model,
        timeout=args.timeout,
        cache_path=args.cache,
        min_request_interval=args.min_request_interval,
    )

    summary = run(client, Path(args.in_path), Path(args.out_path))
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
