from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    db_path: str = "data/agent.db"
    account: str = "realDonaldTrump"
    account_id: str = "107780257626128497"
    source: str = "truthsocial_api"
    poll_interval_seconds: int = 60
    quiet_poll_interval_seconds: int = 300
    impersonate: str = "safari17_0"
    request_timeout: int = 20
    log_level: str = "INFO"
    log_file: str = "logs/agent.jsonl"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    groq_api_key: str = ""
    groq_model: str = ""
    heartbeat_stale_minutes: int = 15
    no_posts_alarm_hours: int = 12

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Config":
        load_dotenv(env_file)
        d = cls()
        return cls(
            db_path=os.environ.get("DB_PATH", d.db_path),
            account=os.environ.get("ACCOUNT", d.account),
            account_id=os.environ.get("ACCOUNT_ID", d.account_id),
            source=os.environ.get("SOURCE", d.source),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", d.poll_interval_seconds)),
            quiet_poll_interval_seconds=int(
                os.environ.get("QUIET_POLL_INTERVAL_SECONDS", d.quiet_poll_interval_seconds)
            ),
            impersonate=os.environ.get("IMPERSONATE", d.impersonate),
            request_timeout=int(os.environ.get("REQUEST_TIMEOUT", d.request_timeout)),
            log_level=os.environ.get("LOG_LEVEL", d.log_level),
            log_file=os.environ.get("LOG_FILE", d.log_file),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", d.telegram_bot_token),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", d.telegram_chat_id),
            groq_api_key=os.environ.get("GROQ_API_KEY", d.groq_api_key),
            groq_model=os.environ.get("GROQ_MODEL", d.groq_model),
            heartbeat_stale_minutes=int(
                os.environ.get("HEARTBEAT_STALE_MINUTES", d.heartbeat_stale_minutes)
            ),
            no_posts_alarm_hours=int(os.environ.get("NO_POSTS_ALARM_HOURS", d.no_posts_alarm_hours)),
        )
