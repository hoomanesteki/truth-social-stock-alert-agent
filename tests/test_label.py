from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import label  # noqa: E402


def _sample_row(post_id: str, text: str, stratum: str, weight: float = 1.0, in_weighted_metrics: bool = True) -> dict:
    return {
        "post_id": post_id,
        "text": text,
        "created_at": "2026-01-01T00:00:00+00:00",
        "url": f"https://truthsocial.com/@realDonaldTrump/{post_id}",
        "stratum": stratum,
        "stratum_population": 1,
        "stratum_sample_size": 1,
        "weight": weight,
        "in_weighted_metrics": in_weighted_metrics,
    }


def _prelabel_row(post_id: str, is_stock_related: bool, category: str, tickers: list[str], companies: list[str] | None = None) -> dict:
    return {
        "post_id": post_id,
        "is_stock_related": is_stock_related,
        "category": category,
        "tickers": tickers,
        "companies": companies or [],
        "reasoning": "test reasoning",
        "model": "openai/gpt-oss-120b",
        "cached": False,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _make_rows(n_candidate: int, n_random: int, n_hard_negative: int) -> list[dict]:
    rows = []
    for i in range(n_candidate):
        rows.append(_sample_row(f"cand-{i}", f"candidate text {i}", "candidate"))
    for i in range(n_random):
        rows.append(_sample_row(f"rand-{i}", f"random text {i}", "random"))
    for i in range(n_hard_negative):
        rows.append(_sample_row(f"trap-{i}", f"trap text {i}", "hard_negative", weight=0.0, in_weighted_metrics=False))
    return rows


def test_blind_selection_deterministic_and_proportional_across_strata():
    rows = _make_rows(n_candidate=60, n_random=30, n_hard_negative=10)

    blind_ids_1 = label.select_blind_post_ids(rows, blind_count=20, seed=42)
    blind_ids_2 = label.select_blind_post_ids(rows, blind_count=20, seed=42)

    # deterministic for a fixed seed
    assert blind_ids_1 == blind_ids_2
    assert len(blind_ids_1) == 20

    # proportional across strata: 60/100, 30/100, 10/100 of a target of 20
    # divides evenly, so the split must be exact.
    by_stratum = {row["post_id"]: row["stratum"] for row in rows}
    counts = {"candidate": 0, "random": 0, "hard_negative": 0}
    for post_id in blind_ids_1:
        counts[by_stratum[post_id]] += 1
    assert counts == {"candidate": 12, "random": 6, "hard_negative": 2}

    # a different seed is free to pick different posts but must still respect
    # the same proportional split and still be deterministic.
    blind_ids_3 = label.select_blind_post_ids(rows, blind_count=20, seed=7)
    counts_3 = {"candidate": 0, "random": 0, "hard_negative": 0}
    for post_id in blind_ids_3:
        counts_3[by_stratum[post_id]] += 1
    assert counts_3 == {"candidate": 12, "random": 6, "hard_negative": 2}


def test_blind_selection_never_exceeds_available_rows():
    rows = _make_rows(n_candidate=2, n_random=1, n_hard_negative=0)
    blind_ids = label.select_blind_post_ids(rows, blind_count=100, seed=1)
    assert len(blind_ids) == 3
    assert blind_ids == {"cand-0", "cand-1", "rand-0"}


def test_changed_from_prelabel_agree_and_disagree_cases():
    prelabel = _prelabel_row("p1", True, "specific_equity", ["AAPL", "MSFT"])

    # exact agreement, ticker order and case should not matter
    assert label.compute_changed_from_prelabel(prelabel, True, "specific_equity", ["msft", "aapl"]) is False

    # category disagreement
    assert label.compute_changed_from_prelabel(prelabel, False, "macro_market", []) is True

    # is_stock_related disagreement (defensive, should track category anyway)
    assert label.compute_changed_from_prelabel(prelabel, False, "specific_equity", ["AAPL", "MSFT"]) is True

    # ticker set disagreement
    assert label.compute_changed_from_prelabel(prelabel, True, "specific_equity", ["AAPL"]) is True

    # no prelabel at all always counts as changed
    assert label.compute_changed_from_prelabel(None, True, "specific_equity", ["AAPL"]) is True

    # agreement on a non equity category with no tickers on either side
    prelabel_macro = _prelabel_row("p2", False, "macro_market", [])
    assert label.compute_changed_from_prelabel(prelabel_macro, False, "macro_market", []) is False


def test_auto_propagation_copies_decision_to_byte_identical_text(tmp_path, monkeypatch):
    duplicate_text = "Intel Politics, the same post three times over."
    rows = [
        _sample_row("dup-1", duplicate_text, "candidate"),
        _sample_row("dup-2", duplicate_text, "candidate"),
        _sample_row("dup-3", duplicate_text, "candidate"),
        _sample_row("unique-1", "a completely different post", "random"),
    ]
    sample_path = tmp_path / "eval_sample.jsonl"
    prelabels_path = tmp_path / "prelabels.jsonl"
    out_path = tmp_path / "labeled.jsonl"
    _write_jsonl(sample_path, rows)
    _write_jsonl(prelabels_path, [])

    # blind_count 0 so every post shows a proposal and takes the s/i/m/n path
    inputs = iter(["s", "INTC", "n", "q"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))

    rc = label.main(
        [
            "--sample",
            str(sample_path),
            "--prelabels",
            str(prelabels_path),
            "--out",
            str(out_path),
            "--blind-count",
            "0",
            "--seed",
            "42",
        ]
    )
    assert rc == 0

    out_rows = label.load_jsonl(out_path)
    by_id = {row["post_id"]: row for row in out_rows}

    # the first post labeled directly, the other two byte identical posts
    # auto propagated with the same decision.
    assert by_id["dup-1"]["propagated"] is False
    assert by_id["dup-2"]["propagated"] is True
    assert by_id["dup-3"]["propagated"] is True
    for post_id in ("dup-1", "dup-2", "dup-3"):
        assert by_id[post_id]["category"] == "specific_equity"
        assert by_id[post_id]["tickers"] == ["INTC"]
        assert by_id[post_id]["is_stock_related"] is True

    # the fourth post (unique text) was labeled separately as not_financial
    assert by_id["unique-1"]["category"] == "not_financial"
    assert by_id["unique-1"]["propagated"] is False

    assert len(out_rows) == 4


def test_resumability_skips_already_labeled_post_ids(tmp_path, monkeypatch):
    rows = [
        _sample_row("p1", "already labeled post", "candidate"),
        _sample_row("p2", "still needs a label", "random"),
    ]
    sample_path = tmp_path / "eval_sample.jsonl"
    prelabels_path = tmp_path / "prelabels.jsonl"
    out_path = tmp_path / "labeled.jsonl"
    _write_jsonl(sample_path, rows)
    _write_jsonl(prelabels_path, [])

    # pre populate the output as if a previous session already labeled p1
    prior_row = label.build_output_row(
        rows[0],
        is_stock_related=False,
        category="not_financial",
        tickers=[],
        companies=[],
        blind=False,
        propagated=False,
        changed_from_prelabel=True,
        labeled_at="2026-01-01T00:00:00+00:00",
    )
    _write_jsonl(out_path, [prior_row])

    # only one input consumed: if p1 were re-shown, this single "n" would be
    # spent on it and the loop would hang waiting for a second input, which
    # would raise StopIteration and fail the test.
    inputs = iter(["n", "q"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))

    rc = label.main(
        [
            "--sample",
            str(sample_path),
            "--prelabels",
            str(prelabels_path),
            "--out",
            str(out_path),
            "--blind-count",
            "0",
            "--seed",
            "42",
        ]
    )
    assert rc == 0

    out_rows = label.load_jsonl(out_path)
    post_ids = [row["post_id"] for row in out_rows]
    assert post_ids == ["p1", "p2"]
    # the original p1 row was left untouched, not relabeled
    assert out_rows[0]["labeled_at"] == "2026-01-01T00:00:00+00:00"


def test_build_queue_excludes_labeled_ids_and_orders_blind_first():
    rows = _make_rows(n_candidate=2, n_random=2, n_hard_negative=0)
    blind_ids = {"rand-0"}
    labeled_ids = {"cand-0"}

    queue = label.build_queue(rows, blind_ids, labeled_ids)

    assert queue[0] == "rand-0"
    assert "cand-0" not in queue
    assert set(queue) == {"rand-0", "cand-1", "rand-1"}
