"""hub say: сообщение Claude владельцу (отправит бот H05)."""

from __future__ import annotations

import sqlite3

from hub import time as ht
from hub.store import Store


def cmd_say(args) -> int:
    store = Store()
    con = sqlite3.connect(str(store.path))
    try:
        con.execute(
            "INSERT INTO outbox(ts, text, task_id, sent_ts) VALUES (?, ?, ?, NULL)",
            (ht.now_ms(), args.text, getattr(args, "task", None) or ""),
        )
        con.commit()
    finally:
        con.close()
    print("ok")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("say", help="сообщение владельцу в TG")
    p.add_argument("text", help="текст сообщения")
    p.add_argument("--task", default=None, help="ID задачи")
    p.set_defaults(func=cmd_say)
