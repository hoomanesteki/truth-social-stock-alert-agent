from __future__ import annotations

from datetime import datetime, timezone

from tsalert.models import Detection, Post, TickerMention
from tsalert.store import Store


def make_post(post_id: str = "1") -> Post:
    now = datetime(2026, 8, 25, 18, 32, 56, tzinfo=timezone.utc)
    return Post(
        id=post_id,
        account="realDonaldTrump",
        created_at=now,
        text="hello",
        url=f"https://truthsocial.com/@realDonaldTrump/{post_id}",
        raw_html="<p>hello</p>",
        is_reply=False,
        is_repost=False,
        is_quote=False,
        has_media=False,
        source="fixture",
        fetched_at=now,
    )


def test_upsert_post_dedup(tmp_path):
    post = make_post()
    with Store(tmp_path / "agent.db") as store:
        assert store.upsert_post(post) is True
        assert store.upsert_post(post) is False


def test_dedup_survives_reopen(tmp_path):
    db_path = tmp_path / "agent.db"
    post = make_post()
    with Store(db_path) as store:
        assert store.upsert_post(post) is True
    with Store(db_path) as store:
        assert store.upsert_post(post) is False


def test_claim_alert_idempotent(tmp_path):
    post = make_post()
    with Store(tmp_path / "agent.db") as store:
        store.upsert_post(post)
        assert store.claim_alert(post.id, "telegram") is True
        assert store.claim_alert(post.id, "telegram") is False


def test_state_round_trip(tmp_path):
    with Store(tmp_path / "agent.db") as store:
        assert store.get_state("last_seen_id") is None
        assert store.get_state("last_seen_id", "default") == "default"
        store.set_state("last_seen_id", "12345")
        assert store.get_state("last_seen_id") == "12345"
        store.set_state("last_seen_id", "67890")
        assert store.get_state("last_seen_id") == "67890"


def test_stats_reflects_inserted_rows(tmp_path):
    with Store(tmp_path / "agent.db") as store:
        store.upsert_post(make_post("1"))
        store.upsert_post(make_post("2"))
        store.claim_alert("1", "telegram")
        store.record_alert_result("1", "telegram", "delivered")
        store.claim_alert("2", "telegram")
        store.record_alert_result("2", "telegram", "failed", error="boom")
        stats = store.stats()
        assert stats["posts"] == 2
        assert stats["alerts_delivered"] == 1
        assert stats["alerts_failed"] == 1


def test_save_detection_updates_stock_related(tmp_path):
    with Store(tmp_path / "agent.db") as store:
        store.upsert_post(make_post("1"))
        mention = TickerMention(
            ticker="TSLA", company="Tesla", matched_text="Tesla", method="alias", confidence=0.9
        )
        detection = Detection(
            post_id="1", is_stock_related=True, mentions=(mention,), detector="rules", latency_ms=12.5
        )
        store.save_detection(detection)
        assert store.stats()["stock_related"] == 1


def test_recent_posts_and_iter_posts(tmp_path):
    with Store(tmp_path / "agent.db") as store:
        store.upsert_post(make_post("1"))
        store.upsert_post(make_post("2"))
        recent = store.recent_posts(limit=10)
        assert {p.id for p in recent} == {"1", "2"}
        assert {p.id for p in store.iter_posts()} == {"1", "2"}
