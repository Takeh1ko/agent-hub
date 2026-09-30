"""Связь оркестратор ↔ человек: сообщения (TG) и вопросы с вариантами (architecture §9, §11).

Входящее сообщение человека пишет TG-мост (V26): строка message(direction=in) + событие owner_message.
Исходящее от оркестратора (`ahub say`) — message(direction=out); мост отправляет и ставит delivered_at.
Вопрос (`ahub ask`) — question(open); ответ человека (кнопка/текст) → answered + событие answer.
"""

from __future__ import annotations

import json

from ahub import events
from ahub.model import Ev
from ahub.store import Store
from ahub.time import now_ms


def owner_message(store: Store, text: str, *, project: str = "", chat_id: int | None = None,
                  now: int | None = None) -> int:
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        mid = int(c.execute("INSERT INTO message(ts, direction, text, project, chat_id) VALUES(?,?,?,?,?)",
                            (ts, "in", text, project, chat_id)).lastrowid)
        store.add_event(Ev.OWNER_MESSAGE, project=project, payload={"message_id": mid, "text": text[:2000]},
                        now=ts, con=c)
    return mid


def inbox(store: Store, *, mark: bool = True, now: int | None = None) -> list[dict]:
    """Непрочитанные сообщения человека; mark — пометить прочитанными и подтвердить их события."""
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, ts, text, project FROM message WHERE direction='in' AND delivered_at IS NULL ORDER BY id")]
        if mark and rows:
            c.execute(f"UPDATE message SET delivered_at=? WHERE id IN ({','.join('?' * len(rows))})",
                      (ts, *[r["id"] for r in rows]))
    if mark:
        events.ack(store, kinds=(Ev.OWNER_MESSAGE.value,))
    return rows


def say(store: Store, text: str, *, project: str = "", now: int | None = None) -> int:
    with store.tx() as c:
        return int(c.execute("INSERT INTO message(ts, direction, text, project) VALUES(?,?,?,?)",
                             (now if now is not None else now_ms(), "out", text, project)).lastrowid)


def outbox(store: Store) -> list[dict]:
    with store.read() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, ts, text, project FROM message WHERE direction='out' AND delivered_at IS NULL ORDER BY id")]


def mark_sent(store: Store, message_id: int, *, now: int | None = None) -> None:
    with store.tx() as c:
        c.execute("UPDATE message SET delivered_at=? WHERE id=?", (now if now is not None else now_ms(), message_id))


def ask(store: Store, text: str, options: list[str] | None = None, *, task_id: int | None = None,
        asked_by: str = "orchestrator", now: int | None = None) -> int:
    with store.tx() as c:
        return int(c.execute("INSERT INTO question(ts, task_id, asked_by, text, options_json) VALUES(?,?,?,?,?)",
                             (now if now is not None else now_ms(), task_id, asked_by, text,
                              json.dumps(options or [], ensure_ascii=False))).lastrowid)


def answer(store: Store, question_id: int, text: str, *, via: str = "tg", now: int | None = None) -> bool:
    """Ответ человека. False — вопроса нет или уже отвечен (первый ответ побеждает)."""
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        row = c.execute("SELECT * FROM question WHERE id=?", (question_id,)).fetchone()
        if row is None or row["status"] != "open":
            return False
        c.execute("UPDATE question SET status='answered', answer=?, answered_via=?, answered_at=? WHERE id=?",
                  (text, via, ts, question_id))
        store.add_event(Ev.ANSWER, task_id=row["task_id"], payload={"question_id": question_id,
                                                                    "question": row["text"], "answer": text},
                        now=ts, con=c)
    return True


def open_questions(store: Store) -> list[dict]:
    with store.read() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM question WHERE status='open' ORDER BY id")]
    for r in rows:
        r["options"] = json.loads(r.pop("options_json") or "[]")
    return rows


def cancel_question(store: Store, question_id: int) -> bool:
    with store.tx() as c:
        return c.execute("UPDATE question SET status='cancelled' WHERE id=? AND status='open'",
                         (question_id,)).rowcount == 1


def raise_alarm(store: Store, text: str, *, critical: bool = False, project: str = "", details: dict | None = None,
                now: int | None = None) -> int:
    return store.add_event(Ev.ALARM, project=project, critical=critical,
                           payload={"text": text, **(details or {})}, now=now)


def alarms(store: Store, *, unacked_only: bool = True) -> list:
    return [e for e in store.events(needs_reaction=True, unacked=unacked_only) if e.kind == Ev.ALARM.value]
