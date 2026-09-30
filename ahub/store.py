"""Хранилище хаба: SQLite (WAL), короткие соединения, миграции по PRAGMA user_version.

Правила:
- Каждый метод открывает и закрывает своё соединение (процессы задач, сервис и клиенты пишут одновременно).
- Запись — в транзакции BEGIN IMMEDIATE (`tx()`), чтобы проверка и изменение были атомарны.
- Состояние задачи здесь НЕ меняется напрямую — только через ahub.transitions (V02b), с журналом.
- Время приходит параметром `now` (мс) там, где важно для тестов; иначе — ahub.time.now_ms().
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ahub import paths
from ahub.model import NEEDS_REACTION, Ev, Kind, State
from ahub.time import now_ms

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BUSY_TIMEOUT_MS = 15_000


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


@dataclass
class Task:
    id: int
    project: str
    kind: Kind
    title: str
    spec: str = ""
    spec_hash: str = ""
    result_format: str = ""
    executor: str = ""
    review: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    budget_go: float = 0.0
    budget_usd: float = 0.0
    state: State = State.QUEUED
    phase: str = ""
    state_reason: str = ""
    round: int = 0
    branch: str = ""
    worktree: str = ""
    base_sha: str = ""
    accepted_sha: str = ""
    created_by: str = ""
    created_at: int = 0
    updated_at: int = 0
    finished_at: int | None = None
    owner: str = ""
    owner_pid: int | None = None
    lease_until: int | None = None
    version: int = 0
    request: str = ""  # просьба владельцу: '' | stop
    after: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"T{self.id}"

    @classmethod
    def from_row(cls, row: sqlite3.Row, after: list[int] | None = None) -> "Task":
        d = dict(row)
        d["review"] = _loads(d.pop("review_json"), {})
        d["limits"] = _loads(d.pop("limits_json"), {})
        d["kind"] = Kind(d["kind"])
        d["state"] = State(d["state"])
        d["after"] = list(after or [])
        return cls(**d)


# Поля задачи, которые можно менять обычным обновлением (не состояние и не владение).
_TASK_PLAIN_FIELDS = frozenset({
    "title", "spec", "spec_hash", "result_format", "executor", "budget_go", "budget_usd", "phase",
    "round", "branch", "worktree", "base_sha", "accepted_sha", "state_reason",
})
_TASK_JSON_FIELDS = {"review": "review_json", "limits": "limits_json"}


@dataclass
class Event:
    id: int
    ts: int
    task_id: int | None
    project: str
    kind: str
    payload: dict
    needs_reaction: bool
    critical: bool
    delivered_at: int | None
    acked_at: int | None
    tg_sent_at: int | None
    deliveries: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        d = dict(row)
        d["payload"] = _loads(d.pop("payload_json"), {})
        d["needs_reaction"] = bool(d["needs_reaction"])
        d["critical"] = bool(d["critical"])
        return cls(**d)


@dataclass
class Session:
    id: int
    task_id: int | None
    provider: str
    external_id: str
    role: str
    round: int
    model: str
    pid: int | None
    status: str
    outcome: str
    started_at: int
    ended_at: int | None
    cost_go: float
    cost_usd: float
    quota: float
    tokens: dict
    log_path: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Session":
        d = dict(row)
        d["tokens"] = _loads(d.pop("tokens_json"), {})
        return cls(**d)


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # --- соединения ---

    def _open(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self._open()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция записи: BEGIN IMMEDIATE … COMMIT, при исключении — ROLLBACK."""
        con = self._open()
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    def _migrate(self) -> None:
        con = self._open()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
            con.execute("BEGIN IMMEDIATE")
            try:
                current = con.execute("PRAGMA user_version").fetchone()[0]
                for f in files:
                    num = int(f.name[:3])
                    if num <= current:
                        continue
                    for stmt in _split_sql(f.read_text(encoding="utf-8")):
                        con.execute(stmt)
                    con.execute(f"PRAGMA user_version={num}")
                    current = num
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    def schema_version(self) -> int:
        with self.read() as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    # --- meta ---

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        with self.read() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self.tx() as con:
            con.execute("INSERT INTO meta(key, value) VALUES(?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def meta_del(self, key: str) -> None:
        with self.tx() as con:
            con.execute("DELETE FROM meta WHERE key=?", (key,))

    # --- задачи ---

    def create_task(self, *, project: str, kind: Kind | str, title: str, spec: str = "",
                    spec_hash: str = "", result_format: str = "", executor: str = "",
                    review: dict | None = None, limits: dict | None = None,
                    budget_go: float = 0.0, budget_usd: float = 0.0,
                    state: State | str = State.QUEUED, created_by: str = "",
                    after: list[int] | None = None, now: int | None = None,
                    con: sqlite3.Connection | None = None) -> int:
        """Создать задачу + событие created (в одной транзакции). Возвращает id."""
        ts = now if now is not None else now_ms()
        kind = Kind(kind)
        state = State(state)
        if state not in (State.QUEUED, State.DRAFT):
            raise ValueError(f"новая задача — только queued или draft, не {state}")

        def _do(c: sqlite3.Connection) -> int:
            cur = c.execute(
                "INSERT INTO task(project, kind, title, spec, spec_hash, result_format, executor,"
                " review_json, limits_json, budget_go, budget_usd, state, created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project, kind.value, title, spec, spec_hash, result_format, executor,
                 _dumps(review or {}), _dumps(limits or {}), float(budget_go), float(budget_usd),
                 state.value, created_by, ts, ts))
            tid = int(cur.lastrowid)
            for a in after or []:
                c.execute("INSERT OR IGNORE INTO task_dep(task_id, after_id) VALUES(?, ?)", (tid, int(a)))
            self.add_event(Ev.CREATED, task_id=tid, project=project,
                           payload={"kind": kind.value, "state": state.value, "by": created_by},
                           now=ts, con=c)
            return tid

        if con is not None:
            return _do(con)
        with self.tx() as c:
            return _do(c)

    def get_task(self, task_id: int, con: sqlite3.Connection | None = None) -> Task | None:
        def _do(c: sqlite3.Connection) -> Task | None:
            row = c.execute("SELECT * FROM task WHERE id=?", (int(task_id),)).fetchone()
            if row is None:
                return None
            after = [r[0] for r in c.execute(
                "SELECT after_id FROM task_dep WHERE task_id=? ORDER BY after_id", (int(task_id),))]
            return Task.from_row(row, after)

        if con is not None:
            return _do(con)
        with self.read() as c:
            return _do(c)

    def list_tasks(self, *, states: set[State] | frozenset[State] | None = None,
                   project: str | None = None, limit: int | None = None,
                   newest_first: bool = False) -> list[Task]:
        sql = "SELECT * FROM task"
        where, args = [], []
        if states is not None:
            if not states:
                return []
            where.append(f"state IN ({','.join('?' * len(states))})")
            args.extend(State(s).value for s in states)
        if project is not None:
            where.append("project=?")
            args.append(project)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.read() as c:
            rows = c.execute(sql, args).fetchall()
            deps: dict[int, list[int]] = {}
            ids = [r["id"] for r in rows]
            if ids:
                for tid, aid in c.execute(
                        f"SELECT task_id, after_id FROM task_dep WHERE task_id IN ({','.join('?' * len(ids))})"
                        " ORDER BY after_id", ids):
                    deps.setdefault(tid, []).append(aid)
        return [Task.from_row(r, deps.get(r["id"])) for r in rows]

    def update_task(self, task_id: int, *, now: int | None = None,
                    con: sqlite3.Connection | None = None, **fields: Any) -> None:
        """Обычные поля задачи (не состояние, не владение). Неизвестное поле — ошибка."""
        bad = set(fields) - _TASK_PLAIN_FIELDS - set(_TASK_JSON_FIELDS)
        if bad:
            raise ValueError(f"поля нельзя менять update_task: {sorted(bad)}")
        if not fields:
            return
        sets, args = [], []
        for k, v in fields.items():
            if k in _TASK_JSON_FIELDS:
                sets.append(f"{_TASK_JSON_FIELDS[k]}=?")
                args.append(_dumps(v))
            else:
                sets.append(f"{k}=?")
                args.append(v)
        sets.append("updated_at=?")
        args.append(now if now is not None else now_ms())
        args.append(int(task_id))
        sql = f"UPDATE task SET {', '.join(sets)} WHERE id=?"
        if con is not None:
            con.execute(sql, args)
            return
        with self.tx() as c:
            c.execute(sql, args)

    def dependents_of(self, task_id: int) -> list[int]:
        with self.read() as c:
            return [r[0] for r in c.execute(
                "SELECT task_id FROM task_dep WHERE after_id=? ORDER BY task_id", (int(task_id),))]

    # --- события ---

    def add_event(self, kind: Ev | str, *, task_id: int | None = None, project: str = "",
                  payload: dict | None = None, critical: bool = False, now: int | None = None,
                  con: sqlite3.Connection | None = None) -> int:
        """Событие журнала. needs_reaction — по виду (model.NEEDS_REACTION)."""
        k = Ev(kind)
        row = (now if now is not None else now_ms(), task_id, project, k.value, _dumps(payload or {}),
               1 if k in NEEDS_REACTION else 0, 1 if critical else 0)
        sql = ("INSERT INTO event(ts, task_id, project, kind, payload_json, needs_reaction, critical)"
               " VALUES(?,?,?,?,?,?,?)")
        if con is not None:
            return int(con.execute(sql, row).lastrowid)
        with self.tx() as c:
            return int(c.execute(sql, row).lastrowid)

    def events(self, *, after_id: int = 0, task_id: int | None = None,
               needs_reaction: bool | None = None, unacked: bool = False,
               limit: int | None = None) -> list[Event]:
        sql = "SELECT * FROM event WHERE id>?"
        args: list[Any] = [int(after_id)]
        if task_id is not None:
            sql += " AND task_id=?"
            args.append(int(task_id))
        if needs_reaction is not None:
            sql += " AND needs_reaction=?"
            args.append(1 if needs_reaction else 0)
        if unacked:
            sql += " AND acked_at IS NULL"
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.read() as c:
            return [Event.from_row(r) for r in c.execute(sql, args)]

    def last_event_id(self) -> int:
        with self.read() as c:
            row = c.execute("SELECT MAX(id) FROM event").fetchone()
        return int(row[0] or 0)

    # --- сессии ---

    def add_session(self, *, task_id: int | None, provider: str, role: str, model: str = "",
                    round: int = 0, pid: int | None = None, external_id: str = "",
                    log_path: str = "", now: int | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO session(task_id, provider, external_id, role, round, model, pid, started_at, log_path)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, provider, external_id, role, int(round), model, pid,
                 now if now is not None else now_ms(), log_path))
            return int(cur.lastrowid)

    def update_session(self, session_id: int, **fields: Any) -> None:
        allowed = {"external_id", "pid", "status", "outcome", "ended_at", "cost_go", "cost_usd", "quota",
                   "tokens", "log_path"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"поля сессии: {sorted(bad)}")
        if not fields:
            return
        sets, args = [], []
        for k, v in fields.items():
            if k == "tokens":
                sets.append("tokens_json=?")
                args.append(_dumps(v))
            else:
                sets.append(f"{k}=?")
                args.append(v)
        args.append(int(session_id))
        with self.tx() as c:
            c.execute(f"UPDATE session SET {', '.join(sets)} WHERE id=?", args)

    def get_session(self, session_id: int) -> Session | None:
        with self.read() as c:
            row = c.execute("SELECT * FROM session WHERE id=?", (int(session_id),)).fetchone()
        return Session.from_row(row) if row else None

    def list_sessions(self, task_id: int | None = None, *, status: str | None = None) -> list[Session]:
        sql, args = "SELECT * FROM session WHERE 1=1", []
        if task_id is not None:
            sql += " AND task_id=?"
            args.append(int(task_id))
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id"
        with self.read() as c:
            return [Session.from_row(r) for r in c.execute(sql, args)]


def _split_sql(script: str) -> list[str]:
    """Разбить миграцию на операторы (без триггеров с ; внутри — их в схеме нет)."""
    out, buf = [], []
    for line in script.splitlines():
        stripped = line.split("--", 1)[0]
        buf.append(stripped)
        if stripped.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out
