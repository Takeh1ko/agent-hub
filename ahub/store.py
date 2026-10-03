"""Hub storage: SQLite (WAL), short connections, migrations via PRAGMA user_version.

Rules:
- Each method opens and closes its own connection (task processes, service, and clients write concurrently).
- Writes go in a BEGIN IMMEDIATE transaction (`tx()`), so check-and-change stays atomic.
- Task state never changes here directly — only via ahub.transitions (V02b), with a log entry.
- Time comes in as a `now` parameter (ms) where tests care; otherwise — ahub.time.now_ms().
- A row becomes a dataclass by its own fields only (`from_row`): columns a newer schema added are dropped,
  so a process on the old code keeps working after a migration (a live reload adds columns under it).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from functools import lru_cache
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


@lru_cache(maxsize=None)
def _columns(cls: type) -> frozenset[str]:
    """Dataclass field names — the columns a row may bring (`from_row`)."""
    return frozenset(f.name for f in fields(cls))  # type: ignore[arg-type]


def _row_kwargs(cls: type, row: sqlite3.Row, json_cols: dict[str, str]) -> dict[str, Any]:
    """Row → dataclass kwargs: the dataclass fields and its JSON columns only.

    A column a newer schema added (a migration under a live process) is dropped, not fatal.
    json_cols — `{json column: dataclass field}`.
    """
    raw = dict(row)
    d = {k: v for k, v in raw.items() if k in _columns(cls)}
    for col, target in json_cols.items():
        d[target] = _loads(raw.get(col), {})
    return d


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
    request: str = ""  # owner request: '' | stop | nudge
    request_text: str = ""  # the text of the request (nudge message)
    after: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"T{self.id}"

    @classmethod
    def from_row(cls, row: sqlite3.Row, after: list[int] | None = None) -> "Task":
        d = _row_kwargs(cls, row, {"review_json": "review", "limits_json": "limits"})
        d["kind"] = Kind(d["kind"])
        d["state"] = State(d["state"])
        d["after"] = list(after or [])
        return cls(**d)


# Task fields allowed in a plain update (not state, not ownership).
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
        d = _row_kwargs(cls, row, {"payload_json": "payload"})
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
        d = _row_kwargs(cls, row, {"tokens_json": "tokens"})
        return cls(**d)


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # --- connections ---

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
        """Write transaction: BEGIN IMMEDIATE … COMMIT, ROLLBACK on exception."""
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

    # --- tasks ---

    def create_task(self, *, project: str, kind: Kind | str, title: str, spec: str = "",
                    spec_hash: str = "", result_format: str = "", executor: str = "",
                    review: dict | None = None, limits: dict | None = None,
                    budget_go: float = 0.0, budget_usd: float = 0.0,
                    state: State | str = State.QUEUED, created_by: str = "",
                    after: list[int] | None = None, now: int | None = None,
                    con: sqlite3.Connection | None = None) -> int:
        """Create a task + created event (in one transaction). Returns the id."""
        ts = now if now is not None else now_ms()
        kind = Kind(kind)
        state = State(state)
        if state not in (State.QUEUED, State.DRAFT):
            raise ValueError(f"new task must be queued or draft, not {state}")

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
                   project: str | None = None, projects: tuple[str, ...] | None = None,
                   limit: int | None = None,
                   newest_first: bool = False) -> list[Task]:
        """Tasks, optionally of one project (`project`) or of a scope (`projects`, ahub/scope.py)."""
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
        if projects:
            where.append(f"project IN ({','.join('?' * len(projects))})")
            args.extend(projects)
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

    def task_projects(self) -> list[str]:
        """Project names that have tasks — the hub config may not know one of them (ahub projects)."""
        with self.read() as c:
            return [str(r[0]) for r in
                    c.execute("SELECT DISTINCT project FROM task WHERE project!='' ORDER BY project")]

    def update_task(self, task_id: int, *, now: int | None = None,
                    con: sqlite3.Connection | None = None, **fields: Any) -> None:
        """Plain task fields (not state, not ownership). Unknown field — error."""
        bad = set(fields) - _TASK_PLAIN_FIELDS - set(_TASK_JSON_FIELDS)
        if bad:
            raise ValueError(f"cannot change fields update_task: {sorted(bad)}")
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

    # --- events ---

    def add_event(self, kind: Ev | str, *, task_id: int | None = None, project: str = "",
                  payload: dict | None = None, critical: bool = False, now: int | None = None,
                  con: sqlite3.Connection | None = None) -> int:
        """Log event. needs_reaction — by kind (model.NEEDS_REACTION)."""
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

    # --- sessions ---

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
            raise ValueError(f"session fields: {sorted(bad)}")
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
    """Split a migration into statements (no triggers with inner ; — the schema has none)."""
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
