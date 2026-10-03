"""Money of the hub's own sessions: what a project and a model cost (architecture §9, §12).

Only the hub session table is counted: a session is the work a task did through the hub, so its money
belongs to the project of that task. The machine-wide opencode.db counts every opencode session on the
box (Claude's own included) — that one belongs to the Go month limit in `ahub top`'s header, never to a
project's bill. Go credit and real dollars stay separate everywhere here: they are not the same money.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ahub.scope import Scope
from ahub.store import Store
from ahub.time import now_ms, to_local


@dataclass(frozen=True)
class Money:
    """What a scope spent: the Go credit and the real dollars."""

    go: float = 0.0
    usd: float = 0.0

    def __add__(self, other: "Money") -> "Money":
        return Money(self.go + other.go, self.usd + other.usd)


@dataclass(frozen=True)
class ModelSpend:
    """What one model of one project cost: sessions, Go, USD."""

    project: str
    model: str
    sessions: int
    go: float
    usd: float


def month_start(now: int | None = None) -> int:
    """The first moment of this month, local time — the "this month" of `ahub projects` and `ahub cost`."""
    lt = to_local(now if now is not None else now_ms())
    return int(lt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _condition(sc: Scope | None) -> tuple[str, list[str]]:
    """The SQL condition of a scope for money.

    Unlike `scope.where`, a session without a project of its own (no task) is nobody's bill — hub-wide
    rows belong to every scope when they are read, but not when they are paid for.
    """
    if sc is None or sc.all:
        return "", []
    return (f"t.project IN ({','.join('?' * len(sc.projects))})", list(sc.projects))


_PROJECT = "COALESCE(t.project,'')"


def _grouped(store: Store, with_model: bool, sc: Scope | None, since: int | None) -> list[sqlite3.Row]:
    """Sum the sessions of the scope by project (and by model, when asked); the priciest group first.

    The project of a session is the project of its task — a session without a task comes under "".
    Without the model the row carries a NULL, and the group is the project alone: grouping by the name
    `model` as well would bind it to the session column and split a project into one group per model.
    """
    args: list[object] = []
    sql = ("SELECT " + _PROJECT + " AS project, " + ("s.model" if with_model else "NULL") + " AS model,"
           " COUNT(*) AS n, COALESCE(SUM(s.cost_go),0) AS go, COALESCE(SUM(s.cost_usd),0) AS usd"
           " FROM session s LEFT JOIN task t ON t.id=s.task_id WHERE 1=1")
    if since is not None:
        sql += " AND s.started_at>=?"
        args.append(int(since))
    cond, cond_args = _condition(sc)
    if cond:
        sql += " AND " + cond
        args.extend(cond_args)
    sql += (" GROUP BY project, model ORDER BY go DESC, project, model" if with_model
            else " GROUP BY project ORDER BY go DESC, project")
    with store.read() as c:
        return c.execute(sql, args).fetchall()


def by_project(store: Store, *, scope: Scope | None = None, since: int | None = None) -> dict[str, Money]:
    """Go/USD per project name of the scope; sessions without a task come under the key ""."""
    return {str(r["project"]): Money(float(r["go"]), float(r["usd"]))
            for r in _grouped(store, False, scope, since)}


def by_model(store: Store, *, scope: Scope | None = None, since: int | None = None) -> list[ModelSpend]:
    """One row per project and model of the scope, the priciest first."""
    return [ModelSpend(str(r["project"]), str(r["model"]), int(r["n"]), float(r["go"]), float(r["usd"]))
            for r in _grouped(store, True, scope, since)]


def total(store: Store, *, scope: Scope | None = None, since: int | None = None) -> Money:
    """The whole scope in one number: Go and USD summed."""
    money = Money()
    for m in by_project(store, scope=scope, since=since).values():
        money += m
    return money
