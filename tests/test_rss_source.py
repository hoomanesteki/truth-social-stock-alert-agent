from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timedelta, timezone

from tsalert.sources.rss_mirror import TrumpsTruthRssSource

_NS = {"truth": "https://truthsocial.com/ns"}


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _source(feed_xml: str) -> TrumpsTruthRssSource:
    transport = lambda url, params: FakeResponse(feed_xml)
    return TrumpsTruthRssSource(transport=transport)


def test_parses_all_feed_items(rss_feed_xml):
    source = _source(rss_feed_xml)
    posts = source.fetch_latest(limit=200)
    assert len(posts) == 100


def test_ids_match_truth_original_id(rss_feed_xml):
    root = ET.fromstring(rss_feed_xml)
    expected_ids = {
        item.findtext("truth:originalId", namespaces=_NS)
        for item in root.findall("./channel/item")
    }
    source = _source(rss_feed_xml)
    posts = source.fetch_latest(limit=200)
    assert {p.id for p in posts} == expected_ids


def test_pub_date_becomes_tz_aware_utc(rss_feed_xml):
    source = _source(rss_feed_xml)
    posts = source.fetch_latest(limit=200)
    for post in posts:
        assert post.created_at.tzinfo is not None
        assert post.created_at.utcoffset() == timedelta(0)


def test_fetch_history_is_newest_first(rss_feed_xml):
    source = _source(rss_feed_xml)
    posts = source.fetch_history(limit=200)
    ids = [int(p.id) for p in posts]
    assert ids == sorted(ids, reverse=True)


def test_flags_are_false_feed_does_not_carry_them(rss_feed_xml):
    source = _source(rss_feed_xml)
    posts = source.fetch_latest(limit=200)
    assert all(not p.is_repost and not p.is_quote and not p.has_media for p in posts)
