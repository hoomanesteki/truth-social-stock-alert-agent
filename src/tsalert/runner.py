from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from tsalert.alerts.dispatcher import AlertDispatcher
from tsalert.models import Post
from tsalert.monitor import HealthMonitor
from tsalert.reliability import AdaptiveInterval, with_retries
from tsalert.sources.base import PermanentSourceError, TransientSourceError, id_sort_key
from tsalert.store import Store

logger = logging.getLogger(__name__)

_LAST_SEEN_KEY = "last_seen_post_id"


def _last_seen_key(account: str) -> str:
    """Namespace the polling cursor per account.

    One shared key works fine for a single account and silently breaks the
    moment there are two: whichever polled last overwrites the other's high
    water mark, and the other one skips everything in between.
    """
    return f"{_LAST_SEEN_KEY}:{account}" if account else _LAST_SEEN_KEY
_UNDETECTED_BACKLOG_LIMIT = 50


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
        account: str = "",
        prime_without_alerting: bool = False,
        time_latency: bool = True,
        max_alert_age_hours: int | None = None,
    ) -> None:
        self.account = account
        # Off for the replay sources. Their posts carry the timestamps they
        # really had, days or weeks back, so stamping publish to fetch on
        # them records the age of the archive. Anyone who ran the demo and
        # then the latency report saw a p50 of about two days and would
        # reasonably conclude the agent was broken.
        self.time_latency = time_latency
        # Age backstop for alerting, None to disable. Eligibility is a flag
        # on the row, and a flag is only as good as whatever wrote it: a
        # store built before backfill learned to mark its rows leaves the
        # whole archive looking alertable, and the backlog pass then works
        # through it a batch at a time, announcing month old news as if it
        # just happened. The post's own timestamp cannot be got wrong the
        # same way. Off by default and off for the replay sources, whose
        # whole purpose is to alert on posts that are deliberately old.
        self.max_alert_age = (
            timedelta(hours=max_alert_age_hours) if max_alert_age_hours else None
        )
        # A brand new database has an empty alerts table, so every post the
        # first poll happens to return looks unsent and gets delivered. That
        # is how you end up messaging someone twenty times for posts they
        # already know about. When this is set, the first poll records what
        # it sees and moves the cursor without alerting.
        self.prime_without_alerting = prime_without_alerting
        self.source = source
        self.alerts_sent = 0
        # Set when a source asks us to back off for longer than the retry
        # budget covers. The run loop uses it instead of the usual interval.
        self.next_delay_override: float | None = None
        self.detector = detector
        self.dispatcher = dispatcher
        self.store = store
        self.monitor = monitor
        self.interval = interval
        self.sleep = sleep

    def poll_once(self) -> int:
        self.dispatcher.retry_failed()
        # dedup (upsert_post/is_new) and delivery (claim_alert/alerts rows)
        # are separate concerns on separate schedules: upsert_post answers
        # "have I seen this post before", which is permanent and global,
        # while delivery is per channel and can fail independently, even
        # before a claim row is ever written (a crash between save_detection
        # and dispatch). retry_failed above recovers claims stuck mid-send;
        # this recovers posts dedup already knows about that were never
        # claimed for delivery at all, which used to mean they were lost
        # forever the moment upsert_post's is_new gate turned False.
        self.dispatcher.recover_undelivered()
        # A crash between upsert_post and save_detection leaves detected_at
        # NULL forever, since upsert_post's is_new gate then hides the post
        # from every future poll. Runs before fetching new posts so a
        # backlog from a prior crash does not get pushed further behind by
        # this poll's own new posts.
        for post in self.store.undetected_posts(limit=_UNDETECTED_BACKLOG_LIMIT):
            self._detect_and_dispatch(post, time_it=False)

        since_id = self._read_since_id()

        try:
            # A single transient blip (one dropped connection, one 5xx) is
            # worth a few seconds of retry here rather than costing a whole
            # poll interval, which can be minutes under AdaptiveInterval.
            posts = with_retries(
                lambda: self.source.fetch_latest(since_id=since_id),
                sleep=self.sleep,
            )
        except TransientSourceError as exc:
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                # A 429 that outlasted the retry budget. Waiting the adaptive
                # interval here would ignore what the server actually asked
                # for and go straight back to hammering it.
                self.next_delay_override = float(retry_after)
                logger.warning(
                    "source asked for %ss before the next request: %s", retry_after, exc
                )
            else:
                logger.warning("transient source error, will retry next poll: %s", exc)
            self._record_poll(ok=False, new_posts=0)
            return 0
        except PermanentSourceError as exc:
            logger.error("permanent source error: %s", exc)
            self._record_poll(ok=False, new_posts=0)
            self.dispatcher.dispatch_ops("permanent_source_error", str(exc))
            return 0

        new_count = 0
        last_id = since_id
        priming = self.prime_without_alerting and since_id is None
        if priming:
            logger.info(
                "first poll on an empty store, recording %d post(s) without alerting",
                len(posts),
            )
        try:
            for post in posts:
                if last_id is None or id_sort_key(post.id) > id_sort_key(last_id):
                    last_id = post.id

                is_new = self.store.upsert_post(post, alert_eligible=not priming)
                if not is_new:
                    continue

                # Priming is the deliberate skip past history on first
                # start. Those posts are days or weeks old, so timing them
                # measures the age of the archive, not how fast we are, and
                # publish to fetch is the one stage the latency report leads
                # with. The backlog pass below already learned this lesson.
                self._detect_and_dispatch(post, alert=not priming, time_it=not priming)
                new_count += 1
        except (ValueError, TypeError) as exc:
            # An unexpected id shape or malformed post must never kill the
            # loop. id_sort_key already makes comparisons safe, but this is
            # the backstop for anything else in the batch (detector,
            # store) that assumes well formed data.
            logger.error("unexpected data shape while processing a batch: %s", exc)
            self._record_poll(ok=False, new_posts=0)
            return 0

        if last_id is not None:
            self.store.set_state(_last_seen_key(self.account), last_id)

        self._record_poll(ok=True, new_posts=new_count)

        return new_count

    def _read_since_id(self) -> str | None:
        """Read the polling cursor, falling back to the pre namespace key.

        Namespacing the key orphaned the cursor in any database written
        before the change, and the agent quietly restarted from the top of
        the timeline. Read the old key once and carry it forward.
        """
        key = _last_seen_key(self.account)
        value = self.store.get_state(key)
        if value is not None or key == _LAST_SEEN_KEY:
            return value
        legacy = self.store.get_state(_LAST_SEEN_KEY)
        if legacy is not None:
            self.store.set_state(key, legacy)
        return legacy

    def _record_poll(self, ok: bool, new_posts: int) -> None:
        """Record the poll and raise whatever alarms it triggered.

        This has to run on the failure paths too. Checking only after a
        successful poll meant a run of transient errors raised the counter,
        never asked the monitor about it, and then the next success reset the
        counter, so repeated_errors could not fire on the one thing it exists
        to catch.
        """
        self._record_source_state()
        self.monitor.record_poll(ok=ok, new_posts=new_posts)
        for alarm in self.monitor.check():
            self.dispatcher.dispatch_ops(alarm.name, alarm.detail)

    def _record_source_state(self) -> None:
        """Persist which source is live, so the dashboard can show it.

        The failover object lives in the agent process and the dashboard is a
        separate one reading the same database, so without this the page had
        no way to tell whether posts were arriving from the primary or from
        the mirror. That is the single most useful thing to know when the
        ingestion is the fragile part, and it was the one thing the page did
        not show.
        """
        active = getattr(self.source, "active_source_name", None) or getattr(
            self.source, "name", ""
        )
        self.store.set_state("active_source", active)
        transition = getattr(self.source, "last_transition", None)
        if transition is not None:
            self.store.set_state(
                "last_source_transition",
                json.dumps({
                    "at": transition.at.isoformat(),
                    "from": transition.from_source,
                    "to": transition.to_source,
                    "reason": transition.reason,
                }),
            )
        try:
            health = self.source.health()
        except Exception:  # a health probe must never break a poll
            return
        self.store.set_state("source_detail", str(getattr(health, "detail", "")))
        self.store.set_state("source_ok", "1" if getattr(health, "ok", False) else "0")

    def _worth_alerting(self, post: Post) -> bool:
        """Both gates an alert has to pass: eligible, and recent enough."""
        if not self.store.is_alert_eligible(post.id):
            return False
        if self.max_alert_age is None:
            return True
        age = datetime.now(timezone.utc) - post.created_at
        if age > self.max_alert_age:
            logger.info("not alerting on %s, it is %s old", post.id, age)
            return False
        return True

    def _detect_and_dispatch(self, post: Post, *, time_it: bool = True,
                             alert: bool = True) -> None:
        """Run one post through the detector, persist it, dispatch if it hits.

        Shared by the new-post loop and the undetected-backlog recovery pass
        above, so a post recovered after a crash goes through exactly the
        same detect/save/dispatch steps a freshly fetched one does.

        time_it is False for the backlog pass and for priming. Those posts
        were ingested by an earlier run, by the backfill script, or are the
        history we deliberately skip past on first start, so measuring
        publish to fetch on them reports how old the archive is, and a
        handful of month old posts drown every real reading in the table.
        """
        try:
            detection = self.detector.detect(post.detection_text, post.id)
        except Exception as exc:
            # One post must not be able to stop the agent. --detector llm
            # builds a bare LlmDetector, which propagates GroqError on
            # purpose so CombinedDetector can decide what to do about it,
            # and with nothing catching it here a single bad Groq response
            # killed the run. Worse, the post was already stored with
            # detected_at NULL, so the undetected backlog pass replayed it
            # on the next start and died in the same place, before fetching
            # anything. Leaving it undetected is the right outcome: the
            # backlog pass retries it, bounded per poll, and a detector that
            # recovers picks it up with nothing lost.
            logger.error("detector failed on post %s, leaving it for the backlog: %s",
                         post.id, exc)
            return
        self.store.save_detection(detection)
        # Latency gets stamped on every post, including the ones that never
        # alert. Stock mentions run around two percent, so building a sample
        # only from delivered alerts would take days. Publish to fetch is the
        # stage bounded by the poll interval and it dominates the total, so
        # that is the number worth having.
        if time_it and self.time_latency:
            self.store.record_latency(
                post.id,
                published_at=post.created_at.isoformat(),
                fetched_at=post.fetched_at.isoformat(),
                detected_at=datetime.now(timezone.utc).isoformat(),
            )
        # Eligibility is checked here rather than at the call sites because
        # the undetected backlog path also lands in this method, and that is
        # how backfilled history used to reach the dispatcher.
        if detection.is_stock_related and alert and self._worth_alerting(post):
            # The dispatcher stamps latency too, on delivery, so it needs the
            # same replay gate. Otherwise the one post that alerts still
            # lands an archive-age row in the table.
            self.dispatcher.dispatch(post, detection, time_it=time_it and self.time_latency)
            self.alerts_sent += 1

    def run(self, max_iterations: int | None = None) -> None:
        iterations = 0
        total_new = 0
        started_alerts = self.alerts_sent
        try:
            while max_iterations is None or iterations < max_iterations:
                new_count = self.poll_once()
                iterations += 1
                total_new += new_count
                # Print every poll. Otherwise there is no way to tell a working agent
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
                delay = self.interval.next_delay(new_count > 0)
                if self.next_delay_override is not None:
                    delay = max(delay, self.next_delay_override)
                    self.next_delay_override = None
                self.sleep(delay)
        except KeyboardInterrupt:
            pass
        print(
            f"stopped after {iterations} poll(s), {total_new} new post(s), "
            f"{self.alerts_sent - started_alerts} alert(s) sent"
        )
