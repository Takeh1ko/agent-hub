"""inbox/ask/say: вопросы, непрочитанное, outbox, миграция 002."""

from __future__ import annotations

import json
import sqlite3

from hub.cli import main
from hub.store import Store


def _con(store: Store):
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    return con


def _add_inbox(store: Store, text: str, ts: int, source: str = "tg") -> int:
    con = _con(store)
    try:
        cur = con.execute(
            "INSERT INTO inbox(ts, text, source, seen_claude) VALUES (?, ?, ?, 0)",
            (ts, text, source),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def test_ask_creates_open(capsys):
    s = Store()
    assert main(["ask", "продлить бюджет?", "--options", "да,нет", "--task", "T1"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("ask:")
    qid = int(out.split(":")[1])
    con = _con(s)
    try:
        row = con.execute("SELECT * FROM question WHERE id=?", (qid,)).fetchone()
    finally:
        con.close()
    assert row["status"] == "open" and row["text"] == "продлить бюджет?"
    assert json.loads(row["options_json"]) == ["да", "нет"]
    assert row["task_id"] == "T1"


def test_inbox_order_and_mark(capsys):
    s = Store()
    _add_inbox(s, "первое", 1000)
    _add_inbox(s, "второе", 2000)
    assert main(["inbox"]) == 0
    out = capsys.readouterr().out
    assert out.index("первое") < out.index("второе")
    # Отметило прочитанным: второй вызов пустой по inbox.
    con = _con(s)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM inbox WHERE seen_claude=0").fetchone()[0]
    finally:
        con.close()
    assert n == 0


def test_inbox_peek_keeps_unread(capsys):
    s = Store()
    _add_inbox(s, "не читать вслух", 1000)
    assert main(["inbox", "--peek"]) == 0
    assert "не читать вслух" in capsys.readouterr().out
    con = _con(s)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM inbox WHERE seen_claude=0").fetchone()[0]
    finally:
        con.close()
    assert n == 1
    # Обычный вызов после peek всё ещё видит и затем отмечает.
    assert main(["inbox"]) == 0
    assert "не читать вслух" in capsys.readouterr().out


def test_inbox_limit(capsys):
    s = Store()
    for i in range(5):
        _add_inbox(s, f"м{i}", 1000 + i)
    assert main(["inbox", "--limit", "2"]) == 0
    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if "м" in ln]
    assert len(lines) == 2 and "м0" in lines[0] and "м1" in lines[1]


def test_inbox_shows_answered(capsys):
    s = Store()
    _add_inbox(s, "привет", 1000)
    con = _con(s)
    try:
        con.execute(
            "INSERT INTO question(task_id, asked_by, text, options_json,"
            " status, answer, answered_via, ts)"
            " VALUES ('T9','claude','идём дальше?','[]','answered','да','tg',2000)")
        con.commit()
    finally:
        con.close()
    assert main(["inbox", "--peek"]) == 0
    out = capsys.readouterr().out
    assert "привет" in out and "да" in out


def test_say_insert_ok(capsys):
    s = Store()
    assert main(["say", "готово, жду", "--task", "T3"]) == 0
    assert capsys.readouterr().out.strip() == "ok"
    con = _con(s)
    try:
        row = con.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    assert row["text"] == "готово, жду" and row["task_id"] == "T3"
    assert row["sent_ts"] is None


def test_migration_002_idempotent(tmp_path):
    from pathlib import Path

    s = Store(tmp_path / "hub.db")
    con = sqlite3.connect(str(s.path))
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"outbox", "meta"} <= tables
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_event_task_id" in idx
    finally:
        con.close()
    # Повторное применение файла — без ошибок (идемпотентна).
    sql = (Path(__file__).resolve().parent.parent
           / "hub" / "migrations" / "002_handles.sql").read_text(encoding="utf-8")
    con = sqlite3.connect(str(s.path))
    try:
        con.executescript(sql)
        con.executescript(sql)
    finally:
        con.close()
    # Store переоткрывается поверх без ошибок.
    Store(tmp_path / "hub.db")
