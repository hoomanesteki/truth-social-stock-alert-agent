#!/usr/bin/env python3
"""Find the chat id for your bot and write it into .env.

Telegram will not let a bot open a conversation, so you have to message it
first. Send anything to your bot, then run this.

    uv run python scripts/setup_telegram.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from curl_cffi import requests
from dotenv import dotenv_values

API = "https://api.telegram.org"

UNREACHABLE = f"""cannot reach {API}.

The name resolves but the connection times out, which is what a network level
block looks like. Telegram is blocked outright in some countries and on plenty
of corporate and campus networks. Nothing here is wrong with the token or the
code, the packets are not getting out.

Options:
  - run this on a network that allows Telegram, or through a VPN
  - skip Telegram entirely. The console channel needs no network at all:
        uv run python agent.py run --once --source demo
    and the agent keeps working with Telegram unset."""


def call(token: str, method: str):
    """One Bot API call. Network failures are reported, not raised.

    Returns (status, payload). Status is None when the request never got a
    reply, which the caller reports as unreachable rather than as a bad token.
    """
    try:
        r = requests.get(f"{API}/bot{token}/{method}", impersonate="safari17_0", timeout=20)
    except requests.errors.RequestsError as exc:
        return None, {"error": str(exc)}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": r.text[:200]}


def write_chat_id(env_path: Path, chat_id: str) -> None:
    text = env_path.read_text()
    line = f"TELEGRAM_CHAT_ID={chat_id}"
    if re.search(r"^TELEGRAM_CHAT_ID=.*$", text, flags=re.M):
        text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    env_path.write_text(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(REPO / ".env"))
    ap.add_argument("--no-test", action="store_true", help="skip the test message")
    args = ap.parse_args()

    env_path = Path(args.env)
    token = (dotenv_values(env_path).get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env. Get one from @BotFather first.")
        return 1

    status, data = call(token, "getMe")
    if status is None:
        print(UNREACHABLE)
        return 1
    if status != 200 or not data.get("ok"):
        print(f"the token was rejected: {data}")
        return 1
    username = data["result"].get("username")
    print(f"bot: @{username}")

    status, data = call(token, "getUpdates")
    if status is None:
        print(UNREACHABLE)
        return 1
    chats = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat

    if not chats:
        print("\nNo messages yet, so there is no chat to send to.")
        print(f"Open Telegram, search for @{username}, send it any message, then run this again.")
        return 1

    if len(chats) > 1:
        print("\nmore than one chat has messaged this bot:")
        for cid, chat in chats.items():
            print(f"  {cid}  {chat.get('first_name') or chat.get('title')}")
        print("using the most recent one")
    chat_id = str(list(chats)[-1])
    who = chats[int(chat_id)].get("first_name") or chats[int(chat_id)].get("title")
    print(f"chat id: {chat_id}  ({who})")

    write_chat_id(env_path, chat_id)
    print(f"written to {env_path}")

    if args.no_test:
        return 0
    try:
        r = requests.post(
            f"{API}/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": "Alert agent connected. This is a test message.",
                  "disable_web_page_preview": True},
            impersonate="safari17_0", timeout=20)
    except requests.errors.RequestsError as exc:
        print(f"the chat id is saved, but the test message did not go out: {exc}")
        return 1
    try:
        ok = r.status_code == 200 and r.json().get("ok")
    except ValueError:
        ok = False
    if ok:
        print("test message sent, check Telegram")
        return 0
    print(f"test message failed: {r.text[:200]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
