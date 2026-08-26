from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from tsalert.models import parse_iso_datetime
from tsalert.store import Store

_LAST_POLL_KEY = "health_last_poll_at"
_LAST_SUCCESS_KEY = "health_last_successful_poll_at"
_FIRST_SUCCESS_KEY = "health_first_successful_poll_at"
_LAST_NEW_POST_KEY = "health_last_new_post_at"
_CONSECUTIVE_ERRORS_KEY = "health_consecutive_errors"
_ALARM_FIRED_PREFIX = "health_alarm_fired_"


@dataclass(frozen=True)
class HealthAlarm:
    name: str
    detail: str


class HealthMonitor:
    """Checks whether the agent is actually working, not just running.

    no_new_posts is the main one. Polling and parsing can both keep
    succeeding while nothing new comes back, usually because the endpoint
    changed shape or started returning empty pages. Nothing crashes, so
    nothing else would catch it.

    State lives in store.agent_state rather than memory. A restart should
    not reset the staleness clock, and alarm suppression has to survive one
    too or it just re-fires the moment the process comes back.
    """

    def __init__(
        self,
        store: Store,
        stale_minutes: int = 15,
        no_posts_hours: int = 12,
        error_threshold: int = 5,
        min_repeat_minutes: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.stale_minutes = stale_minutes
        self.no_posts_hours = no_posts_hours
        self.error_threshold = error_threshold
        self.min_repeat_minutes = min_repeat_minutes
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record_poll(self, ok: bool, new_posts: int) -> None:
        now = self._clock()
        self.store.set_state(_LAST_POLL_KEY, now.isoformat())

        if ok:
            self.store.set_state(_LAST_SUCCESS_KEY, now.isoformat())
            if self.store.get_state(_FIRST_SUCCESS_KEY) is None:
                self.store.set_state(_FIRST_SUCCESS_KEY, now.isoformat())
            consecutive_errors = 0
        else:
            consecutive_errors = self._get_int(_CONSECUTIVE_ERRORS_KEY) + 1
        self.store.set_state(_CONSECUTIVE_ERRORS_KEY, str(consecutive_errors))

        if new_posts > 0:
            self.store.set_state(_LAST_NEW_POST_KEY, now.isoformat())

    def check(self) -> list[HealthAlarm]:
        now = self._clock()
        alarms: list[HealthAlarm] = []

        last_success = self._get_datetime(_LAST_SUCCESS_KEY)
        if last_success is not None:
            age = now - last_success
            if age > timedelta(minutes=self.stale_minutes):
                alarms.append(
                    HealthAlarm(
                        "no_successful_poll",
                        f"last successful poll was {age} ago, threshold is "
                        f"{self.stale_minutes} minutes",
                    )
                )

        # Fall back to the first successful poll when no post has ever been
        # seen, so the staleness clock starts from when the agent began
        # rather than staying unset forever on a fresh store where
        # ingestion is broken from the very first poll (HTTP 200, empty
        # list). Without this fallback that exact case, the one this alarm
        # exists for, never fires.
        last_new_post = self._get_datetime(_LAST_NEW_POST_KEY) or self._get_datetime(_FIRST_SUCCESS_KEY)
        if last_new_post is not None:
            age = now - last_new_post
            if age > timedelta(hours=self.no_posts_hours):
                alarms.append(
                    HealthAlarm(
                        "no_new_posts",
                        f"no new post in {age}, threshold is {self.no_posts_hours} hours. "
                        "polls and parsing may still look healthy, this is the sign an "
                        "endpoint has quietly changed shape.",
                    )
                )

        consecutive_errors = self._get_int(_CONSECUTIVE_ERRORS_KEY)
        if consecutive_errors >= self.error_threshold:
            alarms.append(
                HealthAlarm(
                    "repeated_errors",
                    f"{consecutive_errors} consecutive poll failures, threshold is "
                    f"{self.error_threshold}",
                )
            )

        return [a for a in alarms if self._should_fire(a.name, now)]

    def status(self) -> dict[str, str | int | None]:
        """Raw state for display purposes, outside the interface contract above."""
        return {
            "last_poll_at": self.store.get_state(_LAST_POLL_KEY),
            "last_successful_poll_at": self.store.get_state(_LAST_SUCCESS_KEY),
            "last_new_post_at": self.store.get_state(_LAST_NEW_POST_KEY),
            "consecutive_errors": self._get_int(_CONSECUTIVE_ERRORS_KEY),
        }

    def _should_fire(self, alarm_name: str, now: datetime) -> bool:
        key = _ALARM_FIRED_PREFIX + alarm_name
        last_fired = self._get_datetime(key)
        if last_fired is not None and now - last_fired < timedelta(minutes=self.min_repeat_minutes):
            return False
        self.store.set_state(key, now.isoformat())
        return True

    def _get_datetime(self, key: str) -> datetime | None:
        value = self.store.get_state(key)
        return parse_iso_datetime(value) if value else None

    def _get_int(self, key: str) -> int:
        value = self.store.get_state(key)
        return int(value) if value else 0
