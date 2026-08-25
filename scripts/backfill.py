#!/usr/bin/env python3
"""Walk the primary API backwards via max_id to build a local post archive.

    uv run python scripts/backfill.py --days 45 --delay 2.5 --out data/history.jsonl

This talks to the live network and is meant to be run by hand, not from
tests or CI. It is resumable: the max_id cursor is persisted in agent_state
after every page, so a re-run continues instead of starting over.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tsalert.config import Config
from tsalert.reliability import with_retries
from tsalert.sources.base import PermanentSourceError
from tsalert.sources.truthsocial import TruthSocialApiSource
from tsalert.store import Store

STATE_KEY = "backfill_max_id_cursor"
DEFAULT_DELAY = 2.5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill historical posts from the primary API.")
    parser.add_argument("--days", type=int, default=45, help="how many days back to walk")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, help="seconds between page requests"
    )
    parser.add_argument("--out", default="data/history.jsonl", help="JSONL output path")
    parser.add_argument("--db", default=None, help="SQLite db path, defaults to Config.db_path")
    parser.add_argument("--limit", type=int, default=20, help="posts requested per page")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.delay < DEFAULT_DELAY:
        # Measured safe at 2.5s in testing. Lowering it risks the account's
        # IP getting rate limited or blocked, which would kill the project.
        raise SystemExit(
            f"refusing to run with --delay below the {DEFAULT_DELAY}s politeness floor"
        )

    config = Config.from_env()
    db_path = args.db or config.db_path
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    source = TruthSocialApiSource(
        account_id=config.account_id,
        impersonate=config.impersonate,
        timeout=config.request_timeout,
    )

    pages = 0
    total_posts = 0
    oldest_seen: datetime | None = None

    with Store(db_path) as store, out_path.open("a", encoding="utf-8") as out_file:
        cursor = store.get_state(STATE_KEY)
        posts_before = store.stats()["posts"]
        if cursor is not None:
            print(f"resuming: {posts_before} posts already collected, cursor={cursor}")

        try:
            while True:
                page = with_retries(lambda: source.fetch_history(before_id=cursor, limit=args.limit))
                pages += 1

                if not page:
                    print("reached an empty page, stopping")
                    break

                for post in page:
                    store.upsert_post(post)
                    out_file.write(json.dumps(post.to_dict()) + "\n")
                    total_posts += 1
                    if oldest_seen is None or post.created_at < oldest_seen:
                        oldest_seen = post.created_at

                # Page is newest-first, so the last item is the oldest on
                # this page and becomes the next max_id cursor.
                cursor = page[-1].id
                store.set_state(STATE_KEY, cursor)

                if pages % 10 == 0:
                    print(
                        f"progress: pages={pages} posts={total_posts} "
                        f"oldest_reached={oldest_seen.isoformat() if oldest_seen else 'n/a'}"
                    )

                if oldest_seen is not None and oldest_seen < cutoff:
                    print(
                        f"reached cutoff: oldest post {oldest_seen.isoformat()} "
                        f"is older than {cutoff.isoformat()}"
                    )
                    break

                time.sleep(args.delay)
        except KeyboardInterrupt:
            out_file.flush()
            print(
                f"interrupted: pages={pages} posts={total_posts} "
                f"oldest_reached={oldest_seen.isoformat() if oldest_seen else 'n/a'}"
            )
            return 0
        except PermanentSourceError as exc:
            out_file.flush()
            print(f"stopping on permanent source error: {exc}")
            return 0

        out_file.flush()

    print(
        f"done: pages={pages} posts={total_posts} "
        f"oldest_reached={oldest_seen.isoformat() if oldest_seen else 'n/a'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
