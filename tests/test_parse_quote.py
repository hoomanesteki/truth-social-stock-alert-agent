from __future__ import annotations

from tsalert.sources.parse import html_to_text, parse_status


def test_real_quote_posts_populate_quoted_text_from_quote_content(all_statuses):
    quote_statuses = [s for s in all_statuses if s.get("quote_id")]
    assert len(quote_statuses) == 2
    for status in quote_statuses:
        post = parse_status(status, source="fixture")
        assert post.is_quote is True
        # Both real quote posts in the fixtures quote a media-only post with
        # no text of its own (content is "<p></p>"), so quoted_text is
        # legitimately empty for these two. This still proves the plumbing
        # pulls from status["quote"]["content"] rather than ignoring it.
        assert post.quoted_text == html_to_text(status["quote"]["content"])
        assert "RT:" in post.text


def test_detection_text_joins_text_and_quoted_text_when_quote_has_words():
    status = {
        "id": "1",
        "created_at": "2026-08-25T18:32:56.587Z",
        "content": (
            '<p><span class="quote-inline"><br/>RT: '
            "https://truthsocial.com/users/x/statuses/2</span></p>"
        ),
        "quote_id": "2",
        "quote": {"content": "<p>Buy $TSLA now, big things coming.</p>"},
    }
    post = parse_status(status, source="fixture")

    assert post.text.startswith("RT:")
    assert post.quoted_text == "Buy $TSLA now, big things coming."
    assert post.detection_text == f"{post.text}\n\n{post.quoted_text}"
    assert "TSLA" in post.detection_text
    # text itself stays just the RT stub, sentiment needs to tell "what he
    # said" apart from "what he amplified".
    assert "TSLA" not in post.text


def test_detection_text_equals_text_when_there_is_no_quote():
    status = {
        "id": "1",
        "created_at": "2026-08-25T18:32:56.587Z",
        "content": "<p>Just a normal post.</p>",
    }
    post = parse_status(status, source="fixture")

    assert post.quoted_text == ""
    assert post.detection_text == post.text
