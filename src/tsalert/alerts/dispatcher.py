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
    """Sends alerts through every configured channel, at least once per post
    per channel, and records what happened so a retry never duplicates a
    delivery.
    """

    def __init__(
        self,
        channels: list[AlertChannel],
        store: Store,
        max_attempts: int = 4,
        max_cycles: int = 5,
        sentiment_scorer: Any = None,
        base_delay: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.channels = channels
        self.store = store
        # max_attempts bounds sends within a single dispatch/retry call (the
        # per-call budget _send_with_retries spends on backoff). max_cycles
        # bounds how many separate retry_failed passes a transient failure
        # gets over its lifetime. Conflating the two is what let a permanent
        # failure keep getting retried and a transient one stop being retried
        # the moment one dispatch call used up its per-call budget.
        self.max_attempts = max_attempts
        self.max_cycles = max_cycles
        self.sentiment_scorer = sentiment_scorer
        self.base_delay = base_delay
        self.sleep = sleep
        self._rng = random.Random()
        # Channels that used their whole retry budget and still failed. A
        # channel in here is skipped for the rest of the pass. Cleared at the
        # top of retry_failed, which is the first thing every poll does, so
        # each poll gives a down channel exactly one probe.
        self._down_channels: set[str] = set()

    def dispatch(self, post: Post, detection: Detection,
                 time_it: bool = True) -> list[DeliveryResult]:
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
            results.append(
                self._send_and_record(post, channel, text, detected_at, time_it=time_it)
            )
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
            if channel.name in self._down_channels:
                continue
            ok, attempts, error, permanent = self._send_with_retries(channel, text)
            self._note_channel_health(channel.name, ok, permanent)
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
        every attempt for a transient reason (channel down, network trouble)
        would stay stuck at status='failed' forever, since nothing else ever
        revisits it. A 'pending' row means claim_alert ran and then the
        process died before record_alert_result ever did, which is exactly a
        crash mid-send, so it belongs in the same retry set.

        retryable_alerts already excludes 'permanent_failure' rows by status,
        so a bad token or bad chat id is never retried here, and bounds the
        rest by max_cycles (lifetime retry_failed passes), not max_attempts
        (the per-call send budget). Meant to be called at the start of each
        poll cycle.
        """
        # Every poll starts here, so this is where a channel marked down
        # gets its next chance. Without the reset, one outage would silence
        # the channel until the process restarted.
        self._down_channels.clear()
        results = []
        for post_id, channel_name in self.store.retryable_alerts(self.max_cycles):
            channel = self._find_channel(channel_name)
            if channel is None or not channel.is_configured():
                continue
            loaded = self.store.get_post_with_detection(post_id)
            if loaded is None:
                continue
            post, detection = loaded
            text = self._format(post, detection)
            detected_at = datetime.now(timezone.utc)
            results.append(
                self._send_and_record(post, channel, text, detected_at,
                                      is_retry=True, time_it=False)
            )
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
                # Recovered posts were ingested by an earlier run, so timing
                # them measures how old the backlog is, not how fast we are.
                results.extend(self.dispatch(post, detection, time_it=False))
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
        self,
        post: Post,
        channel: AlertChannel,
        text: str,
        detected_at: datetime,
        is_retry: bool = False,
        time_it: bool = True,
    ) -> DeliveryResult:
        if channel.name in self._down_channels:
            # Nothing is written to the store on purpose. The row keeps
            # whatever status it already had, 'pending' from claim_alert or
            # 'failed' from an earlier try, and retryable_alerts picks both
            # of those up later. Recording a failure here instead would spend
            # one of the alert's max_cycles retries on a send that never
            # happened, so a long outage would exhaust the retry budget of
            # every queued alert without a single request leaving the machine.
            return DeliveryResult(
                channel=channel.name,
                post_id=post.id,
                ok=False,
                attempts=0,
                error="channel is down, left for the next poll",
                delivered_at=None,
            )
        ok, attempts, error, permanent = self._send_with_retries(channel, text)
        self._note_channel_health(channel.name, ok, permanent)
        # A transient failure that took the whole channel down says nothing
        # about this particular alert, so it must not spend a retry cycle.
        # Otherwise the one alert unlucky enough to be the probe would be
        # abandoned after max_cycles polls of an outage while the alerts
        # behind it, skipped and never counted, waited indefinitely. Same
        # outage, opposite outcomes, decided by queue order.
        outage = not ok and not permanent
        if outage:
            is_retry = False
        if ok:
            status = "delivered"
        elif permanent:
            # Never retryable: retryable_alerts filters on status, so this
            # keeps a bad token or bad chat id out of every future retry pass.
            status = "permanent_failure"
        else:
            status = "failed"
        # attempts is the real number of sends _send_with_retries performed
        # this call, not 1. Recording anything else would let the stored
        # counter undercount, which is what let attempts < max_attempts
        # bound poll cycles instead of sends.
        self.store.record_alert_result(
            post.id, channel.name, status, error or None, attempts_made=attempts, is_retry=is_retry
        )
        delivered_at = None
        if ok:
            delivered_at = datetime.now(timezone.utc)
        if ok and time_it:
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

    def _note_channel_health(self, name: str, ok: bool, permanent: bool) -> None:
        """Track whether a channel is merely flaky or actually down.

        _send_with_retries already spends four attempts with backoff on a
        transient failure, so a call that comes back exhausted is not a blip,
        it is an outage. Found this by running with Telegram blocked at the
        network layer: every alert in the poll paid the full budget on its
        own, four timeouts plus backoff each, and one poll that should have
        taken a second took nearly six minutes. Alerts were never lost, but
        the agent fell far enough behind that the latency target was gone.

        A permanent failure does not count. A bad token fails immediately on
        the first attempt, so it costs no time, and it is already kept out of
        every future retry by its status.
        """
        if ok:
            self._down_channels.discard(name)
            return
        if permanent:
            return
        if name not in self._down_channels:
            logger.warning(
                "channel %s failed every attempt, skipping it for the rest of this poll", name
            )
        self._down_channels.add(name)

    def _send_with_retries(self, channel: AlertChannel, text: str) -> tuple[bool, int, str, bool]:
        """Send with in-call backoff. Returns (ok, attempts, error, permanent).

        permanent is True only when the loop ended on a PermanentSourceError,
        which the caller uses to record a status that retry_failed will never
        pick back up, since retrying a 4xx cannot ever succeed.
        """
        error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                channel.send(text)
                return True, attempt, "", False
            except PermanentSourceError as exc:
                return False, attempt, str(exc), True
            except TransientSourceError as exc:
                error = str(exc)
                if attempt >= self.max_attempts:
                    return False, attempt, error, False
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = float(retry_after)
                else:
                    cap = min(_MAX_BACKOFF_SECONDS, self.base_delay * (2 ** (attempt - 1)))
                    delay = self._rng.uniform(cap * _BACKOFF_JITTER, cap)
                self.sleep(delay)
        return False, self.max_attempts, error, False  # pragma: no cover
