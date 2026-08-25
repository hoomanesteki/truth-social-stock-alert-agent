from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from tsalert.config import Config
from tsalert.logging_setup import setup_logging
from tsalert.store import Store

_REDACTED_FIELDS = {"telegram_bot_token", "groq_api_key"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Truth Social stock alert agent")
    parser.add_argument("--source", default=None, help="override the configured source")
    parser.add_argument("--env-file", default=".env", help="path to a .env file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.from_env(args.env_file)
    if args.source:
        config = dataclasses.replace(config, source=args.source)

    setup_logging(config.log_level, config.log_file)

    print("Resolved config:")
    for name, value in dataclasses.asdict(config).items():
        if name in _REDACTED_FIELDS and value:
            value = "***"
        print(f"  {name}: {value}")

    with Store(config.db_path) as store:
        print("Store stats:")
        for name, value in store.stats().items():
            print(f"  {name}: {value}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
