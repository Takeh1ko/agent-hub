"""hub inbox: непрочитанное от владельца + вопросы (open/новые answered)."""

from __future__ import annotations

import sqlite3
import sys

from hub.store import Store

SEEN_KEY = "claude_seen_question"


def _seen_cursor(con) -> int:
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key=?", (SEEN_KEY,)).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(row["value"] if isinstance(row, sqlite3.Row) else row[0])
    except (TypeError, ValueError):
        return 0


def cmd_inbox(args) -> int:
    store = Store()
    try:
        limit = int(getattr(args, "limit", 20))
    except (TypeError, ValueError):
        print(f"непонятный --limit: {getattr(args, 'limit', None)!r}",
              file=sys.stderr)
        return 2
    if limit < 0:
        print(f"непонятный --limit: {limit}", file=sys.stderr)
        return 2
    peek = bool(getattr(args, "peek", False))
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM inbox WHERE seen_claude=0 ORDER BY ts, id LIMIT ?",
            (limit,),
        ).fetchall()
        opened = con.execute(
            "SELECT * FROM question WHERE status='open' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        cursor = _seen_cursor(con)
        answered = con.execute(
            "SELECT * FROM question WHERE status='answered' AND id > ?"
            " ORDER BY id LIMIT ?",
            (cursor, limit),
        ).fetchall()
        for r in rows:
            print(f"inbox:{r['id']} {r['text']}")
        for q in opened:
            print(f"question:{q['id']} [{q['task_id']}] {q['text']}")
        for q in answered:
            print(f"answer:{q['id']} [{q['task_id']}] {q['text']} → {q['answer']}")
        if not rows and not opened and not answered:
            print("(пусто)")
        elif not peek:
            if rows:
                ids = [r["id"] for r in rows]
                con.execute(
                    "UPDATE inbox SET seen_claude=1 WHERE id IN "
                    f"({','.join('?' for _ in ids)})",
                    ids,
                )
            if answered:
                top = max(q["id"] for q in answered)
                con.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (SEEN_KEY, str(top)),
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
