from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from tsalert.alerts.base import format_alert
from tsalert.alerts.console import ConsoleChannel
from tsalert.alerts.discord import DiscordChannel
from tsalert.alerts.file_sink import FileChannel
from tsalert.alerts.dispatcher import AlertDispatcher
from tsalert.alerts.telegram import TelegramChannel
from tsalert.config import Config
from tsalert.detect.combined import CombinedDetector
from tsalert.detect.lexicon import Lexicon
from tsalert.detect.llm_detector import LlmDetector
from tsalert.detect.rules import RuleDetector
from tsalert.llm import GroqClient
from tsalert.logging_setup import setup_logging
from tsalert.monitor import HealthMonitor
from tsalert.reliability import AdaptiveInterval
from tsalert.runner import AgentRunner
from tsalert.sentiment import Sentiment, SentimentScorer
from tsalert.sources.failover import FailoverSource
from tsalert.sources.fixture import FixtureSource
from tsalert.sources.rss_mirror import TrumpsTruthRssSource
from tsalert.sources.truthsocial import TruthSocialApiSource
from tsalert.store import Store

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent
_LEXICON_PATH = _REPO_ROOT / "data" / "lexicon" / "tickers.csv"
_LLM_CACHE_PATH = _REPO_ROOT / "data" / "llm_detector_cache.jsonl"
# The detector's own model must differ from whatever model produced
# data/eval/prelabels.jsonl (gpt-oss-120b), or the eval comparison would be
# grading the LLM arm against labels from its own lineage.
_DEFAULT_LLM_MODEL = "qwen/qwen3.6-27b"
_DETECTOR_CHOICES = ("rules", "llm", "combined")
# The offline demo replays the same recorded pages the test suite uses.
# fixture.py's own docstring calls this out: "used by tests and --source fixture".
_FIXTURE_PATHS = [
    _REPO_ROOT / "tests" / "fixtures" / "statuses_page1.json",
    _REPO_ROOT / "tests" / "fixtures" / "statuses_page2.json",
]
# The recorded pages above hold one stock mention, an S&P Global post that only
# started matching once the lookup table grew to cover the S&P 500. One alert out
# of twenty posts is thin for a demo, so the demo file is a denser set of real
# archive posts, four mentions and four lookalikes, which shows both halves of
# the decision with no network and no credentials.
_DEMO_PATHS = [_REPO_ROOT / "tests" / "fixtures" / "demo_statuses.json"]

_REDACTED_FIELDS = {"telegram_bot_token", "groq_api_key", "discord_webhook_url"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Truth Social stock alert agent")
    parser.add_argument("--env-file", default=".env", help="path to a .env file")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="start the monitor loop")
    run_parser.add_argument("--once", action="store_true", help="a single poll then exit")
    run_parser.add_argument(
        "--source",
        choices=["truthsocial", "rss", "fixture", "demo"],
        default=None,
        help="override the configured source",
    )
    run_parser.add_argument("--max-iterations", type=int, default=None, help="bound the loop")
    run_parser.add_argument(
        "--detector",
        choices=_DETECTOR_CHOICES,
        default=None,
        help="override the detector arm: rules, llm, or combined "
        "(default: combined if GROQ_API_KEY is set, otherwise rules)",
    )

    sub.add_parser("test-alert", help="send one sample alert through every configured channel")
    sub.add_parser("health", help="print current health state and any active alarms")
    sub.add_parser("stats", help="print store counts")

    return parser.parse_args(argv)


def _resolve_source_name(config: Config, override: str | None) -> str:
    if override is not None:
        return override
    if config.source in ("fixture", "demo"):
        return config.source
    if config.source in ("rss", "trumpstruth_rss"):
        return "rss"
    return "truthsocial"


def build_source(source_name: str, config: Config):
    if source_name == "fixture":
        return FixtureSource(_FIXTURE_PATHS)
    if source_name == "demo":
        return FixtureSource(_DEMO_PATHS, source_name="demo")
    if source_name == "rss":
        return TrumpsTruthRssSource(account=config.account, timeout=config.request_timeout)
    # Default: the live API as primary, the RSS mirror as a degraded fallback,
    # switched automatically by FailoverSource's circuit breaker.
    primary = TruthSocialApiSource(
        account_id=config.account_id,
        impersonate=config.impersonate,
        timeout=config.request_timeout,
    )
    fallback = TrumpsTruthRssSource(account=config.account, timeout=config.request_timeout)
    return FailoverSource(primary, fallback)


def build_channels(config: Config) -> list:
    """Channels in delivery order, cheapest and most reliable first.

    Order matters because the dispatcher walks this list per post and a
    channel that is down costs its timeout before the next one is tried. File
    and console never touch the network, so putting them first means a remote
    channel being unreachable delays the remote alert and nothing else.

    "Configured" throughout means the credential fields are non empty, not
    that they work. A revoked Telegram token and a deleted Discord webhook
    both look configured and fail on send, which is the dispatcher's problem
    and is isolated per channel.
    """
    # Never fails, needs nothing, and keeps the record when every remote
    # channel is down. First in the list for exactly that reason.
    channels = [FileChannel(config.alerts_file), ConsoleChannel()]
    discord = DiscordChannel(config.discord_webhook_url, timeout=config.request_timeout)
    if discord.is_configured():
        channels.append(discord)
    # REQUEST_TIMEOUT reached the source but not the channel, so the Telegram
    # timeout was stuck at its own default however it was configured. That is
    # the number that sets what an unreachable channel costs: four attempts at
    # 20 seconds is about 85 seconds of a poll spent learning nothing.
    telegram = TelegramChannel(
        config.telegram_bot_token, config.telegram_chat_id,
        timeout=config.request_timeout,
    )
    if telegram.is_configured():
        channels.append(telegram)
    return channels


def _resolve_detector_name(config: Config, override: str | None) -> str:
    if override is not None:
        return override
    return "combined" if config.groq_api_key else "rules"


def build_detector(config: Config, detector_name: str | None = None):
    lexicon = Lexicon.load(_LEXICON_PATH)
    name = _resolve_detector_name(config, detector_name)
    rules = RuleDetector(lexicon)

    if name == "rules":
        return rules

    if not config.groq_api_key:
        raise ValueError(f"--detector {name} requires GROQ_API_KEY to be set")

    llm = LlmDetector(
        GroqClient(
            api_key=config.groq_api_key,
            model=config.groq_model or _DEFAULT_LLM_MODEL,
            cache_path=_LLM_CACHE_PATH,
        ),
        lexicon=lexicon,
    )
    if name == "llm":
        return llm

    # combined: rules run first since they are free, the LLM is only
    # consulted on rule candidates. See detect/combined.py for the cascade.
    return CombinedDetector(rules, llm)


def build_sentiment_scorer(config: Config) -> SentimentScorer | None:
    if not config.groq_api_key:
        return None
    client = GroqClient(
        api_key=config.groq_api_key,
        model=config.groq_model or _DEFAULT_LLM_MODEL,
        cache_path=_LLM_CACHE_PATH,
    )
    return SentimentScorer(client)




def print_active_channels(channels: list, config: Config | None = None) -> None:
    names = ", ".join(c.name for c in channels)
    print(f"Active channels: {names}")
    if config is None:
        return
    # Say why a channel is missing rather than just leaving it off the list.
    # load_dotenv does not override variables already in the environment, so
    # an empty TELEGRAM_BOT_TOKEN exported in the shell silently beats a good
    # value in .env, and the only symptom is a channel quietly not being
    # there. That is the documented way to turn Telegram off, so the fix is
    # to make it visible, not to override it.
    live = {c.name for c in channels}
    if "discord" not in live and not config.discord_webhook_url:
        print("  discord is off: DISCORD_WEBHOOK_URL empty. This is the primary "
              "remote channel, see the README for the 30 second setup.")
    if "telegram" not in live:
        missing = [
            name
            for name, value in (("TELEGRAM_BOT_TOKEN", config.telegram_bot_token),
                                ("TELEGRAM_CHAT_ID", config.telegram_chat_id))
            if not value
        ]
        if missing:
            print(f"  telegram is off: {' and '.join(missing)} empty. "
                  f"A value exported in your shell overrides .env, even an empty one.")


def print_config(config: Config) -> None:
    print("Resolved config:")
    for name, value in dataclasses.asdict(config).items():
        if name in _REDACTED_FIELDS and value:
            value = "***"
        print(f"  {name}: {value}")


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    setup_logging(config.log_level, config.log_file)
    source_name = _resolve_source_name(config, args.source)

    detector_name = _resolve_detector_name(config, args.detector)
    try:
        detector = build_detector(config, args.detector)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    scorer = build_sentiment_scorer(config)

    with Store(config.db_path) as store:
        source = build_source(source_name, config)
        channels = build_channels(config)
        dispatcher = AlertDispatcher(channels, store, sentiment_scorer=scorer)
        monitor = HealthMonitor(
            store,
            stale_minutes=config.heartbeat_stale_minutes,
            no_posts_hours=config.no_posts_alarm_hours,
        )
        interval = AdaptiveInterval(
            base=config.poll_interval_seconds,
            max_interval=config.quiet_poll_interval_seconds,
        )
        # A fresh database against a live source would alert on whatever the
        # first page happens to contain, which is a burst of messages about
        # posts the recipient already saw. Replays are exempt: alerting is the
        # whole point of the demo.
        is_replay = source_name in ("fixture", "demo")
        runner = AgentRunner(
            source, detector, dispatcher, store, monitor, interval,
            account=config.account,
            prime_without_alerting=not is_replay,
            time_latency=not is_replay,
            max_alert_age_hours=None if is_replay else config.max_alert_age_hours,
        )

        print(f"Source: {source_name}")
        print(f"Detector: {getattr(detector, 'name', detector_name)}")
        print_active_channels(channels, config)

        max_iterations = 1 if args.once else args.max_iterations
        runner.run(max_iterations=max_iterations)

    return 0


def cmd_test_alert(args: argparse.Namespace, config: Config) -> int:
    with Store(config.db_path) as store:
        scorer = build_sentiment_scorer(config)
        channels = build_channels(config)
        print_active_channels(channels, config)
        dispatcher = AlertDispatcher(channels, store, sentiment_scorer=scorer)
        results = dispatcher.dispatch_ops(
            "test_alert", "This is a test alert sent from the command line."
        )
        for result in results:
            status = "delivered" if result.ok else f"failed: {result.error}"
            print(f"  {result.channel}: {status}")
    return 0


def cmd_health(args: argparse.Namespace, config: Config) -> int:
    with Store(config.db_path) as store:
        monitor = HealthMonitor(
            store,
            stale_minutes=config.heartbeat_stale_minutes,
            no_posts_hours=config.no_posts_alarm_hours,
        )
        status = monitor.status()
        print("Health state:")
        for key, value in status.items():
            print(f"  {key}: {value}")

        # Read only: this command shares a database with the running agent,
        # and check() writes the suppression timestamps. Recording here would
        # mean running `health` silenced the agent's own next alarm for an
        # hour.
        alarms = monitor.check(record=False)
        if not alarms:
            print("No active alarms.")
        else:
            print("Active alarms:")
            for alarm in alarms:
                print(f"  {alarm.name}: {alarm.detail}")
    return 0


def cmd_stats(args: argparse.Namespace, config: Config) -> int:
    with Store(config.db_path) as store:
        print("Store stats:")
        for name, value in store.stats().items():
            print(f"  {name}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.from_env(args.env_file)

    if args.command == "run":
        return cmd_run(args, config)
    if args.command == "test-alert":
        return cmd_test_alert(args, config)
    if args.command == "health":
        return cmd_health(args, config)
    if args.command == "stats":
        return cmd_stats(args, config)

    print_config(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
