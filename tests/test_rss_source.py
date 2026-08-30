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


_FEED_WITH_MISSING_ID = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:truth="https://truthsocial.com/ns">
    <channel>
        <item>
            <description><![CDATA[<p>has an id</p>]]></description>
            <pubDate>Tue, 25 Aug 2026 18:32:56 +0000</pubDate>
            <truth:originalUrl>https://truthsocial.com/@realDonaldTrump/1</truth:originalUrl>
            <truth:originalId>1</truth:originalId>
        </item>
        <item>
            <description><![CDATA[<p>missing originalId entirely</p>]]></description>
            <pubDate>Tue, 25 Aug 2026 18:19:49 +0000</pubDate>
            <truth:originalUrl>https://truthsocial.com/@realDonaldTrump/2</truth:originalUrl>
        </item>
        <item>
            <description><![CDATA[<p>blank originalId</p>]]></description>
            <pubDate>Tue, 25 Aug 2026 18:19:39 +0000</pubDate>
            <truth:originalUrl>https://truthsocial.com/@realDonaldTrump/3</truth:originalUrl>
            <truth:originalId>   </truth:originalId>
        </item>
    </channel>
</rss>
"""


def test_item_missing_original_id_is_skipped_not_given_a_bogus_id():
    source = _source(_FEED_WITH_MISSING_ID)

    posts = source.fetch_latest(limit=200)

    assert [p.id for p in posts] == ["1"]
    assert source.skipped_missing_id_count == 2


def test_a_non_numeric_id_is_skipped_rather_than_poisoning_the_cursor():
    """Truth Social ids are numeric, and id_sort_key ranks a non-numeric id
    above every numeric one. This mirror filters since_id on the client, so
    the moment such an id became last_seen_post_id the filter excluded every
    real post after it. The cursor lives in the store, so a restart did not
    clear it: ingestion from the mirror stopped dead and stayed stopped.
    """
    feed = """<?xml version="1.0"?>
<rss xmlns:truth="https://truthsocial.com/ns"><channel>
  <item>
    <truth:originalId>post-abc</truth:originalId>
    <truth:originalUrl>https://truthsocial.com/a</truth:originalUrl>
    <description>Apple stock is up</description>
    <pubDate>Mon, 25 Aug 2026 10:00:00 +0000</pubDate>
  </item>
  <item>
    <truth:originalId>117000000000000001</truth:originalId>
    <truth:originalUrl>https://truthsocial.com/b</truth:originalUrl>
    <description>Buy Nvidia now</description>
    <pubDate>Mon, 25 Aug 2026 11:00:00 +0000</pubDate>
  </item>
</channel></rss>"""

    source = _source(feed)
    posts = source.fetch_latest()

    assert [p.id for p in posts] == ["117000000000000001"]
    assert source.skipped_missing_id_count == 1
