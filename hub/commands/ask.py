"""hub ask: вопрос Claude владельцу."""

from __future__ import annotations

import json
import sqlite3

from hub import time as ht
from hub.store import Store


def cmd_ask(args) -> int:
    raw = getattr(args, "options", None) or ""
    if raw.strip():
        opts = [o.strip() for o in str(raw).split(",")]
        opts = [o for o in opts if o]
    else:
        opts = []
    task_id = getattr(args, "task", None) or ""
    store = Store()
    con = sqlite3.connect(str(store.path))
    try:
        cur = con.execute(
            "INSERT INTO question(task_id, asked_by, text, options_json,"
            " status, answer, answered_via, ts)"
            " VALUES (?, ?, ?, ?, 'open', '', '', ?)",
            (task_id, "claude", args.text,
             json.dumps(opts, ensure_ascii=False), ht.now_ms()),
        )
        con.commit()
        print(f"ask:{int(cur.lastrowid)}")
    finally:
        con.close()
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("ask", help="спросить владельца")
    p.add_argument("text", help="текст вопроса")
    p.add_argument("--options", default=None, help="варианты через запятую")
    p.add_argument("--task", default=None, help="ID задачи")
    p.set_defaults(func=cmd_ask)
