"""Переходы состояний задачи, владение (аренда) и идемпотентность команд.

Правило владения (architecture §2): у активной задачи один владелец — её процесс. Он берёт аренду
(`acquire`), продлевает её (`renew`) и отпускает, когда задача уходит из активных состояний. Пока аренда жива,
состояние активной задачи меняет только владелец; остальные просят (`request_stop`). Сервис забирает задачу,
только когда аренда истекла (процесс умер) — `acquire` это позволяет.

Все проверки и изменения — в одной транзакции BEGIN IMMEDIATE: двое не переведут задачу одновременно.
Повтор того же перехода (задача уже в целевом состоянии) — без изменений и без события.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from ahub.model import ACTIVE, FINAL, STATE_EVENT, WAITING_DECISION, Ev, State, can_move
from ahub.store import Store, Task, _dumps
from ahub.time import now_ms

DEFAULT_LEASE_MS = 90_000  # владелец продлевает чаще (раз в ~30 с)


class TransitionError(ValueError):
    """Переход запрещён таблицей состояний."""


class ConflictError(RuntimeError):
    """Задача занята другим владельцем или изменилась с момента чтения."""


def _lease_alive(task: Task, now: int) -> bool:
    return bool(task.owner) and task.lease_until is not None and task.lease_until > now


def move(store: Store, task_id: int, to: State | str, *, reason: str = "", by: str = "",
         owner: str | None = None, expect_from: set[State] | frozenset[State] | None = None,
         fields: dict[str, Any] | None = None, payload: dict | None = None, critical: bool = False,
         now: int | None = None, con: sqlite3.Connection | None = None) -> Task:
    """Перевести задачу в `to`. Возвращает задачу после перехода.

    owner — токен вызывающего владельца (процесс задачи/сервис); None — клиент без владения.
    expect_from — допустимые исходные состояния (защита от гонки «прочитал — перевёл»).
    fields — обычные поля задачи, меняемые тем же действием (раунд, ветка, база…).
    """
    ts = now if now is not None else now_ms()
    dst = State(to)

    def _do(c: sqlite3.Connection) -> Task:
        task = store.get_task(task_id, con=c)
        if task is None:
            raise TransitionError(f"нет задачи T{task_id}")
        if task.state is dst:
            return task  # идемпотентный повтор
        if expect_from is not None and task.state not in expect_from:
            raise ConflictError(f"{task.label}: ожидалось {sorted(s.value for s in expect_from)},"
                                f" сейчас {task.state.value}")
        if not can_move(task.state, dst):
            raise TransitionError(f"{task.label}: переход {task.state.value} → {dst.value} запрещён")
        if task.state in ACTIVE and _lease_alive(task, ts) and owner != task.owner:
            raise ConflictError(f"{task.label}: задачей владеет процесс pid={task.owner_pid};"
                                f" изменить может только он (попросите остановку)")
        if fields:
            store.update_task(task_id, now=ts, con=c, **fields)
        releasing = dst not in ACTIVE
        sets = ["state=?", "state_reason=?", "version=version+1", "updated_at=?"]
        args: list[Any] = [dst.value, reason, ts]
        if dst in FINAL:
            sets.append("finished_at=?")
            args.append(ts)
        if releasing:
            sets += ["owner=''", "owner_pid=NULL", "lease_until=NULL", "request=''"]
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
    """Зависимые, ещё не начатые задачи → «Нужно решение»: их основа не будет принята."""
    word = "отклонена" if dst is State.REJECTED else "в ошибке"
    for (dep_id,) in c.execute("SELECT task_id FROM task_dep WHERE after_id=?", (task.id,)).fetchall():
        dep = store.get_task(dep_id, con=c)
        if dep is None or dep.state not in (State.QUEUED, State.DRAFT):
            continue
        if dep.state is State.DRAFT:
            continue  # черновик ещё не запущен — решит человек при запуске
        move(store, dep_id, State.NEEDS_DECISION, reason=f"зависимость {task.label} {word}", by="hub",
             now=ts, con=c)


# --- владение ---

def acquire(store: Store, task_id: int, owner: str, *, pid: int | None, lease_ms: int = DEFAULT_LEASE_MS,
            now: int | None = None) -> bool:
    """Взять задачу в очереди или в работе: свободна, аренда истекла или уже наша.

    False — занята живым владельцем или задача не в очереди/работе (решённую не берут).
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
    """Продлить аренду. False — задачу забрали (владелец должен немедленно прекратить работу)."""
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
    """Активная задача без живой аренды — процесс умер или не взял её."""
    return task.state in ACTIVE and not _lease_alive(task, now)


# --- просьбы владельцу ---

def request_stop(store: Store, task_id: int, *, reason: str = "остановлена командой", by: str = "",
                 now: int | None = None) -> str:
    """Остановить задачу. Возвращает 'stopped' (сразу) или 'requested' (попросили владельца)."""
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        task = store.get_task(task_id, con=c)
        if task is None:
            raise TransitionError(f"нет задачи T{task_id}")
        if task.state is State.STOPPED:
            return "stopped"
        if task.state in ACTIVE and _lease_alive(task, ts):
            c.execute("UPDATE task SET request='stop' WHERE id=?", (int(task_id),))
            store.add_event(Ev.STATE, task_id=task_id, project=task.project,
                            payload={"request": "stop", "reason": reason, "by": by}, now=ts, con=c)
            return "requested"
        if task.state in FINAL or task.state in WAITING_DECISION - {State.NEEDS_DECISION}:
            raise TransitionError(f"{task.label}: {task.state.value} — останавливать нечего")
        move(store, task_id, State.STOPPED, reason=reason, by=by, now=ts, con=c)
        return "stopped"


# --- идемпотентность ---

def once(store: Store, key: str, fn: Callable[[sqlite3.Connection], Any], *, now: int | None = None) -> Any:
    """Выполнить fn(con) один раз для ключа. Повтор с тем же ключом возвращает прежний результат.

    fn работает в той же транзакции, что и запись ключа: либо и действие, и ключ, либо ничего.
    Результат должен сериализоваться в JSON.
    """
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        row = c.execute("SELECT result_json FROM op WHERE key=?", (key,)).fetchone()
        if row is not None:
            return json.loads(row[0])
        result = fn(c)
        c.execute("INSERT INTO op(key, ts, result_json) VALUES(?,?,?)", (key, ts, _dumps(result)))
        return result
