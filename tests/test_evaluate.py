from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import evaluate

# The real, curated lexicon. Only $CASHTAG text is used in these handcrafted
# posts, which RuleDetector always turns into a candidate at a confidence
# above its default threshold regardless of a ticker's ambiguity tier or any
# concurrent change to the lexicon's contents, so these tests do not depend
# on what the lexicon currently contains.
_LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "lexicon" / "tickers.csv"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _sample_row(post_id, text, stratum, weight, in_weighted_metrics) -> dict:
    return {
        "post_id": post_id,
        "text": text,
        "stratum": stratum,
        "stratum_population": 1,
        "stratum_sample_size": 1,
        "weight": weight,
        "in_weighted_metrics": in_weighted_metrics,
    }


def _build_handcrafted_set(tmp_path: Path):
    # p1, p2: stratum candidate, weight 1.0 each.
    # p3, p4: stratum random, weight 3.0 each (a stratum sampled at 1-in-3).
    # p5: stratum hard_negative, weight 0.0, must never enter headline math.
    sample_rows = [
        _sample_row("p1", "$TSLA up big today", "candidate", 1.0, True),
        _sample_row("p2", "$AAPL earnings beat", "candidate", 1.0, True),
        _sample_row("p3", "I like going for long walks in the park", "random", 3.0, True),
        _sample_row(
            "p4", "The car company that Elon Musk runs had a big day", "random", 3.0, True
        ),
        _sample_row("p5", "$ALLX is the ticker some people mention", "hard_negative", 0.0, False),
    ]

    labeled_rows = [
        {"post_id": "p1", "is_stock_related": True, "tickers": ["TSLA"], "blind": True},
        {"post_id": "p2", "is_stock_related": False, "tickers": [], "blind": False},
        {"post_id": "p3", "is_stock_related": False, "tickers": [], "blind": True},
        {"post_id": "p4", "is_stock_related": True, "tickers": ["TSLA"], "blind": False},
        {"post_id": "p5", "is_stock_related": False, "tickers": [], "blind": True},
    ]

    # The LLM arm is deliberately made "perfect" on this handcrafted set so
    # its weighted metrics come out to exactly 1.0 and are easy to check by
    # hand, while the rule arm (computed live, not from this file) gets p2
    # wrong (fires on the AAPL cashtag though the human said not related)
    # and misses p4 (no cashtag or alias appears in that sentence at all).
    prelabel_rows = [
        {"post_id": "p1", "is_stock_related": True, "tickers": ["TSLA"], "companies": ["Tesla"]},
        {"post_id": "p2", "is_stock_related": False, "tickers": [], "companies": []},
        {"post_id": "p3", "is_stock_related": False, "tickers": [], "companies": []},
        {"post_id": "p4", "is_stock_related": True, "tickers": ["TSLA"], "companies": ["Tesla"]},
        {"post_id": "p5", "is_stock_related": False, "tickers": [], "companies": []},
    ]

    sample_path = tmp_path / "eval_sample.jsonl"
    labeled_path = tmp_path / "labeled.jsonl"
    prelabels_path = tmp_path / "prelabels.jsonl"
    _write_jsonl(sample_path, sample_rows)
    _write_jsonl(labeled_path, labeled_rows)
    _write_jsonl(prelabels_path, prelabel_rows)
    return sample_path, labeled_path, prelabels_path


def test_weighted_metrics_computed_correctly_on_handcrafted_set(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    rules_weighted = result["headline"]["rules"]["weighted"]
    # p1 TP w=1, p2 FP w=1, p3 TN w=3, p4 FN w=3.
    assert rules_weighted["tp"] == 1.0
    assert rules_weighted["fp"] == 1.0
    assert rules_weighted["fn"] == 3.0
    assert rules_weighted["tn"] == 3.0
    assert rules_weighted["precision"] == 0.5
    assert rules_weighted["recall"] == 0.25
    assert abs(rules_weighted["f1"] - (2 * 0.5 * 0.25 / (0.5 + 0.25))) < 1e-9

    rules_unweighted = result["headline"]["rules"]["unweighted"]
    assert rules_unweighted["tp"] == 1
    assert rules_unweighted["fp"] == 1
    assert rules_unweighted["fn"] == 1
    assert rules_unweighted["tn"] == 1
    assert rules_unweighted["precision"] == 0.5
    assert rules_unweighted["recall"] == 0.5

    llm_weighted = result["headline"]["llm"]["weighted"]
    assert llm_weighted["tp"] == 4.0  # p1 (w1) + p4 (w3)
    assert llm_weighted["fp"] == 0.0
    assert llm_weighted["fn"] == 0.0
    assert llm_weighted["precision"] == 1.0
    assert llm_weighted["recall"] == 1.0
    assert llm_weighted["f1"] == 1.0


def test_hard_negative_rows_excluded_from_weighted_numbers(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    # Only p1..p4 (candidate/random) count toward headline weight: 1+1+3+3=8.
    rules_weighted = result["headline"]["rules"]["weighted"]
    total_weight = rules_weighted["tp"] + rules_weighted["fp"] + rules_weighted["fn"] + rules_weighted["tn"]
    assert total_weight == 8.0
    assert result["headline_n"] == 4

    # p5 (hard_negative) is where the rule arm fires on an unknown-ticker
    # cashtag despite the human confirming it is not stock related: a real
    # false fire, but it must never leak into the headline numbers above.
    suppression = result["suppression"]["rules"]
    assert suppression["n_hard_negative_labeled"] == 1
    assert suppression["n_confirmed_negative"] == 1
    assert suppression["n_correctly_quiet"] == 0
    assert len(suppression["false_fires"]) == 1
    assert suppression["false_fires"][0]["post_id"] == "p5"

    llm_suppression = result["suppression"]["llm"]
    assert llm_suppression["n_correctly_quiet"] == 1
    assert llm_suppression["false_fires"] == []


def test_missing_labeled_file_exits_zero_with_clear_message(tmp_path, capsys):
    sample_path, _labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)
    missing_labeled_path = tmp_path / "does_not_exist.jsonl"

    exit_code = evaluate.main(
        [
            "--labeled",
            str(missing_labeled_path),
            "--sample",
            str(sample_path),
            "--prelabels",
            str(prelabels_path),
            "--lexicon",
            str(_LEXICON_PATH),
            "--no-write-report",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "labeled rows found: 0" in out
    assert "does not exist yet" in out


def test_partial_labeled_file_reports_the_real_count(tmp_path):
    sample_path, _full_labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)

    # Only 2 of the 5 sample rows have been labeled so far, matching the
    # "labeling is still in progress" scenario the script must handle.
    partial_labeled_path = tmp_path / "partial_labeled.jsonl"
    _write_jsonl(
        partial_labeled_path,
        [
            {"post_id": "p1", "is_stock_related": True, "tickers": ["TSLA"], "blind": True},
            {"post_id": "p2", "is_stock_related": False, "tickers": [], "blind": False},
        ],
    )

    result = evaluate.evaluate(partial_labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    assert result["labeled_count"] == 2
    assert result["matched_count"] == 2
    assert result["missing_labels_count"] == 3
    assert "labeled rows found: 2" in result["report_text"]
    assert "sample rows with no label yet: 3 of 5" in result["report_text"]


def test_evaluate_never_crashes_and_exits_zero_when_sample_missing(tmp_path, capsys):
    missing_sample = tmp_path / "no_sample.jsonl"
    missing_labeled = tmp_path / "no_labeled.jsonl"
    missing_prelabels = tmp_path / "no_prelabels.jsonl"

    exit_code = evaluate.main(
        [
            "--labeled",
            str(missing_labeled),
            "--sample",
            str(missing_sample),
            "--prelabels",
            str(missing_prelabels),
            "--lexicon",
            str(_LEXICON_PATH),
            "--no-write-report",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Cannot evaluate" in out
