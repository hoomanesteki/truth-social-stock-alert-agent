from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prelabel
from tsalert.llm import GroqClient


def _good_body(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = {}
        self.text = ""

    def json(self):
        return self._json_body


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_resumability_skips_post_ids_already_in_output(tmp_path):
    in_path = tmp_path / "eval_sample.jsonl"
    out_path = tmp_path / "prelabels.jsonl"

    posts = [
        {"post_id": "1", "text": "Boeing is building Air Force One"},
        {"post_id": "2", "text": "President DONALD J. TRUMP"},
        {"post_id": "3", "text": "The stock market is booming"},
    ]
    _write_jsonl(in_path, posts)

    # post_id "1" already has a label in the output file.
    _write_jsonl(
        out_path,
        [
            {
                "post_id": "1",
                "is_stock_related": True,
                "category": "specific_equity",
                "tickers": ["BA"],
                "companies": ["Boeing"],
                "reasoning": "already labeled",
                "model": "openai/gpt-oss-120b",
                "cached": False,
            }
        ],
    )

    calls: list[dict] = []

    def transport(url, payload):
        calls.append(payload)
        text = payload["messages"][1]["content"]
        if "TRUMP" in text:
            body = {
                "is_stock_related": False,
                "category": "not_financial",
                "tickers": [],
                "companies": [],
                "reasoning": "signature",
            }
        else:
            body = {
                "is_stock_related": False,
                "category": "macro_market",
                "tickers": [],
                "companies": [],
                "reasoning": "generic market talk",
            }
        return FakeResponse(200, json_body=_good_body(body))

    client = GroqClient(
        api_key="fake-key",
        model="openai/gpt-oss-120b",
        transport=transport,
        sleep=lambda s: None,
    )

    summary = prelabel.run(client, in_path, out_path)

    assert summary["skipped"] == 1
    assert summary["labeled"] == 2
    # only the two un-labeled posts triggered a call, post_id "1" was skipped
    assert len(calls) == 2

    out_lines = [json.loads(line) for line in out_path.read_text().splitlines()]
    post_ids = [row["post_id"] for row in out_lines]
    assert post_ids == ["1", "2", "3"]
    assert post_ids.count("1") == 1  # not re-labeled or duplicated


def test_resumability_across_two_runs_labels_only_new_posts(tmp_path):
    in_path = tmp_path / "eval_sample.jsonl"
    out_path = tmp_path / "prelabels.jsonl"

    posts = [
        {"post_id": "10", "text": "Micron announced a new plant"},
        {"post_id": "11", "text": "S&P 500 closed higher today"},
    ]
    _write_jsonl(in_path, posts)

    calls = {"n": 0}

    def transport(url, payload):
        calls["n"] += 1
        body = {
            "is_stock_related": False,
            "category": "not_financial",
            "tickers": [],
            "companies": [],
            "reasoning": "placeholder",
        }
        return FakeResponse(200, json_body=_good_body(body))

    client = GroqClient(
        api_key="fake-key",
        model="openai/gpt-oss-120b",
        transport=transport,
        sleep=lambda s: None,
    )

    first_summary = prelabel.run(client, in_path, out_path)
    assert first_summary["labeled"] == 2
    assert calls["n"] == 2

    # A second run against the same output file should skip both posts.
    second_summary = prelabel.run(client, in_path, out_path)
    assert second_summary["labeled"] == 0
    assert second_summary["skipped"] == 2
    assert calls["n"] == 2  # no new transport calls on the resumed run

    out_lines = out_path.read_text().splitlines()
    assert len(out_lines) == 2  # no duplicate rows written
