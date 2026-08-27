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

# Stand-in for the model id that prelabel.py stamps on every row it writes.
# The report has to name it, since the arm being scored is only as meaningful
# as the model that produced the predictions.
_PRELABEL_MODEL = "test/prelabel-model-a"


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
    # That perfection is also what the circularity check must flag: it is the
    # same shape as the real eval set, where labeled.jsonl was derived from
    # these predictions and the arm ends up graded against its own output.
    prelabel_rows = [
        {"post_id": "p1", "is_stock_related": True, "tickers": ["TSLA"], "companies": ["Tesla"], "model": _PRELABEL_MODEL},
        {"post_id": "p2", "is_stock_related": False, "tickers": [], "companies": [], "model": _PRELABEL_MODEL},
        {"post_id": "p3", "is_stock_related": False, "tickers": [], "companies": [], "model": _PRELABEL_MODEL},
        {"post_id": "p4", "is_stock_related": True, "tickers": ["TSLA"], "companies": ["Tesla"], "model": _PRELABEL_MODEL},
        {"post_id": "p5", "is_stock_related": False, "tickers": [], "companies": [], "model": _PRELABEL_MODEL},
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


def _rewrite_prelabels(prelabels_path: Path, changes: dict[str, dict | None]) -> None:
    """Rewrite prelabels.jsonl, patching or dropping rows by post_id.

    A value of None drops that row entirely, which is how "the LLM has no
    prediction for this post" is expressed on disk.
    """
    rows = []
    for line in prelabels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["post_id"] in changes:
            patch = changes[row["post_id"]]
            if patch is None:
                continue
            row = {**row, **patch}
        rows.append(row)
    _write_jsonl(prelabels_path, rows)


def test_combined_arm_is_scored_and_is_the_cascade_that_ships(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    assert "combined" in evaluate.ARMS
    assert set(result["headline"]) == {"rules", "llm", "combined"}

    # p1: rule candidate ($TSLA) and the LLM confirms   -> positive, human positive, TP w=1
    # p2: rule candidate ($AAPL) and the LLM refuses    -> negative, human negative, TN w=1
    # p3: no rule candidate, the LLM is never consulted -> negative, human negative, TN w=3
    # p4: no rule candidate, so the LLM's positive is never asked for and never
    #     applied           -> negative, human positive, FN w=3
    combined = result["headline"]["combined"]["weighted"]
    assert combined["tp"] == 1.0
    assert combined["fp"] == 0.0
    assert combined["fn"] == 3.0
    assert combined["tn"] == 4.0
    assert combined["precision"] == 1.0
    assert combined["recall"] == 0.25


def test_combined_recall_cannot_exceed_the_rule_arm_recall(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    rules = result["headline"]["rules"]["weighted"]
    llm = result["headline"]["llm"]["weighted"]
    combined = result["headline"]["combined"]["weighted"]

    # The cascade gates the LLM on a rule candidate, so it can only ever turn a
    # rule positive off, never turn a rule negative on. p4 is the whole point:
    # the LLM gets it right on its own and the shipped detector still misses it.
    assert llm["recall"] == 1.0
    assert combined["recall"] == rules["recall"]
    assert combined["recall"] < llm["recall"]


def test_combined_arm_needs_no_llm_prediction_where_the_cascade_would_not_call_one(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)
    # p1 has a rule candidate, so the cascade would call the LLM and cannot be
    # scored without its answer. p4 has none, so the cascade short circuits on
    # the rule verdict and stays scoreable.
    _rewrite_prelabels(prelabels_path, {"p1": None, "p4": None})

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    assert result["headline"]["llm"]["n_skipped_no_prediction"] == 2
    assert result["headline"]["combined"]["n_skipped_no_prediction"] == 1
    combined = result["headline"]["combined"]["weighted"]
    assert combined["n"] == 3
    # p4 still counts as the false negative it is, without a guess standing in
    # for the missing prelabel.
    assert combined["fn"] == 3.0


def test_total_agreement_with_the_labels_is_flagged_not_reported_as_a_score(tmp_path, capsys):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)
    report_path = tmp_path / "metrics.md"

    exit_code = evaluate.main(
        [
            "--labeled",
            str(labeled_path),
            "--sample",
            str(sample_path),
            "--prelabels",
            str(prelabels_path),
            "--lexicon",
            str(_LEXICON_PATH),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)
    assert result["agreement"]["llm"] == {
        "n_scored": 5,
        "n_agree": 5,
        "rate": 1.0,
        "total": True,
    }

    console = capsys.readouterr().out
    written = report_path.read_text(encoding="utf-8")
    # The warning has to reach both places, since metrics.md is what gets read
    # later and the console is what gets read now.
    for text in (console, written):
        assert "WARNING" in text
        assert "5/5 labeled rows" in text
        assert "not a measurement" in text
        assert "x == x" in text


def test_partial_agreement_is_reported_without_a_circularity_warning(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)
    # One disagreement is enough: the arm is now capable of being wrong against
    # these labels, which is exactly what the check is looking for.
    _rewrite_prelabels(prelabels_path, {"p3": {"is_stock_related": True}})

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    llm_agreement = result["agreement"]["llm"]
    assert llm_agreement["n_agree"] == 4
    assert llm_agreement["n_scored"] == 5
    assert llm_agreement["total"] is False
    assert all(not result["agreement"][arm]["total"] for arm in evaluate.ARMS)
    assert "WARNING" not in result["report_text"]


def test_report_names_the_model_that_produced_the_scored_predictions(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    assert result["prelabel_models"] == [_PRELABEL_MODEL]
    assert _PRELABEL_MODEL in result["report_text"]
    # And it says out loud that this is not necessarily what the agent runs.
    assert "not necessarily the model the agent runs" in result["report_text"]


def test_unstamped_prelabels_report_the_model_as_unknown_rather_than_guessing(tmp_path):
    sample_path, labeled_path, prelabels_path = _build_handcrafted_set(tmp_path)
    _rewrite_prelabels(
        prelabels_path,
        {post_id: {"model": None} for post_id in ("p1", "p2", "p3", "p4", "p5")},
    )

    result = evaluate.evaluate(labeled_path, sample_path, prelabels_path, _LEXICON_PATH)

    assert result["prelabel_models"] == []
    assert "model that produced the scored LLM predictions: unknown" in result["report_text"]
