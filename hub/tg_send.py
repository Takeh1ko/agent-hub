"""Временный пульт до полноценного бота (H04): отправить отчёт владельцу и прочитать его ответы.

    python -m hub.tg_send "текст"          # отправить (Markdown не используется — простой текст)
    python -m hub.tg_send --file отчёт.txt # отправить файл как текст (режется на части по 4000)
    python -m hub.tg_send --inbox          # новые сообщения владельца (смещение хранится локально)

Сеть — через системный HTTPS_PROXY (на ПК владельца Telegram только через VPN).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

from hub.secrets import TELEGRAM_BOT_TOKEN

OWNER_CHAT_ID = 1177080392
STATE = Path(os.environ.get("AGENT_HUB_HOME") or Path.home() / ".local/share/agent-hub") / "tg_offset.json"
LIMIT = 4000


def _call(method: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler()).open(req, timeout=30) as resp:
        return json.load(resp)


def send(text: str, chat_id: int = OWNER_CHAT_ID) -> None:
    for i in range(0, len(text), LIMIT):
        _call("sendMessage", {"chat_id": chat_id, "text": text[i:i + LIMIT],
                              "disable_web_page_preview": True})


def inbox() -> list[str]:
    offset = json.loads(STATE.read_text())["offset"] if STATE.exists() else 0
    ups = _call("getUpdates", {"offset": offset, "timeout": 0}).get("result", [])
    out = []
    for u in ups:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        if msg.get("text"):
            out.append(msg["text"])
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"offset": offset}))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="tg_send")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--file")
    ap.add_argument("--inbox", action="store_true")
    a = ap.parse_args()
    if a.inbox:
        msgs = inbox()
        print("\n---\n".join(msgs) if msgs else "(нет новых)")
        return
    text = Path(a.file).read_text(encoding="utf-8") if a.file else (a.text or "")
    if text.strip():
        send(text)
        print("отправлено")


if __name__ == "__main__":
    main()
