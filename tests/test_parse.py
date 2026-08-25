from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tsalert.sources.parse import MalformedStatusError, html_to_text, parse_status


def test_parses_all_fixture_statuses_without_error(all_statuses):
    posts = [parse_status(s, source="fixture") for s in all_statuses]
    assert len(posts) == 40
    assert sum(p.is_repost for p in posts) == 3
    assert sum(p.is_quote for p in posts) == 2
    assert sum(p.has_media for p in posts) == 13


def test_text_has_no_markup_and_entities_unescaped(all_statuses):
    for status in all_statuses:
        post = parse_status(status, source="fixture")
        assert "<" not in post.text
        assert "&amp;" not in post.text
        assert "&#39;" not in post.text
        assert "&quot;" not in post.text


def test_known_repost_flagged_and_keeps_outer_id(all_statuses):
    by_id = {s["id"]: s for s in all_statuses}
    status = by_id["117156901249242368"]
    assert status["reblog"] is not None
    post = parse_status(status, source="fixture")
    assert post.is_repost is True
    assert post.id == "117156901249242368"
    assert post.id != status["reblog"]["id"]


def test_known_quote_flagged(all_statuses):
    by_id = {s["id"]: s for s in all_statuses}
    status = by_id["117156926105160416"]
    assert status["quote_id"] == "117156925333799291"
    post = parse_status(status, source="fixture")
    assert post.is_quote is True


def test_created_at_is_tz_aware_utc_and_matches_fixture(all_statuses):
    status = all_statuses[0]
    post = parse_status(status, source="fixture")
    assert post.created_at.tzinfo is not None
    assert post.created_at.utcoffset() == timedelta(0)
    expected = datetime.fromisoformat(status["created_at"].replace("Z", "+00:00"))
    assert post.created_at == expected


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"created_at": "2026-08-25T18:32:56.587Z"},
        {"id": None, "created_at": "2026-08-25T18:32:56.587Z"},
        {"id": "1", "created_at": "not-a-date"},
    ],
)
def test_malformed_status_raises(status):
    with pytest.raises(MalformedStatusError):
        parse_status(status, source="fixture")


def test_missing_content_yields_empty_text():
    status = {"id": "1", "created_at": "2026-08-25T18:32:56.587Z"}
    post = parse_status(status, source="fixture")
    assert post.text == ""


def test_html_to_text_br_and_paragraph_to_newline():
    assert html_to_text("<p>a<br/>b</p><p>c</p>") == "a\nb\nc"


def test_html_to_text_preserves_anchor_visible_text():
    html_content = '<p>See <a href="https://example.com">example.com</a></p>'
    assert html_to_text(html_content) == "See example.com"


def test_html_to_text_collapses_excess_newlines():
    assert html_to_text("a<br/><br/><br/><br/>b") == "a\n\nb"
