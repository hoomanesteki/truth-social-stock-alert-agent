from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable

from tsalert.alerts.base import AlertChannel, DeliveryResult, format_alert, format_ops_alert
from tsalert.models import Detection, Post, TickerMention
from tsalert.sources.base import PermanentSourceError, TransientSourceError
from tsalert.store import Store

logger = logging.getLogger(__name__)

_MAX_BACKOFF_SECONDS = 30.0
_BACKOFF_JITTER = 0.2


class AlertDispatcher:
    """Sends alerts through every configured channel, exactly once per post
    per channel, and records what happened so a retry never duplicates a
    delivery.
    """

    def __init__(
        self,
        channels: list[AlertChannel],
        store: Store,
        max_attempts: int = 4,
        sentiment_scorer: Any = None,
        base_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.channels = channels
        self.store = store
        self.max_attempts = max_attempts
        self.sentiment_scorer = sentiment_scorer
        self.base_delay = base_delay
        self.sleep = sleep
        self._rng = random.Random()

    def dispatch(self, post: Post, detection: Detection) -> list[DeliveryResult]:
        detected_at = datetime.now(timezone.utc)
        text = self._format(post, detection)
        results = []
        for channel in self.channels:
            if not channel.is_configured():
                continue
            # claim_alert is the idempotency gate: the row it inserts
            # survives a process restart because it lives in the sqlite
            # file rather than in memory. A second dispatch for the same post and
            # channel finds the row already there and skips silently.
            if not self.store.claim_alert(post.id, channel.name):
                continue
            results.append(self._send_and_record(post, channel, text, detected_at))
        return results

    def dispatch_ops(self, alarm_name: str, detail: str) -> list[DeliveryResult]:
        text = format_ops_alert(alarm_name, detail)
        results = []
        for channel in self.channels:
            if not channel.is_configured():
                continue
            # No claim_alert here on purpose: an ops alarm (source down,
            # heartbeat stale) can legitimately fire again later and each
            # occurrence should be delivered, not swallowed by a claim row
            # left over from the first time it fired.
            ok, attempts, error = self._send_with_retries(channel, text)
            results.append(
                DeliveryResult(
                    channel=channel.name,
                    post_id="",
                    ok=ok,
                    attempts=attempts,
                    error=error,
                    delivered_at=datetime.now(timezone.utc) if ok else None,
                )
            )
        return results

    def retry_failed(self) -> list[DeliveryResult]:
        """Re-attempt alerts stuck at status 'failed' or 'pending'.

        claim_alert only guards the first send: once that row exists, no
        future dispatch() call for the same post and channel can get past
        step 1, retries included. Without this method a post that failed
        every attempt (channel down, transient network trouble) would stay
        stuck at status='failed' forever, since nothing else ever revisits
        it. A 'pending' row means claim_alert ran and then the process died
        before record_alert_result ever did, which is exactly a crash
        mid-send, so it belongs in the same retry set. Meant to be called
        at the start of each poll cycle.
        """
        results = []
        for post_id, channel_name in self.store.retryable_alerts(self.max_attempts):
            channel = self._find_channel(channel_name)
            if channel is None or not channel.is_configured():
                continue
            loaded = self.store.get_post_with_detection(post_id)
            if loaded is None:
                continue
            post, detection = loaded
            text = self._format(post, detection)
            detected_at = datetime.now(timezone.utc)
            results.append(self._send_and_record(post, channel, text, detected_at))
        return results

    def recover_undelivered(self) -> list[DeliveryResult]:
        """Deliver stock related posts that have no alerts row at all.

        This is the other half of restart safety, separate from
        retry_failed: retry_failed revisits alerts that were at least
        claimed (status 'failed' or 'pending'). This revisits posts where
        the process died before claim_alert was ever reached, so dedup
        (upsert_post, permanent and global) already knows the post but
        delivery (per channel, independently fallible) has no record of it
        at all. Bounded per channel by undelivered_stock_posts' limit so a
        large backlog cannot stall a poll cycle.
        """
        results = []
        for channel in self.channels:
            if not channel.is_configured():
                continue
            for post_id in self.store.undelivered_stock_posts(channel.name):
                loaded = self.store.get_post_with_detection(post_id)
                if loaded is None:
                    continue
                post, detection = loaded
                results.extend(self.dispatch(post, detection))
        return results


    def _format(self, post: Post, detection: Detection) -> str:
        """Render an alert, adding sentiment when a scorer is configured.

        Scoring calls a remote model, so a failure here must not cost us the
        alert. Losing the annotation is survivable, losing the alert is not.
        """
        sentiment = None
        if self.sentiment_scorer is not None:
            try:
                sentiment = self.sentiment_scorer.score(
                    post.detection_text, [m.ticker for m in detection.mentions]
                )
            except Exception as exc:
                logger.warning("sentiment scoring failed, sending without it: %s", exc)
        return format_alert(post, detection, sentiment=sentiment)

    # -- internals -----------------------------------------------------

    def _find_channel(self, name: str) -> AlertChannel | None:
        for channel in self.channels:
            if channel.name == name:
                return channel
        return None


    def _send_and_record(
        self, post: Post, channel: AlertChannel, text: str, detected_at: datetime
    ) -> DeliveryResult:
        ok, attempts, error = self._send_with_retries(channel, text)
        status = "delivered" if ok else "failed"
        # attempts is the real number of sends _send_with_retries performed
        # this call, not 1. Recording anything else would let the stored
        # counter undercount, which is what let attempts < max_attempts
        # bound poll cycles instead of sends.
        self.store.record_alert_result(
            post.id, channel.name, status, error or None, attempts_made=attempts
        )
        delivered_at = None
        if ok:
            delivered_at = datetime.now(timezone.utc)
            self.store.record_latency(
                post.id,
                published_at=post.created_at.isoformat(),
                fetched_at=post.fetched_at.isoformat(),
                detected_at=detected_at.isoformat(),
                delivered_at=delivered_at.isoformat(),
            )
        return DeliveryResult(
            channel=channel.name,
            post_id=post.id,
            ok=ok,
            attempts=attempts,
            error=error,
            delivered_at=delivered_at,
        )

    def _send_with_retries(self, channel: AlertChannel, text: str) -> tuple[bool, int, str]:
        error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                channel.send(text)
                return True, attempt, ""
            except PermanentSourceError as exc:
                return False, attempt, str(exc)
            except TransientSourceError as exc:
                error = str(exc)
                if attempt >= self.max_attempts:
                    return False, attempt, error
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = float(retry_after)
                else:
                    cap = min(_MAX_BACKOFF_SECONDS, self.base_delay * (2 ** (attempt - 1)))
                    delay = self._rng.uniform(cap * _BACKOFF_JITTER, cap)
                self.sleep(delay)
        return False, self.max_attempts, error  # pragma: no cover
