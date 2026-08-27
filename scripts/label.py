#!/usr/bin/env python3
"""Human review CLI for the eval sample. Produces the final label per post.

    uv run python scripts/label.py --sample data/eval/eval_sample.jsonl \
        --prelabels data/eval/prelabels.jsonl --out data/eval/labeled.jsonl \
        --blind-count 30 --seed 42

The blind holdout is the point of the design here. The LLM arm
is partly graded against labels the LLM helped produce. That is
contamination. To measure how much it is worth, a fixed number of posts
(blind-count) are selected deterministically, proportionally across strata,
using the given seed. Those posts are presented FIRST, with no proposed
label shown at all, so they are labeled with zero exposure to the model's
opinion. Comparing the LLM arm's score on that blind subset against its
score on the reviewed subset (where the human saw the proposal before
deciding) measures how much seeing the proposal actually moved the human,
which tells us how much seeing the proposal actually moved the reviewer.
Only after every blind post is done does the CLI start showing proposals
for the rest of the sample.

This CLI is what a human sits in front of for about an hour, so correctness
of saved data matters more than pretty output:
  - every accepted decision is appended to the output file and flushed
    immediately, so a Ctrl-C at any moment loses no completed work.
  - on restart, post_ids already present in the output file are skipped, so
    the session resumes exactly where it left off.
  - the corpus contains exact duplicate posts (the same text posted more
    than once). A decision on one is auto propagated to every other
    unlabeled post whose text is byte identical, and those rows are marked
    propagated: true, so the human is never asked to relabel the same text.
  - the undo key rewrites the output file without the previous decision (and
    any posts it propagated to) and puts that post back at the front of the
    queue, so a mistaken keystroke is recoverable.
"""
from __future__ import annotations

import argparse
import json
import random
import textwrap
from datetime import datetime, timezone
from pathlib import Path

WRAP_WIDTH = 90

KEY_TO_CATEGORY = {
    "s": "specific_equity",
    "i": "index_or_etf",
    "m": "macro_market",
    "n": "not_financial",
}

from tsalert.detect.policy import CATEGORY_IS_STOCK_RELATED  # noqa: E402

KEY_LEGEND = """keys:
  y  accept the proposal as is (non-blind only)
  s  specific_equity, then enter tickers comma separated
  i  index_or_etf
  m  macro_market
  n  not_financial
  e  edit tickers on the current decision
  u  undo the previous post and relabel it
  q  save and quit"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Human review CLI for the eval sample.")
    parser.add_argument("--sample", default="data/eval/eval_sample.jsonl", help="input JSONL sample to label")
    parser.add_argument("--prelabels", default="data/eval/prelabels.jsonl", help="LLM proposals, JSONL, optional")
    parser.add_argument("--out", default="data/eval/labeled.jsonl", help="output JSONL of final labels")
    parser.add_argument("--blind-count", type=int, default=30, help="how many posts form the blind holdout")
    parser.add_argument("--seed", type=int, default=42, help="random seed for the blind selection")
    return parser.parse_args(argv)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_prelabels(path: Path) -> dict[str, dict]:
    return {row["post_id"]: row for row in load_jsonl(path)}


def append_and_flush(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()


def rewrite_all(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
        f.flush()


def _allocate_proportional(sizes: dict[str, int], target: int) -> dict[str, int]:
    """Split `target` items across strata proportionally to `sizes`, using
    largest remainder rounding so the allocation always sums to exactly
    `target` (never less, never more, and never above a stratum's own size).
    Ties in the remainder go to the stratum with the alphabetically earlier
    name, so the result is fully deterministic for a fixed input.
    """
    total_pop = sum(sizes.values())
    alloc = {k: 0 for k in sizes}
    if total_pop == 0 or target <= 0:
        return alloc

    exact = {k: target * v / total_pop for k, v in sizes.items()}
    alloc = {k: min(int(exact[k]), sizes[k]) for k in sizes}
    remainder = target - sum(alloc.values())

    order = sorted(sizes, key=lambda k: (-(exact[k] - int(exact[k])), k))
    n = len(order)
    idx = 0
    guard = 0
    while remainder > 0 and n and guard < n * (target + 1):
        k = order[idx % n]
        if alloc[k] < sizes[k]:
            alloc[k] += 1
            remainder -= 1
        idx += 1
        guard += 1
    return alloc


def select_blind_post_ids(rows: list[dict], blind_count: int, seed: int) -> set[str]:
    """Deterministically choose blind_count post_ids from rows, allocated
    proportionally across strata so no stratum is over or under represented
    in the blind holdout. Same rows, blind_count, and seed always produce
    the same set.
    """
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row["stratum"], []).append(row["post_id"])

    total = len(rows)
    if total == 0 or blind_count <= 0:
        return set()
    target = min(blind_count, total)

    sizes = {stratum: len(ids) for stratum, ids in groups.items()}
    counts = _allocate_proportional(sizes, target)

    rng = random.Random(seed)
    blind_ids: set[str] = set()
    for stratum in sorted(groups):
        k = counts.get(stratum, 0)
        if k:
            blind_ids.update(rng.sample(groups[stratum], k=k))
    return blind_ids


def build_queue(rows: list[dict], blind_ids: set[str], labeled_ids: set[str]) -> list[str]:
    """Order the posts still needing review: every blind post first (in
    original sample order), then everything else (also in original sample
    order). Posts already present in labeled_ids, whether from a prior run
    or from auto propagation earlier in this run, are skipped entirely.
    """
    blind_first = [r["post_id"] for r in rows if r["post_id"] in blind_ids and r["post_id"] not in labeled_ids]
    rest = [r["post_id"] for r in rows if r["post_id"] not in blind_ids and r["post_id"] not in labeled_ids]
    return blind_first + rest


def find_propagation_targets(rows_by_id: dict[str, dict], post_id: str, labeled_ids: set[str]) -> list[str]:
    """Other post_ids whose text is byte identical to post_id's text and
    that are not already labeled. Order follows rows_by_id's iteration
    order, which is the original sample order.
    """
    text = rows_by_id[post_id]["text"]
    return [
        pid
        for pid, row in rows_by_id.items()
        if pid != post_id and pid not in labeled_ids and row["text"] == text
    ]


def compute_changed_from_prelabel(prelabel: dict | None, is_stock_related: bool, category: str, tickers: list[str]) -> bool:
    """True when the human's final decision differs from the LLM's proposal
    on is_stock_related, category, or the ticker set (case insensitive,
    order insensitive). No prelabel at all counts as changed, since there
    was nothing for the human to agree with.
    """
    if prelabel is None:
        return True
    if bool(prelabel.get("is_stock_related")) != bool(is_stock_related):
        return True
    if prelabel.get("category") != category:
        return True
    prelabel_tickers = {t.upper() for t in (prelabel.get("tickers") or [])}
    final_tickers = {t.upper() for t in (tickers or [])}
    if prelabel_tickers != final_tickers:
        return True
    return False


def build_output_row(
    sample_row: dict,
    is_stock_related: bool,
    category: str,
    tickers: list[str],
    companies: list[str],
    blind: bool,
    propagated: bool,
    changed_from_prelabel: bool,
    labeled_at: str,
) -> dict:
    return {
        "post_id": sample_row["post_id"],
        "text": sample_row["text"],
        "stratum": sample_row["stratum"],
        "weight": sample_row["weight"],
        "in_weighted_metrics": sample_row["in_weighted_metrics"],
        "is_stock_related": is_stock_related,
        "category": category,
        "tickers": tickers,
        "companies": companies,
        "blind": blind,
        "propagated": propagated,
        "changed_from_prelabel": changed_from_prelabel,
        "labeled_at": labeled_at,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def print_post(position: int, total: int, row: dict, blind: bool, prelabel: dict | None) -> None:
    marker = "  BLIND" if blind else ""
    print(f"[{position}/{total}] stratum: {row['stratum']}{marker}")
    print(textwrap.fill(row["text"], width=WRAP_WIDTH))
    if not blind:
        if prelabel is not None:
            tickers = ", ".join(prelabel.get("tickers") or []) or "none"
            print(f"proposed: {prelabel.get('category')}  tickers: {tickers}")
            print(f"reasoning: {prelabel.get('reasoning', '')}")
        else:
            print("proposed: no prelabel available")
    print(KEY_LEGEND)


def prompt_tickers(default: list[str] | None = None) -> list[str]:
    raw = input("tickers (comma separated): ").strip()
    if not raw:
        return list(default) if default else []
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def print_final_summary(rows: list[dict]) -> None:
    total = len(rows)
    blind_rows = [r for r in rows if r.get("blind")]
    changed = sum(1 for r in rows if r.get("changed_from_prelabel"))
    blind_changed = sum(1 for r in blind_rows if r.get("changed_from_prelabel"))
    overall_rate = changed / total if total else 0.0
    blind_rate = blind_changed / len(blind_rows) if blind_rows else 0.0
    print(f"labeled: {total}")
    print(f"blind: {len(blind_rows)}")
    print(f"correction rate overall: {overall_rate:.3f}")
    print(f"correction rate blind: {blind_rate:.3f}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sample_path = Path(args.sample)
    prelabels_path = Path(args.prelabels)
    out_path = Path(args.out)

    rows = load_jsonl(sample_path)
    if not rows:
        print(f"no rows found in {sample_path}")
        return 1
    rows_by_id = {row["post_id"]: row for row in rows}
    prelabels = load_prelabels(prelabels_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing_rows = load_jsonl(out_path)
    labeled_ids = {row["post_id"] for row in existing_rows}

    total = len(rows)
    blind_ids = select_blind_post_ids(rows, args.blind_count, args.seed)
    queue = build_queue(rows, blind_ids, labeled_ids)

    if labeled_ids:
        print(f"resuming: {len(labeled_ids)} of {total} already labeled")
    else:
        print(f"{total} posts to label, {len(blind_ids)} in the blind holdout")

    session_log: list[list[str]] = []

    while queue:
        post_id = queue[0]
        row = rows_by_id[post_id]
        blind = post_id in blind_ids
        prelabel = prelabels.get(post_id)
        position = len(labeled_ids) + 1
        print_post(position, total, row, blind, prelabel)

        try:
            action = input("> ").strip().lower()
        except EOFError:
            action = "q"

        if action == "q":
            break

        if action == "u":
            if not session_log:
                print("nothing to undo yet")
                continue
            undo_ids = session_log.pop()
            existing_rows = [r for r in existing_rows if r["post_id"] not in undo_ids]
            for pid in undo_ids:
                labeled_ids.discard(pid)
            rewrite_all(out_path, existing_rows)
            queue.insert(0, undo_ids[0])
            print(f"undone: {undo_ids[0]}")
            continue

        if blind and action == "y":
            print("no proposal is shown for a blind post, choose s/i/m/n")
            continue

        if action == "y":
            if prelabel is None:
                print("no prelabel available, choose s/i/m/n")
                continue
            category = prelabel.get("category")
            is_stock_related = CATEGORY_IS_STOCK_RELATED.get(category, bool(prelabel.get("is_stock_related")))
            tickers = [t.upper() for t in (prelabel.get("tickers") or [])]
            companies = list(prelabel.get("companies") or [])
        elif action in KEY_TO_CATEGORY:
            category = KEY_TO_CATEGORY[action]
            is_stock_related = CATEGORY_IS_STOCK_RELATED[category]
            companies = list(prelabel.get("companies") or []) if prelabel else []
            tickers = prompt_tickers() if category == "specific_equity" else []
        elif action == "e":
            if prelabel is None:
                print("no prelabel to edit, choose s/i/m/n")
                continue
            category = prelabel.get("category")
            is_stock_related = CATEGORY_IS_STOCK_RELATED.get(category, bool(prelabel.get("is_stock_related")))
            tickers = prompt_tickers(default=[t.upper() for t in (prelabel.get("tickers") or [])])
            companies = list(prelabel.get("companies") or [])
        else:
            print("unrecognized key")
            continue

        changed = compute_changed_from_prelabel(prelabel, is_stock_related, category, tickers)
        out_row = build_output_row(row, is_stock_related, category, tickers, companies, blind, False, changed, now_iso())
        append_and_flush(out_path, out_row)
        existing_rows.append(out_row)
        labeled_ids.add(post_id)
        queue.pop(0)
        written_ids = [post_id]

        targets = find_propagation_targets(rows_by_id, post_id, labeled_ids)
        for target_id in targets:
            target_row = rows_by_id[target_id]
            target_blind = target_id in blind_ids
            target_prelabel = prelabels.get(target_id)
            target_changed = compute_changed_from_prelabel(target_prelabel, is_stock_related, category, tickers)
            target_out_row = build_output_row(
                target_row, is_stock_related, category, tickers, companies, target_blind, True, target_changed, now_iso()
            )
            append_and_flush(out_path, target_out_row)
            existing_rows.append(target_out_row)
            labeled_ids.add(target_id)
            written_ids.append(target_id)
            if target_id in queue:
                queue.remove(target_id)

        if targets:
            print(f"propagated to {len(targets)} identical post(s)")

        session_log.append(written_ids)

    print_final_summary(existing_rows)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Every decision was already appended and flushed, so nothing is lost.
        # Exit quietly rather than dumping a traceback over the session summary.
        print("\ninterrupted. progress is saved, rerun the same command to resume.")
        raise SystemExit(130)
