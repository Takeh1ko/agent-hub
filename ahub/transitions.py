"""Task state transitions, ownership (lease), and command idempotency.

Ownership rule (architecture §2): an active task has one owner — its process. It acquires the lease
(`acquire`), renews it (`renew`), and releases it when the task leaves active states. While the lease is alive,
only the owner changes an active task's state; everyone else asks (`request_stop`, `request_nudge`). The service
reclaims a task only once the lease has expired (process died) — `acquire` allows that.

All checks and writes run in one BEGIN IMMEDIATE transaction: two actors never transition a task at once.
Repeating the same transition (task already in the target state) is a no-op, with no event.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ahub import reasons
from ahub.i18n import t as _t
from ahub.model import ACTIVE, FINAL, NUDGEABLE, STATE_EVENT, WAITING_DECISION, Ev, State, can_move
from ahub.store import Store, Task, _dumps
from ahub.time import now_ms

DEFAULT_LEASE_MS = 90_000  # the owner renews more often (roughly every 30 s)


class TransitionError(ValueError):
    """Transition forbidden by the state table."""


class ConflictError(RuntimeError):
    """Task held by another owner or changed since it was read."""


def _lease_alive(task: Task, now: int) -> bool:
    return bool(task.owner) and task.lease_until is not None and task.lease_until > now


def move(store: Store, task_id: int, to: State | str, *, reason: str = "", by: str = "",
         owner: str | None = None, expect_from: set[State] | frozenset[State] | None = None,
         fields: dict[str, Any] | None = None, payload: dict | None = None, critical: bool = False,
         now: int | None = None, con: sqlite3.Connection | None = None) -> Task:
    """Move a task to `to`. Returns the task after the transition.

    owner — caller owner token (task process/service); None — client without ownership.
    expect_from — allowed source states (guard against "read then moved" races).
    fields — plain task fields changed by the same action (round, branch, base…).
    reason — a reason code blob (ahub.reasons.dump) or free text (a human note); it is stored as it is.
    """
    ts = now if now is not None else now_ms()
    dst = State(to)

    def _do(c: sqlite3.Connection) -> Task:
        task = store.get_task(task_id, con=c)
        if task is None:
            raise TransitionError(_t("trans.no_task", id=task_id))
        if task.state is dst:
            return task  # idempotent repeat
        if expect_from is not None and task.state not in expect_from:
            raise ConflictError(_t("trans.expect_from", label=task.label,
                                   expected=sorted(s.value for s in expect_from), actual=task.state.value))
        if not can_move(task.state, dst):
            raise TransitionError(_t("trans.forbidden", label=task.label, src=task.state.value, dst=dst.value))
        if task.state in ACTIVE and _lease_alive(task, ts) and owner != task.owner:
            raise ConflictError(_t("trans.owned", label=task.label, pid=task.owner_pid))
        if fields:
            store.update_task(task_id, now=ts, con=c, **fields)
        releasing = dst not in ACTIVE
        sets = ["state=?", "state_reason=?", "version=version+1", "updated_at=?"]
        args: list[Any] = [dst.value, reason, ts]
        if dst in FINAL:
            sets.append("finished_at=?")
            args.append(ts)
        if releasing:
            sets += ["owner=''", "owner_pid=NULL", "lease_until=NULL", "request=''", "request_text=''"]
        if dst not in ACTIVE:
            sets.append("phase=''")
        args.append(int(task_id))
        c.execute(f"UPDATE task SET {', '.join(sets)} WHERE id=?", args)
        body = {"from": task.state.value, "to": dst.value, "reason": reason, "by": by}
        if payload:
            body.update(payload)
        store.add_event(Ev.STATE, task_id=task_id, project=task.project, payload=body, now=ts, con=c)
        ev = STATE_EVENT.get(dst)
        if ev is not None:
            store.add_event(ev, task_id=task_id, project=task.project,
                            payload={"reason": reason, **(payload or {})}, critical=critical, now=ts, con=c)
        if dst in (State.REJECTED, State.ERROR):
            _cascade(store, c, task, dst, ts)
        return store.get_task(task_id, con=c)  # type: ignore[return-value]

    if con is not None:
        return _do(con)
    with store.tx() as c:
        return _do(c)


def _cascade(store: Store, c: sqlite3.Connection, task: Task, dst: State, ts: int) -> None:
    """Dependents not yet started → "Needs decision": their base will never be accepted."""
    for (dep_id,) in c.execute("SELECT task_id FROM task_dep WHERE after_id=?", (task.id,)).fetchall():
        dep = store.get_task(dep_id, con=c)
        if dep is None or dep.state not in (State.QUEUED, State.DRAFT):
            continue
        if dep.state is State.DRAFT:
            continue  # still a draft — a human decides at launch
        code = "dep_rejected" if dst is State.REJECTED else "dep_error"
        move(store, dep_id, State.NEEDS_DECISION, reason=reasons.dump(code, task=task.label), by="hub",
             now=ts, con=c)


# --- ownership ---

def acquire(store: Store, task_id: int, owner: str, *, pid: int | None, lease_ms: int = DEFAULT_LEASE_MS,
            now: int | None = None) -> bool:
    """Claim a queued or running task: free, lease expired, or already ours.

    False — held by a live owner, or the task is not queued/working (finished ones are never claimed).
    """
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        states = [s.value for s in ACTIVE | {State.QUEUED}]
        cur = c.execute(
            "UPDATE task SET owner=?, owner_pid=?, lease_until=?, version=version+1"
            " WHERE id=? AND (owner='' OR owner=? OR lease_until IS NULL OR lease_until<=?)"
            f" AND state IN ({','.join('?' * len(states))})",
            (owner, pid, ts + lease_ms, int(task_id), owner, ts, *states))
        return cur.rowcount == 1


def renew(store: Store, task_id: int, owner: str, *, lease_ms: int = DEFAULT_LEASE_MS,
          now: int | None = None) -> bool:
    """Renew the lease. False — the task was taken over (the owner must stop work at once)."""
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        cur = c.execute("UPDATE task SET lease_until=? WHERE id=? AND owner=?",
                        (ts + lease_ms, int(task_id), owner))
        return cur.rowcount == 1


def release(store: Store, task_id: int, owner: str) -> bool:
    with store.tx() as c:
        cur = c.execute("UPDATE task SET owner='', owner_pid=NULL, lease_until=NULL WHERE id=? AND owner=?",
                        (int(task_id), owner))
        return cur.rowcount == 1


def is_orphan(task: Task, now: int) -> bool:
    """Active task with no live lease — the process died or never claimed it."""
    return task.state in ACTIVE and not _lease_alive(task, now)


# --- requests to the owner ---

def request_stop(store: Store, task_id: int, *, reason: str | None = None, by: str = "",
                 now: int | None = None) -> str:
    """Stop a task. Returns 'stopped' (at once) or 'requested' (asked the owner)."""
    ts = now if now is not None else now_ms()
    if reason is None:
        reason = reasons.dump("stop_command")
    with store.tx() as c:
        task = store.get_task(task_id, con=c)
        if task is None:
            raise TransitionError(_t("trans.no_task", id=task_id))
        if task.state is State.STOPPED:
            return "stopped"
        if task.state in ACTIVE and _lease_alive(task, ts):
            c.execute("UPDATE task SET request='stop', request_text='' WHERE id=?", (int(task_id),))
            store.add_event(Ev.STATE, task_id=task_id, project=task.project,
                            payload={"request": "stop", "reason": reason, "by": by}, now=ts, con=c)
            return "requested"
        if task.state in FINAL or task.state in WAITING_DECISION - {State.NEEDS_DECISION}:
            raise TransitionError(_t("trans.nothing_to_stop", label=task.label, state=task.state.value))
        move(store, task_id, State.STOPPED, reason=reason, by=by, now=ts, con=c)
        return "stopped"


def request_nudge(store: Store, task_id: int, *, text: str, by: str = "",
                  now: int | None = None) -> str:
    """Message a working agent in its own session: the engine interrupts the current turn (the same
    cooperative poll as a stop) and continues the SAME session with this text.

    Only for a task the process is really running (a live lease) and whose session id is known —
    otherwise there is nobody to deliver it to. Returns 'requested'.
    """
    ts = now if now is not None else now_ms()
    msg = " ".join((text or "").split())
    if not msg:
        raise TransitionError(_t("trans.nudge_empty"))
    with store.tx() as c:
        task = store.get_task(task_id, con=c)
        if task is None:
            raise TransitionError(_t("trans.no_task", id=task_id))
        if task.request == "stop":
            raise TransitionError(_t("trans.nudge_stopping", label=task.label))
        if task.state not in NUDGEABLE:
            raise TransitionError(_t("trans.nudge_not_running", label=task.label, state=task.state.value))
        if not _lease_alive(task, ts):
            raise TransitionError(_t("trans.nudge_no_process", label=task.label))
        if not _last_session_id(store, c, task_id):
            raise TransitionError(_t("trans.nudge_no_session", label=task.label))
        c.execute("UPDATE task SET request='nudge', request_text=? WHERE id=?", (msg, int(task_id)))
        store.add_event(Ev.NUDGE, task_id=task_id, project=task.project,
                        payload={"text": msg, "by": by}, now=ts, con=c)
        return "requested"


def clear_request(store: Store, task_id: int, *, kind: str, text: str = "") -> bool:
    """The owner takes its request back (it acted on it). `text` — only the same nudge, not a newer one."""
    sql = "UPDATE task SET request='', request_text='' WHERE id=? AND request=?"
    args: list[Any] = [int(task_id), kind]
    if text:
        sql += " AND request_text=?"
        args.append(text)
    with store.tx() as c:
        return c.execute(sql, args).rowcount == 1


def _last_session_id(store: Store, c: sqlite3.Connection, task_id: int) -> str:
    """Provider-side session id of the last session of the task ('' — no session yet)."""
    row = c.execute("SELECT external_id FROM session WHERE task_id=? AND external_id!='' ORDER BY id DESC"
                    " LIMIT 1", (int(task_id),)).fetchone()
    return str(row[0]) if row else ""


# --- idempotency ---

def once(store: Store, key: str, fn: Callable[[sqlite3.Connection], Any], *, now: int | None = None) -> Any:
    """Run fn(con) once per key. A repeat with the same key returns the earlier result.

    fn runs in the same transaction as the key write: either both the action and the key, or neither.
    The result must serialize to JSON.
    """
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        row = c.execute("SELECT result_json FROM op WHERE key=?", (key,)).fetchone()
        if row is not None:
            return json.loads(row[0])
        result = fn(c)
        c.execute("INSERT INTO op(key, ts, result_json) VALUES(?,?,?)", (key, ts, _dumps(result)))
        return result
