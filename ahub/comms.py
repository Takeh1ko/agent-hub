"""Orchestrator ↔ human link: messages (TG) and questions with options (architecture §9, §11).

The TG bridge (V26) writes incoming human messages: a message(direction=in) row + owner_message event.
Outgoing from the orchestrator (`ahub say`) — message(direction=out); the bridge sends it and stamps delivered_at.
Question (`ahub ask`) — question(open); human answer (button/text) → answered + answer event.

Every row an orchestrator reads carries its project (ahub/scope.py): the message, the question and the event
of the answer. A question asked without a project but about a task takes the task's one; without a task — hub-wide.
"""

from __future__ import annotations

import json

from ahub import events
from ahub.model import Ev
from ahub.scope import Scope, where
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


def inbox(store: Store, *, mark: bool = True, scope: Scope | None = None, now: int | None = None) -> list[dict]:
    """Unread human messages of the scope; mark — mark them read and ack their events (of the same scope)."""
    ts = now if now is not None else now_ms()
    cond, args = where(scope)
    with store.tx() as c:
        sql = "SELECT id, ts, text, project FROM message WHERE direction='in' AND delivered_at IS NULL"
        if cond:
            sql += " AND " + cond
        rows = [dict(r) for r in c.execute(sql + " ORDER BY id", args)]
        to_mark = [r for r in rows if r["project"] != ""] if (scope and not scope.all) else rows
        if mark and to_mark:
            c.execute(f"UPDATE message SET delivered_at=? WHERE id IN ({','.join('?' * len(to_mark))})",
                      (ts, *[r["id"] for r in to_mark]))
    if mark and to_mark:
        if scope and not scope.all:
            with store.tx() as c:
                marks = ",".join("?" * len(scope.projects))
                c.execute(
                    f"UPDATE event SET acked_at=?, delivered_at=COALESCE(delivered_at, ?)"
                    f" WHERE needs_reaction=1 AND acked_at IS NULL AND kind=? AND project IN ({marks})",
                    (ts, ts, Ev.OWNER_MESSAGE.value, *scope.projects),
                )
        else:
            events.ack(store, kinds=(Ev.OWNER_MESSAGE.value,), scope=scope, now=ts)
    return rows


def message(store: Store, message_id: int) -> dict | None:
    """One message by its number, read or not — `ahub inbox <id>` reads a message in full."""
    with store.read() as c:
        row = c.execute("SELECT id, ts, direction, text, project, chat_id, delivered_at FROM message WHERE id=?",
                        (message_id,)).fetchone()
    return dict(row) if row is not None else None


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
        project: str = "", asked_by: str = "orchestrator", now: int | None = None) -> int:
    """A question to the human. A question about a task belongs to the task's project."""
    with store.tx() as c:
        if task_id is not None and not project:
            row = c.execute("SELECT project FROM task WHERE id=?", (task_id,)).fetchone()
            project = str(row["project"]) if row else ""
        return int(c.execute(
            "INSERT INTO question(ts, task_id, project, asked_by, text, options_json) VALUES(?,?,?,?,?,?)",
            (now if now is not None else now_ms(), task_id, project, asked_by, text,
             json.dumps(options or [], ensure_ascii=False))).lastrowid)


def answer(store: Store, question_id: int, text: str, *, via: str = "tg", now: int | None = None) -> bool:
    """Human answer. False — no such question or already answered (first answer wins)."""
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        row = c.execute("SELECT * FROM question WHERE id=?", (question_id,)).fetchone()
        if row is None or row["status"] != "open":
            return False
        c.execute("UPDATE question SET status='answered', answer=?, answered_via=?, answered_at=? WHERE id=?",
                  (text, via, ts, question_id))
        store.add_event(Ev.ANSWER, task_id=row["task_id"], project=row["project"],
                        payload={"question_id": question_id, "question": row["text"], "answer": text},
                        now=ts, con=c)
    return True


def open_questions(store: Store, scope: Scope | None = None) -> list[dict]:
    cond, args = where(scope)
    with store.read() as c:
        sql = "SELECT * FROM question WHERE status='open'"
        if cond:
            sql += " AND " + cond
        rows = [dict(r) for r in c.execute(sql + " ORDER BY id", args)]
    for r in rows:
        r["options"] = json.loads(r.pop("options_json") or "[]")
    return rows


def cancel_question(store: Store, question_id: int) -> bool:
    with store.tx() as c:
        return c.execute("UPDATE question SET status='cancelled' WHERE id=? AND status='open'",
                         (question_id,)).rowcount == 1


def question(store: Store, question_id: int) -> dict | None:
    """One question by its number, open or not — `ahub questions <id>` reads it in full."""
    with store.read() as c:
        row = c.execute("SELECT * FROM question WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["options"] = json.loads(out.pop("options_json") or "[]")
    return out


def raise_alarm(store: Store, text: str, *, critical: bool = False, project: str = "", details: dict | None = None,
                now: int | None = None) -> int:
    return store.add_event(Ev.ALARM, project=project, critical=critical,
                           payload={"text": text, **(details or {})}, now=now)


def alarms(store: Store, *, unacked_only: bool = True, scope: Scope | None = None) -> list:
    """Alarms of the scope (the observer writes most of them hub-wide — those are in every scope)."""
    return [e for e in store.events(needs_reaction=True, unacked=unacked_only)
            if e.kind == Ev.ALARM.value and (scope is None or e.project in scope)]


ESCALATE_MS = 15 * 60_000


def alarms_for_tg(store: Store, *, now: int | None = None, escalate_ms: int = ESCALATE_MS) -> list:
    """Alarms due for the human: critical — at once; plain — unacked past escalate_ms."""
    ts = now if now is not None else now_ms()
    out = []
    for e in store.events(needs_reaction=True):
        if e.kind != Ev.ALARM.value or e.tg_sent_at is not None:
            continue
        if e.critical or (e.acked_at is None and ts - e.ts >= escalate_ms):
            out.append(e)
    return out


def mark_tg_sent(store: Store, event_ids: list[int], *, now: int | None = None) -> None:
    if not event_ids:
        return
    with store.tx() as c:
        c.execute(f"UPDATE event SET tg_sent_at=? WHERE id IN ({','.join('?' * len(event_ids))})",
                  (now if now is not None else now_ms(), *event_ids))
