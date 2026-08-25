from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_eval_set  # noqa: E402
from tsalert.models import Post  # noqa: E402

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_post(post_id: str, text: str, offset_seconds: int) -> Post:
    created = BASE_TIME + timedelta(seconds=offset_seconds)
    return Post(
        id=post_id,
        account="realDonaldTrump",
        created_at=created,
        text=text,
        url=f"https://truthsocial.com/@realDonaldTrump/{post_id}",
        raw_html=f"<p>{text}</p>",
        is_reply=False,
        is_repost=False,
        is_quote=False,
        has_media=False,
        source="test",
        fetched_at=created,
        quoted_text="",
    )


def _write_history(
    path: Path,
    n_candidate: int,
    n_trap: int,
    n_random: int,
    n_empty: int,
    n_both: int = 0,
) -> None:
    posts: list[Post] = []
    offset = 0

    for i in range(n_candidate):
        # STRONG_CONTEXT word ("earnings"), no lexicon company/ticker so this
        # lands in stratum A purely via the STRONG_CONTEXT branch, not via
        # any emitted mention.
        posts.append(_make_post(f"cand-{i}", f"quarterly earnings report released today, entry {i}", offset))
        offset += 1

    for i in range(n_trap):
        # DJT sign off trap, unconditionally lands in stratum C (as long as
        # it carries no candidate signal of its own).
        posts.append(_make_post(f"trap-{i}", f"Great news everyone. President DJT (entry {i})", offset))
        offset += 1

    for i in range(n_random):
        posts.append(_make_post(f"rand-{i}", f"Just an ordinary day, nothing notable, entry {i}", offset))
        offset += 1

    for i in range(n_empty):
        posts.append(_make_post(f"empty-{i}", "", offset))
        offset += 1

    for i in range(n_both):
        # Both candidate shaped (Micron alias match, a real ticker mention)
        # AND trap shaped (a DJT sign off in the same post). Priority A then
        # C then B means this must land in A, not C.
        posts.append(
            _make_post(
                f"both-{i}",
                f"Micron just posted incredible earnings, entry {i}. President DONALD J. TRUMP",
                offset,
            )
        )
        offset += 1

    with path.open("w", encoding="utf-8") as f:
        for post in posts:
            f.write(json.dumps(post.to_dict()) + "\n")


def test_strata_disjoint_and_weights_correct(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    _write_history(history_path, n_candidate=50, n_trap=40, n_random=200, n_empty=5)

    rows, summary = build_eval_set.build_sample(history_path, seed=42)

    ids_by_stratum: dict[str, set[str]] = {}
    for row in rows:
        ids_by_stratum.setdefault(row["stratum"], set()).add(row["post_id"])

    all_ids = [row["post_id"] for row in rows]
    assert len(all_ids) == len(set(all_ids))  # no post appears twice

    everything = set()
    for ids in ids_by_stratum.values():
        assert everything.isdisjoint(ids)
        everything |= ids

    for row in rows:
        if row["stratum"] == "hard_negative":
            # Purposive sample: weight is hardcoded 0.0, not
            # population / sample_size, and excluded from weighted metrics.
            assert row["weight"] == 0.0
            assert row["in_weighted_metrics"] is False
        else:
            assert row["weight"] == row["stratum_population"] / row["stratum_sample_size"]
            assert row["in_weighted_metrics"] is True

    assert summary["excluded_empty_text"] == 5
    assert summary["posts_read"] == 50 + 40 + 200 + 5


def test_candidate_stratum_is_a_census(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    _write_history(history_path, n_candidate=50, n_trap=40, n_random=200, n_empty=0)

    rows, summary = build_eval_set.build_sample(history_path, seed=42)

    candidate_summary = next(s for s in summary["strata"] if s["name"] == "candidate")
    assert candidate_summary["population"] == candidate_summary["sample_size"]
    assert candidate_summary["weight"] == 1.0
    assert candidate_summary["in_weighted_metrics"] is True

    candidate_rows = [r for r in rows if r["stratum"] == "candidate"]
    assert len(candidate_rows) == candidate_summary["population"] == 50


def test_post_that_is_both_candidate_and_trap_lands_in_a(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    _write_history(history_path, n_candidate=10, n_trap=10, n_random=50, n_empty=0, n_both=3)

    rows, summary = build_eval_set.build_sample(history_path, seed=42)

    rows_by_id = {row["post_id"]: row for row in rows}
    for i in range(3):
        post_id = f"both-{i}"
        assert post_id in rows_by_id, "a post that is both candidate and trap shaped must be sampled"
        assert rows_by_id[post_id]["stratum"] == "candidate"
        assert rows_by_id[post_id]["in_weighted_metrics"] is True

    candidate_summary = next(s for s in summary["strata"] if s["name"] == "candidate")
    # 10 plain candidates + 3 both-shaped posts, all claimed by A before C
    # ever gets a look at them.
    assert candidate_summary["population"] == 13


def test_hard_negative_rows_excluded_from_weighted_metrics(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    _write_history(history_path, n_candidate=50, n_trap=40, n_random=200, n_empty=0)

    rows, summary = build_eval_set.build_sample(history_path, seed=42)

    hard_negative_summary = next(s for s in summary["strata"] if s["name"] == "hard_negative")
    assert hard_negative_summary["weight"] == 0.0
    assert hard_negative_summary["in_weighted_metrics"] is False

    hard_negative_rows = [r for r in rows if r["stratum"] == "hard_negative"]
    assert hard_negative_rows  # sanity: the pool actually produced sampled rows
    for row in hard_negative_rows:
        assert row["weight"] == 0.0
        assert row["in_weighted_metrics"] is False


def test_total_sampled_is_150_when_pool_is_large_enough(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    # 50 candidates (census) + 25 of 40 traps + enough random to reach 150.
    _write_history(history_path, n_candidate=50, n_trap=40, n_random=200, n_empty=0)

    rows, summary = build_eval_set.build_sample(history_path, seed=42)

    assert summary["total_sampled"] == 150
    assert len(rows) == 150

    hard_negative_summary = next(s for s in summary["strata"] if s["name"] == "hard_negative")
    assert hard_negative_summary["sample_size"] == 25

    random_summary = next(s for s in summary["strata"] if s["name"] == "random")
    assert random_summary["sample_size"] == 150 - 50 - 25


def test_same_seed_produces_identical_output(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    _write_history(history_path, n_candidate=50, n_trap=40, n_random=200, n_empty=5)

    rows_a, summary_a = build_eval_set.build_sample(history_path, seed=42)
    rows_b, summary_b = build_eval_set.build_sample(history_path, seed=42)

    assert rows_a == rows_b
    assert summary_a == summary_b


def test_cli_run_twice_is_byte_identical(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    _write_history(history_path, n_candidate=10, n_trap=10, n_random=50, n_empty=2)

    out_a = tmp_path / "eval_a.jsonl"
    out_b = tmp_path / "eval_b.jsonl"

    build_eval_set.main(["--history", str(history_path), "--out", str(out_a), "--seed", "42"])
    build_eval_set.main(["--history", str(history_path), "--out", str(out_b), "--seed", "42"])

    assert out_a.read_bytes() == out_b.read_bytes()


def test_b_skipped_when_a_and_c_already_cover_150(tmp_path: Path):
    history_path = tmp_path / "history.jsonl"
    # Far more candidates than 150 by itself: stratum B should be skipped
    # and the true (larger than 150) total reported honestly.
    _write_history(history_path, n_candidate=200, n_trap=40, n_random=30, n_empty=0)

    rows, summary = build_eval_set.build_sample(history_path, seed=42)

    random_summary = next(s for s in summary["strata"] if s["name"] == "random")
    assert random_summary["sample_size"] == 0

    candidate_summary = next(s for s in summary["strata"] if s["name"] == "candidate")
    assert candidate_summary["sample_size"] == 200
    assert summary["total_sampled"] == 200 + 25
    assert len(rows) == summary["total_sampled"]
