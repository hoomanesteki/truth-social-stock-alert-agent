from __future__ import annotations

from tsalert.sources.fixture import FixtureSource


def _source(fixtures_dir):
    return FixtureSource([fixtures_dir / "statuses_page1.json", fixtures_dir / "statuses_page2.json"])


def test_fetch_latest_oldest_first(fixtures_dir):
    source = _source(fixtures_dir)
    posts = source.fetch_latest(limit=40)
    ids = [int(p.id) for p in posts]
    assert ids == sorted(ids)
    assert len(posts) == 40


def test_fetch_latest_since_id_strictly_newer(fixtures_dir):
    source = _source(fixtures_dir)
    all_posts = source.fetch_latest(limit=40)
    midpoint = all_posts[20].id

    newer = source.fetch_latest(since_id=midpoint, limit=100)

    assert all(int(p.id) > int(midpoint) for p in newer)
    assert len(newer) == 19


def test_fetch_history_before_id_walks_backwards(fixtures_dir):
    source = _source(fixtures_dir)
    first_page = source.fetch_history(limit=10)
    first_ids = [int(p.id) for p in first_page]
    assert first_ids == sorted(first_ids, reverse=True)

    oldest_in_first_page = first_page[-1].id
    second_page = source.fetch_history(before_id=oldest_in_first_page, limit=10)

    assert all(int(p.id) < int(oldest_in_first_page) for p in second_page)
    assert {p.id for p in first_page}.isdisjoint({p.id for p in second_page})
