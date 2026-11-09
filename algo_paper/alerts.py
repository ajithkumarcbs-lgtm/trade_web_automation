"""Telegram alerts for the paper algo, through a bot of its own (@BotFather), never the trading bot's login:
that login lives on the server and must stay in one place.

.env (on this PC):  ALGO_BOT_TOKEN=<the token BotFather gave>   ALGO_CHAT_ID=<your chat id with that bot>
`python -m algo_paper.alerts --chat-id` prints the chat id once you have pressed Start in the bot's chat.
With no token nothing is sent.
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
log = logging.getLogger("algo")


def _env() -> dict[str, str]:
    values = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("ALGO_") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def _call(token: str, method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Alerts:
    def __init__(self) -> None:
        env = _env()
        self.token, self.chat = env.get("ALGO_BOT_TOKEN", ""), env.get("ALGO_CHAT_ID", "")
        self.queue: list[str] = []

    @property
    def on(self) -> bool:
        return bool(self.token and self.chat)

    def add(self, text: str) -> None:
        if self.on:
            self.queue.append(text)

    def flush(self) -> None:
        """Sends what is queued (called from a worker thread); a failed alert never stops the algo."""
        while self.queue:
            text = self.queue.pop(0)
            try:
                _call(self.token, "sendMessage", {"chat_id": self.chat, "text": text[:4000]})
            except Exception as exc:
                log.warning("alert not sent: %s", str(exc).splitlines()[0][:120])
                return


def main() -> None:
    env = _env()
    token = env.get("ALGO_BOT_TOKEN", "")
    if not token:
        sys.exit("ALGO_BOT_TOKEN is not in .env")
    if "--chat-id" in sys.argv:
        updates = _call(token, "getUpdates", {}).get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "") for u in updates if "message" in u}
        print(chats or "no message yet: open the bot in Telegram and press Start, then run this again")
    elif "--test" in sys.argv:
        print(_call(token, "sendMessage", {"chat_id": env.get("ALGO_CHAT_ID", ""),
                                            "text": "📐 Algo paper test: alerts work (no real money)"}).get("ok"))


if __name__ == "__main__":
    main()
