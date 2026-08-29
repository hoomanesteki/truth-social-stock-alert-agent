from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(ValueError):
    """A setting is missing or does not make sense."""


def _positive_int(name: str, raw, default: int) -> int:
    """Read a setting that has to be a positive whole number.

    A negative poll interval turns every sleep into zero and the loop spins
    against someone else's server. The throttle and the hourly cap still
    bound that, but a typo in .env should be caught here rather than left
    for a downstream guard to absorb.
    """
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    db_path: str = "data/agent.db"
    account: str = "realDonaldTrump"
    account_id: str = "107780257626128497"
    source: str = "truthsocial_api"
    poll_interval_seconds: int = 30
    quiet_poll_interval_seconds: int = 60
    impersonate: str = "safari17_0"
    request_timeout: int = 20
    log_level: str = "INFO"
    log_file: str = "logs/agent.jsonl"
    # Primary channel. A webhook URL is the whole credential, with no token
    # to revoke and no chat id to discover, which is why this is primary and
    # Telegram is the optional second.
    discord_webhook_url: str = ""
    # Always on. The channel that cannot go down, and the audit trail.
    alerts_file: str = "data/alerts.jsonl"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    groq_api_key: str = ""
    groq_model: str = ""
    heartbeat_stale_minutes: int = 15
    no_posts_alarm_hours: int = 12
    # Nothing older than this ever alerts, whatever the eligibility flag on
    # the row says. A stock alert about a post from last month is not news,
    # and this is the backstop for the ways old posts get into the store:
    # a database written before backfill learned to mark its rows, an
    # import, a restore. A day is generous for a feed meant to alert inside
    # a minute, and still wide enough that the agent can be down overnight
    # and catch up properly when it returns.
    max_alert_age_hours: int = 24

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Config":
        load_dotenv(env_file)
        d = cls()
        return cls(
            db_path=os.environ.get("DB_PATH", d.db_path),
            account=os.environ.get("ACCOUNT", d.account),
            account_id=os.environ.get("ACCOUNT_ID", d.account_id),
            source=os.environ.get("SOURCE", d.source),
            poll_interval_seconds=_positive_int("POLL_INTERVAL_SECONDS", os.environ.get("POLL_INTERVAL_SECONDS"), d.poll_interval_seconds),
            quiet_poll_interval_seconds=_positive_int("QUIET_POLL_INTERVAL_SECONDS", os.environ.get("QUIET_POLL_INTERVAL_SECONDS"), d.quiet_poll_interval_seconds),
            impersonate=os.environ.get("IMPERSONATE", d.impersonate),
            request_timeout=_positive_int("REQUEST_TIMEOUT", os.environ.get("REQUEST_TIMEOUT"), d.request_timeout),
            log_level=os.environ.get("LOG_LEVEL", d.log_level),
            log_file=os.environ.get("LOG_FILE", d.log_file),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", d.telegram_bot_token),
            discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", d.discord_webhook_url),
            alerts_file=os.environ.get("ALERTS_FILE", d.alerts_file),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", d.telegram_chat_id),
            groq_api_key=os.environ.get("GROQ_API_KEY", d.groq_api_key),
            groq_model=os.environ.get("GROQ_MODEL", d.groq_model),
            heartbeat_stale_minutes=_positive_int("HEARTBEAT_STALE_MINUTES", os.environ.get("HEARTBEAT_STALE_MINUTES"), d.heartbeat_stale_minutes),
            no_posts_alarm_hours=_positive_int("NO_POSTS_ALARM_HOURS", os.environ.get("NO_POSTS_ALARM_HOURS"), d.no_posts_alarm_hours),
            max_alert_age_hours=_positive_int("MAX_ALERT_AGE_HOURS", os.environ.get("MAX_ALERT_AGE_HOURS"), d.max_alert_age_hours),
        )
