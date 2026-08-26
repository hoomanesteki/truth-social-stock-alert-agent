#!/usr/bin/env python3
"""Compute precision, recall and F1 for the rule and LLM detector arms.

    uv run python scripts/evaluate.py --labeled data/eval/labeled.jsonl \
        --sample data/eval/eval_sample.jsonl --prelabels data/eval/prelabels.jsonl

Human labeling of data/eval/labeled.jsonl happens outside this script and can
still be in progress when this runs. That file may not exist yet, or may
cover only some of eval_sample.jsonl's rows. This script never crashes on
that: it states plainly how many labeled rows it found, computes every
number it honestly can from them, and exits 0 either way. It never pads a
missing row with a guess and never projects a partial label set onto rows
nobody has looked at yet.

Expected schema of labeled.jsonl, one JSON object per line (this schema is
this script's own contract, since the labeling step is still in progress
and has not fixed one):
  post_id           str, matches a row in eval_sample.jsonl
  is_stock_related  bool, the human ground truth verdict
  tickers           list[str], optional, the human's ticker list
  blind             bool, optional. True if the labeler judged the post
                    without seeing the LLM prelabel first, False if they saw
                    it before judging. Absent means unknown, and the row is
                    left out of the blind-subset comparison rather than
                    guessed at.

The rule arm is computed live for every labeled row by calling RuleDetector
straight over the post text pulled from --sample, since no rule-arm
predictions are stored on disk anywhere. The LLM arm is read from
--prelabels rather than computed live, since prelabel.py already produced it
independently of the rule detector (see that script's own docstring for why
that independence matters).

Only rows where in_weighted_metrics is true (strata "candidate" and
"random") feed the headline weighted precision/recall/F1. Stratum
"hard_negative" is a purposive, hand picked sample of suspected false
positive traps, not a probability sample, so projecting it back onto the
population would invent a precision number that is not there. Reported separately as a suppression
stress test and folded into error analysis instead.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tsalert.detect.lexicon import Lexicon
from tsalert.detect.rules import RuleDetector

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LEXICON_PATH = _REPO_ROOT / "data" / "lexicon" / "tickers.csv"

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 42
TEXT_TRUNCATE = 100
ARMS = ("rules", "llm")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the rule and LLM detector arms against human labels."
    )
    parser.add_argument("--labeled", default="data/eval/labeled.jsonl", help="human labels, JSONL")
    parser.add_argument(
        "--sample", default="data/eval/eval_sample.jsonl", help="stratified eval sample, JSONL"
    )
    parser.add_argument(
        "--prelabels", default="data/eval/prelabels.jsonl", help="LLM prelabels, JSONL"
    )
    parser.add_argument(
        "--lexicon", default=str(_DEFAULT_LEXICON_PATH), help="ticker lexicon CSV for the rule arm"
    )
    parser.add_argument("--report", default="data/eval/metrics.md", help="where to write the report")
    parser.add_argument(
        "--no-write-report", action="store_true", help="print only, do not write --report to disk"
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Loading and joining
# ---------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file. A missing file is not an error here, just empty."""
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A corrupt line is skipped rather than aborting the whole
                # run: labeling in progress means this file can be hand
                # edited and mid-write.
                continue
    return rows


def index_by_post_id(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        post_id = row.get("post_id")
        if post_id is not None:
            out[str(post_id)] = row
    return out


def _as_ticker_set(values) -> set[str]:
    if not values:
        return set()
    return {str(v).strip().upper() for v in values if str(v).strip()}


def build_joined_rows(
    labeled_rows: list[dict],
    sample_by_id: dict[str, dict],
    prelabels_by_id: dict[str, dict],
    rules: RuleDetector,
) -> tuple[list[dict], list[str]]:
    """Join labeled rows against the sample (for text/stratum/weight) and
    the prelabels (for the LLM arm). Runs the rule arm live.

    Returns (joined_rows, labeled_ids_missing_from_sample). A labeled post_id
    that is not in eval_sample.jsonl cannot be scored (no text, no stratum,
    no weight) and is reported rather than silently dropped.
    """
    joined = []
    missing_from_sample = []

    for row in labeled_rows:
        post_id = row.get("post_id")
        if post_id is None:
            continue
        post_id = str(post_id)
        sample_row = sample_by_id.get(post_id)
        if sample_row is None:
            missing_from_sample.append(post_id)
            continue

        text = sample_row.get("text", "")
        detection = rules.detect(text, post_id)

        prelabel_row = prelabels_by_id.get(post_id)
        if prelabel_row is not None:
            llm_positive = bool(prelabel_row.get("is_stock_related"))
            llm_tickers = _as_ticker_set(prelabel_row.get("tickers"))
        else:
            llm_positive = None
            llm_tickers = None

        blind = row.get("blind")
        if not isinstance(blind, bool):
            blind = None

        joined.append(
            {
                "post_id": post_id,
                "text": text,
                "stratum": sample_row.get("stratum"),
                "weight": float(sample_row.get("weight", 0.0)),
                "in_weighted_metrics": bool(sample_row.get("in_weighted_metrics", False)),
                "blind": blind,
                "human_positive": bool(row.get("is_stock_related")),
                "human_tickers": _as_ticker_set(row.get("tickers")),
                "rules_positive": detection.is_stock_related,
                "rules_tickers": {m.ticker for m in detection.mentions},
                "llm_positive": llm_positive,
                "llm_tickers": llm_tickers,
            }
        )

    return joined, missing_from_sample


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_pr_f1(triples: list[tuple[float, bool, bool]], weighted: bool) -> dict:
    """triples is a list of (weight, predicted, actual)."""
    tp = fp = fn = tn = 0.0
    for weight, predicted, actual in triples:
        w = weight if weighted else 1.0
        if predicted and actual:
            tp += w
        elif predicted and not actual:
            fp += w
        elif not predicted and actual:
            fn += w
        else:
            tn += w

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return {
        "n": len(triples),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _percentile_ci(values: list[float]) -> tuple[float, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    lo_idx = int(0.025 * n)
    hi_idx = min(n - 1, int(0.975 * n))
    return (ordered[lo_idx], ordered[hi_idx])


def bootstrap_ci(
    triples: list[tuple[float, bool, bool]],
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict:
    """Percentile bootstrap over weighted precision/recall/F1.

    Standard library random only, per project constraints. A fresh
    Random(seed) is used every call so results are reproducible in
    isolation, independent of how many other bootstraps ran before it.
    """
    n = len(triples)
    if n == 0:
        return {"precision": None, "recall": None, "f1": None, "n_resamples_with_value": 0}

    rng = random.Random(seed)
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []

    for _ in range(resamples):
        resample = [triples[rng.randrange(n)] for _ in range(n)]
        m = compute_pr_f1(resample, weighted=True)
        if m["precision"] is not None:
            precisions.append(m["precision"])
        if m["recall"] is not None:
            recalls.append(m["recall"])
        if m["f1"] is not None:
            f1s.append(m["f1"])

    return {
        "precision": _percentile_ci(precisions),
        "recall": _percentile_ci(recalls),
        "f1": _percentile_ci(f1s),
        "n_resamples_with_value": len(f1s),
    }


def _triples_for_arm(rows: list[dict], arm: str) -> list[tuple[float, bool, bool]]:
    pred_key = f"{arm}_positive"
    return [
        (row["weight"], row[pred_key], row["human_positive"])
        for row in rows
        if row.get(pred_key) is not None
    ]


def arm_headline_metrics(headline_rows: list[dict], arm: str) -> dict:
    triples = _triples_for_arm(headline_rows, arm)
    skipped = sum(1 for row in headline_rows if row.get(f"{arm}_positive") is None)
    return {
        "n_scored": len(triples),
        "n_skipped_no_prediction": skipped,
        "weighted": compute_pr_f1(triples, weighted=True),
        "unweighted": compute_pr_f1(triples, weighted=False),
        "bootstrap_ci": bootstrap_ci(triples),
    }


def suppression_report(all_rows: list[dict], arm: str) -> dict:
    """On the hard_negative trap stratum: of the traps confirmed by a human
    to actually be negative, how many did the arm correctly stay quiet on.
    """
    trap_rows = [r for r in all_rows if r.get("stratum") == "hard_negative"]
    confirmed_negative = [r for r in trap_rows if r["human_positive"] is False]
    pred_key = f"{arm}_positive"
    scored = [r for r in confirmed_negative if r.get(pred_key) is not None]
    correctly_quiet = sum(1 for r in scored if r[pred_key] is False)
    false_fires = [r for r in scored if r[pred_key] is True]
    return {
        "n_hard_negative_labeled": len(trap_rows),
        "n_confirmed_negative": len(confirmed_negative),
        "n_scored": len(scored),
        "n_correctly_quiet": correctly_quiet,
        "false_fires": false_fires,
    }


def blind_subset_report(headline_rows: list[dict]) -> dict:
    """LLM arm metrics split by whether the human labeler saw the LLM's own
    prelabel before judging (blind False) or judged independently first
    (blind True). Measures how much the LLM arm's reported numbers are
    inflated by being graded against labels it helped produce.
    """
    blind_true = [r for r in headline_rows if r.get("blind") is True]
    blind_false = [r for r in headline_rows if r.get("blind") is False]

    if not blind_true and not blind_false:
        return {"available": False}

    true_metrics = compute_pr_f1(_triples_for_arm(blind_true, "llm"), weighted=True)
    false_metrics = compute_pr_f1(_triples_for_arm(blind_false, "llm"), weighted=True)

    gap_f1 = None
    if true_metrics["f1"] is not None and false_metrics["f1"] is not None:
        gap_f1 = false_metrics["f1"] - true_metrics["f1"]

    return {
        "available": True,
        "blind_true": true_metrics,
        "blind_false": false_metrics,
        "gap_f1": gap_f1,
    }


def _truncate(text: str) -> str:
    text = text.replace("\n", " ")
    if len(text) <= TEXT_TRUNCATE:
        return text
    return text[:TEXT_TRUNCATE] + "..."


def error_analysis(all_rows: list[dict], arm: str) -> dict:
    pred_key = f"{arm}_positive"
    scored = [r for r in all_rows if r.get(pred_key) is not None]

    false_positives = [
        {"post_id": r["post_id"], "stratum": r["stratum"], "text": _truncate(r["text"])}
        for r in scored
        if r[pred_key] is True and r["human_positive"] is False
    ]
    false_negatives = [
        {"post_id": r["post_id"], "stratum": r["stratum"], "text": _truncate(r["text"])}
        for r in scored
        if r[pred_key] is False and r["human_positive"] is True
    ]
    return {"false_positives": false_positives, "false_negatives": false_negatives}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def evaluate(
    labeled_path: Path,
    sample_path: Path,
    prelabels_path: Path,
    lexicon_path: Path,
) -> dict:
    labeled_rows = read_jsonl(labeled_path)
    sample_rows = read_jsonl(sample_path)
    prelabel_rows = read_jsonl(prelabels_path)

    sample_by_id = index_by_post_id(sample_rows)
    prelabels_by_id = index_by_post_id(prelabel_rows)

    result: dict = {
        "labeled_path": str(labeled_path),
        "labeled_exists": labeled_path.exists(),
        "labeled_count": len(labeled_rows),
        "sample_count": len(sample_rows),
        "prelabels_count": len(prelabel_rows),
    }

    if not sample_rows:
        result["fatal"] = f"no rows in {sample_path}, nothing to evaluate against"
        result["report_text"] = _format_report(result)
        return result

    rules = RuleDetector(Lexicon.load(lexicon_path))
    joined, missing_from_sample = build_joined_rows(
        labeled_rows, sample_by_id, prelabels_by_id, rules
    )
    result["matched_count"] = len(joined)
    result["unmatched_labeled_ids"] = missing_from_sample
    result["missing_labels_count"] = len(sample_rows) - len(
        {r["post_id"] for r in joined}
    )

    headline_rows = [r for r in joined if r["in_weighted_metrics"]]
    result["headline_n"] = len(headline_rows)

    if not joined:
        result["report_text"] = _format_report(result)
        return result

    result["headline"] = {arm: arm_headline_metrics(headline_rows, arm) for arm in ARMS}
    result["suppression"] = {arm: suppression_report(joined, arm) for arm in ARMS}
    result["blind"] = blind_subset_report(headline_rows)
    result["errors"] = {arm: error_analysis(joined, arm) for arm in ARMS}

    result["report_text"] = _format_report(result)
    return result


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _fmt(x, digits: int = 3) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{digits}f}"


def _fmt_ci(ci) -> str:
    if ci is None:
        return "n/a"
    return f"[{_fmt(ci[0])}, {_fmt(ci[1])}]"


def _format_arm_block(arm: str, metrics: dict) -> list[str]:
    lines = [f"### {arm} arm"]
    weighted = metrics["weighted"]
    unweighted = metrics["unweighted"]
    ci = metrics["bootstrap_ci"]
    lines.append(
        f"  weighted:   precision={_fmt(weighted['precision'])} "
        f"recall={_fmt(weighted['recall'])} f1={_fmt(weighted['f1'])} "
        f"(tp={_fmt(weighted['tp'],1)} fp={_fmt(weighted['fp'],1)} fn={_fmt(weighted['fn'],1)})"
    )
    lines.append(
        f"  unweighted (on-sample, n={unweighted['n']}): "
        f"precision={_fmt(unweighted['precision'])} recall={_fmt(unweighted['recall'])} "
        f"f1={_fmt(unweighted['f1'])} (tp={int(unweighted['tp'])} fp={int(unweighted['fp'])} "
        f"fn={int(unweighted['fn'])} tn={int(unweighted['tn'])})"
    )
    lines.append(
        f"  bootstrap 95% CI ({BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}): "
        f"precision={_fmt_ci(ci['precision'])} recall={_fmt_ci(ci['recall'])} f1={_fmt_ci(ci['f1'])}"
    )
    if metrics["n_skipped_no_prediction"]:
        lines.append(
            f"  ({metrics['n_skipped_no_prediction']} headline rows skipped for this arm: "
            "no prediction available, e.g. missing from prelabels.jsonl)"
        )
    return lines


def _format_suppression_block(arm: str, report: dict) -> list[str]:
    lines = [f"### {arm} arm"]
    lines.append(f"  hard_negative rows labeled: {report['n_hard_negative_labeled']}")
    lines.append(f"  confirmed actually negative (true traps): {report['n_confirmed_negative']}")
    if report["n_scored"] == 0:
        lines.append("  no scored traps yet for this arm")
        return lines
    lines.append(
        f"  correctly stayed quiet: {report['n_correctly_quiet']}/{report['n_scored']}"
    )
    if report["false_fires"]:
        lines.append("  false fires on traps:")
        for r in report["false_fires"]:
            lines.append(f"    [{r['post_id']}] {r['text']}")
    return lines


def _format_error_block(arm: str, errors: dict) -> list[str]:
    lines = [f"### {arm} arm"]
    fps = errors["false_positives"]
    fns = errors["false_negatives"]
    lines.append(f"  false positives ({len(fps)}):")
    if not fps:
        lines.append("    none")
    else:
        by_stratum: dict[str, list[dict]] = {}
        for r in fps:
            by_stratum.setdefault(r["stratum"], []).append(r)
        for stratum, rows in sorted(by_stratum.items()):
            lines.append(f"    stratum={stratum} ({len(rows)}):")
            for r in rows:
                lines.append(f"      [{r['post_id']}] {r['text']}")
    lines.append(f"  false negatives ({len(fns)}):")
    if not fns:
        lines.append("    none")
    else:
        by_stratum = {}
        for r in fns:
            by_stratum.setdefault(r["stratum"], []).append(r)
        for stratum, rows in sorted(by_stratum.items()):
            lines.append(f"    stratum={stratum} ({len(rows)}):")
            for r in rows:
                lines.append(f"      [{r['post_id']}] {r['text']}")
    return lines


def _format_report(result: dict) -> str:
    lines = []
    lines.append("# Detection evaluation")
    lines.append("")
    lines.append(f"labeled file: {result['labeled_path']}")
    if not result["labeled_exists"]:
        lines.append("labeled file does not exist yet: labeling is still in progress.")
    lines.append(f"labeled rows found: {result['labeled_count']}")
    lines.append(f"eval sample rows: {result['sample_count']}")

    if "fatal" in result:
        lines.append("")
        lines.append(f"Cannot evaluate: {result['fatal']}")
        return "\n".join(lines) + "\n"

    lines.append(f"labeled rows matched to a sample row: {result['matched_count']}")
    if result["unmatched_labeled_ids"]:
        lines.append(
            f"labeled post_ids NOT found in the eval sample (skipped): "
            f"{len(result['unmatched_labeled_ids'])}"
        )
    lines.append(
        f"sample rows with no label yet: {result['missing_labels_count']} of {result['sample_count']}"
    )
    lines.append(f"labeled rows counted toward headline (weighted) metrics: {result['headline_n']}")

    if result["matched_count"] == 0:
        lines.append("")
        lines.append("No labeled rows matched the eval sample yet. Nothing further to report.")
        return "\n".join(lines) + "\n"

    if result["headline_n"] == 0:
        lines.append("")
        lines.append(
            "No labeled rows fall in the weighted-metrics strata (candidate/random) yet. "
            "Headline precision/recall/F1 cannot be computed. "
            "Suppression and error analysis below still use whatever hard_negative rows are labeled."
        )

    if result["headline_n"] > 0:
        lines.append("")
        lines.append("## Headline metrics (candidate + random strata only, hard_negative excluded)")
        for arm in ARMS:
            lines.extend(_format_arm_block(arm, result["headline"][arm]))

    lines.append("")
    lines.append("## Suppression report (hard_negative trap stratum)")
    for arm in ARMS:
        lines.extend(_format_suppression_block(arm, result["suppression"][arm]))

    lines.append("")
    lines.append("## Blind subset comparison (LLM arm only)")
    blind = result.get("blind", {"available": False})
    if not blind["available"]:
        lines.append(
            "  no labeled row carries a 'blind' field yet, cannot compare blind vs non-blind labeling."
        )
    else:
        bt, bf = blind["blind_true"], blind["blind_false"]
        lines.append(f"  blind=true  (n={bt['n']}): precision={_fmt(bt['precision'])} recall={_fmt(bt['recall'])} f1={_fmt(bt['f1'])}")
        lines.append(f"  blind=false (n={bf['n']}): precision={_fmt(bf['precision'])} recall={_fmt(bf['recall'])} f1={_fmt(bf['f1'])}")
        lines.append(f"  gap (blind=false f1 minus blind=true f1): {_fmt(blind['gap_f1'])}")

    lines.append("")
    lines.append("## Error analysis")
    for arm in ARMS:
        lines.extend(_format_error_block(arm, result["errors"][arm]))

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    result = evaluate(
        Path(args.labeled), Path(args.sample), Path(args.prelabels), Path(args.lexicon)
    )
    report_text = result["report_text"]
    print(report_text)

    if not args.no_write_report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
