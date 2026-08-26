from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from tsalert.alerts.dispatcher import AlertDispatcher
from tsalert.monitor import HealthMonitor
from tsalert.reliability import AdaptiveInterval
from tsalert.sources.base import PermanentSourceError, TransientSourceError
from tsalert.store import Store

logger = logging.getLogger(__name__)

_LAST_SEEN_KEY = "last_seen_post_id"


class AgentRunner:
    """Ties a source, detector, dispatcher and health monitor into one loop.

    poll_once is the unit of work; run() is just poll_once on a timer with
    the failure handling that keeps a flaky network or a changed API from
    taking the whole process down.
    """

    def __init__(
        self,
        source: Any,
        detector: Any,
        dispatcher: AlertDispatcher,
        store: Store,
        monitor: HealthMonitor,
        interval: AdaptiveInterval,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.source = source
        self.alerts_sent = 0
        self.detector = detector
        self.dispatcher = dispatcher
        self.store = store
        self.monitor = monitor
        self.interval = interval
        self.sleep = sleep

    def poll_once(self) -> int:
        self.dispatcher.retry_failed()

        since_id = self.store.get_state(_LAST_SEEN_KEY)

        try:
            posts = self.source.fetch_latest(since_id=since_id)
        except TransientSourceError as exc:
            logger.warning("transient source error, will retry next poll: %s", exc)
            self.monitor.record_poll(ok=False, new_posts=0)
            return 0
        except PermanentSourceError as exc:
            logger.error("permanent source error: %s", exc)
            self.monitor.record_poll(ok=False, new_posts=0)
            self.dispatcher.dispatch_ops("permanent_source_error", str(exc))
            return 0

        new_count = 0
        last_id = since_id
        for post in posts:
            if last_id is None or int(post.id) > int(last_id):
                last_id = post.id

            is_new = self.store.upsert_post(post)
            if not is_new:
                continue

            detection = self.detector.detect(post.detection_text, post.id)
            self.store.save_detection(detection)
            # Record ingestion latency for EVERY post, not just the ones that
            # alert. Stock mentions are roughly two percent of posts, so waiting
            # for a delivered alert to sample latency would take days. The
            # publish to fetch stage is the one bounded by the poll interval and
            # it dominates the total, so it is the number worth measuring.
            self.store.record_latency(
                post.id,
                published_at=post.created_at.isoformat(),
                fetched_at=post.fetched_at.isoformat(),
                detected_at=datetime.now(timezone.utc).isoformat(),
            )
            new_count += 1
            if detection.is_stock_related:
                self.dispatcher.dispatch(post, detection)
                self.alerts_sent += 1

        if last_id is not None:
            self.store.set_state(_LAST_SEEN_KEY, last_id)

        self.monitor.record_poll(ok=True, new_posts=new_count)

        for alarm in self.monitor.check():
            self.dispatcher.dispatch_ops(alarm.name, alarm.detail)

        return new_count

    def run(self, max_iterations: int | None = None) -> None:
        iterations = 0
        total_new = 0
        started_alerts = self.alerts_sent
        try:
            while max_iterations is None or iterations < max_iterations:
                new_count = self.poll_once()
                iterations += 1
                total_new += new_count
                # Always say what happened. A silent agent that is working looks
                # identical to a broken one, which makes a demo impossible to read.
                print(
                    f"poll {iterations}: {new_count} new post(s), "
                    f"{self.alerts_sent - started_alerts} alert(s) sent"
                )

                reached_limit = max_iterations is not None and iterations >= max_iterations
                if reached_limit:
                    break
                # No point sleeping after the final bounded iteration, since
                # the caller (--once, tests) is about to exit anyway.
                self.sleep(self.interval.next_delay(new_count > 0))
        except KeyboardInterrupt:
            pass
        print(
            f"stopped after {iterations} poll(s), {total_new} new post(s), "
            f"{self.alerts_sent - started_alerts} alert(s) sent"
        )
