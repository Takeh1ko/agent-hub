"""hub inbox: непрочитанное от владельца + вопросы (open/новые answered)."""

from __future__ import annotations

import json
import sqlite3
import sys

from hub.store import Store

SEEN_KEY = "claude_seen_question"
# Ограничение размера множества показанных, чтобы meta не росла бесконечно.
_SEEN_CAP = 1000


def _load_seen(con) -> set[int]:
    """Множество показанных answered-id из meta (JSON-список)."""
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key=?", (SEEN_KEY,)).fetchone()
    except sqlite3.Error:
        return set()
    if row is None:
        return set()
    val = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    if val is None or val == "":
        return set()
    try:
        data = json.loads(val)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, list):
        out: set[int] = set()
        for x in data:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(data, int):
        legacy = data
    else:
        try:
            legacy = int(val)
        except (TypeError, ValueError):
            return set()
    # Легаси-курсор (целое): показанным считалось всё с id <= N.
    try:
        rows = con.execute(
            "SELECT id FROM question WHERE status='answered' AND id <= ?",
            (legacy,)).fetchall()
    except sqlite3.Error:
        return set()
    return {int(r["id"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows}


def _save_seen(con, seen: set[int]) -> None:
    payload = json.dumps(sorted(seen)[-_SEEN_CAP:])
    con.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SEEN_KEY, payload),
    )


def cmd_inbox(args) -> int:
    store = Store()
    try:
        limit = int(getattr(args, "limit", 20))
    except (TypeError, ValueError):
        print(f"непонятный --limit: {getattr(args, 'limit', None)!r}",
              file=sys.stderr)
        return 2
    if limit <= 0:
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
        seen = _load_seen(con)
        all_answered = con.execute(
            "SELECT * FROM question WHERE status='answered' ORDER BY id",
        ).fetchall()
        answered = [q for q in all_answered if q["id"] not in seen][:limit]
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
                _save_seen(con, seen | {int(q["id"]) for q in answered})
            con.commit()
    finally:
        con.close()
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("inbox", help="непрочитанное от владельца")
    p.add_argument("--limit", type=int, default=20, help="сколько показать")
    p.add_argument("--peek", action="store_true", help="не отмечать прочитанным")
    p.set_defaults(func=cmd_inbox)
