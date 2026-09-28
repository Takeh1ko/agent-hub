"""hub inbox: непрочитанное владельцево + отвеченные вопросы."""

from __future__ import annotations

import sqlite3

from hub.store import Store


def cmd_inbox(args) -> int:
    store = Store()
    limit = int(getattr(args, "limit", 20) or 0)
    peek = bool(getattr(args, "peek", False))
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM inbox WHERE seen_claude=0 ORDER BY ts, id LIMIT ?",
            (limit,),
        ).fetchall()
        questions = con.execute(
            "SELECT * FROM question WHERE status='answered' ORDER BY id",
        ).fetchall()
        for r in rows:
            print(f"inbox:{r['id']} {r['text']}")
        for q in questions:
            print(f"answer:{q['id']} [{q['task_id']}] {q['text']} → {q['answer']}")
        if not rows and not questions:
            print("(пусто)")
        elif rows and not peek:
            ids = [r["id"] for r in rows]
            con.execute(
                f"UPDATE inbox SET seen_claude=1 WHERE id IN "
                f"({','.join('?' for _ in ids)})",
                ids,
            )
            con.commit()
    finally:
        con.close()
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("inbox", help="непрочитанное от владельца")
    p.add_argument("--limit", type=int, default=20, help="сколько показать")
    p.add_argument("--peek", action="store_true", help="не отмечать прочитанным")
    p.set_defaults(func=cmd_inbox)
