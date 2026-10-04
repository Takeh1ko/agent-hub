"""Event delivery to the orchestrator and its presence (contracts §4, §6).

- Only needs_reaction events wake anyone. At once — owner_message, answer, and critical ones; the rest — batched
  over the grouping window (from the oldest undelivered).
- Everything an orchestrator reads is scoped by project (ahub/scope.py): `scope` is the project of its
  repository, `None` — every project (the owner). Acknowledgement is scoped the same way: `ack` in one project
  never marks the events of another read.
- "Delivered" is set by wait/watch handoff; "acked" — ack (explicit, or implicit on reading the task/inbox).
- Delivered but unacked is re-offered once after REDELIVER_MS — so nothing is lost
  if the orchestrator never picked it up, without waking it with the same thing in a loop.
- Presence: wait/watch stamp `presence` (presence_project — one row per who+project, plus the pre-006 table)
  at least every PRESENCE_TOUCH_S; "present" — younger than PRESENT_MS. A failed stamp is logged, never raised.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from ahub import config, reasons, ui
from ahub import log as hublog
from ahub.i18n import t
from ahub.model import Ev
from ahub.scope import Scope, where
from ahub.store import Event, Store, Task
from ahub.time import now_ms

_log = hublog.get("events")

GROUP_WINDOW_MS = 120_000
REDELIVER_MS = 30 * 60_000
MAX_DELIVERIES = 3  # first delivery + 2 reminders; after that — status only ("unread")
PRESENT_MS = 180_000
PRESENCE_TOUCH_S = 60
LINE_LIMIT = 200
IMMEDIATE = frozenset({Ev.OWNER_MESSAGE, Ev.ANSWER})
TASK_REACTIONS = (Ev.DONE.value, Ev.NEEDS_DECISION.value, Ev.ERROR.value)
DEFAULT_WHO = "claude"


def _deliverable(store: Store, now: int, scope: Scope | None) -> list[Event]:
    sql = ("SELECT * FROM event WHERE needs_reaction=1 AND acked_at IS NULL"
           " AND (delivered_at IS NULL OR (delivered_at<? AND deliveries<?))")
    args: list = [now - REDELIVER_MS, MAX_DELIVERIES]
    cond, extra = where(scope)
    if cond:
        sql += " AND " + cond
        args += extra
    sql += " ORDER BY id"
    with store.read() as c:
        return [Event.from_row(r) for r in c.execute(sql, args)]


def is_immediate(ev: Event) -> bool:
    return ev.critical or ev.kind in IMMEDIATE


def ready_batch(store: Store, *, now: int | None = None, scope: Scope | None = None,
                window_ms: int = GROUP_WINDOW_MS) -> list[Event]:
    """What to hand out now: everything available if urgent or the oldest window expired; else empty."""
    ts = now if now is not None else now_ms()
    evs = _deliverable(store, ts, scope)
    if not evs:
        return []
    if any(is_immediate(e) for e in evs) or ts - min(e.ts for e in evs) >= window_ms:
        return evs
    return []


def mark_delivered(store: Store, ids: list[int], *, now: int | None = None) -> None:
    if not ids:
        return
    with store.tx() as c:
        c.execute(f"UPDATE event SET delivered_at=?, deliveries=deliveries+1 WHERE id IN ({','.join('?' * len(ids))})",
                  (now if now is not None else now_ms(), *ids))


def ack(store: Store, ids: list[int] | None = None, *, kinds: tuple[str, ...] | None = None,
        task_id: int | None = None, scope: Scope | None = None, now: int | None = None) -> int:
    """Ack: by id, by kind and/or task, inside the scope. No filters — everything unacked. Returns the count."""
    sql = ("UPDATE event SET acked_at=?, delivered_at=COALESCE(delivered_at, ?)"
           " WHERE needs_reaction=1 AND acked_at IS NULL")
    ts = now if now is not None else now_ms()
    args: list = [ts, ts]
    if ids is not None:
        if not ids:
            return 0
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        args.extend(ids)
    if kinds is not None:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args.extend(kinds)
    if task_id is not None:
        sql += " AND task_id=?"
        args.append(int(task_id))
    cond, extra = where(scope)
    if cond:
        sql += " AND " + cond
        args += extra
    with store.tx() as c:
        return c.execute(sql, args).rowcount


def ack_task(store: Store, task_id: int) -> int:
    """Implicit ack: the orchestrator has read the task."""
    return ack(store, kinds=TASK_REACTIONS, task_id=task_id)


def unacked(store: Store, scope: Scope | None = None) -> list[Event]:
    sql = "SELECT * FROM event WHERE needs_reaction=1 AND acked_at IS NULL"
    args: list = []
    cond, extra = where(scope)
    if cond:
        sql += " AND " + cond
        args += extra
    with store.read() as c:
        return [Event.from_row(r) for r in c.execute(sql + " ORDER BY id", args)]


def _watch_mark_key(who: str, scope: Scope | None) -> str:
    """Where the watch summary stands — per consumer and per scope (a project does not re-announce another's)."""
    names = ",".join(sorted((scope or Scope()).projects))
    return f"watch_summary:{who}:{names}"


def watch_start_summary(store: Store, *, who: str = DEFAULT_WHO,
                        scope: Scope | None = None) -> list[Event]:
    """Start summary for watch: delivered-but-unacked events of the scope not summarised before.

    Remembers the highest summarised id per consumer (who + scope) in store meta,
    so a restart does not re-announce the same old unread lines. Old events stay
    unread (inbox/status still list them), they are just not announced again.
    Returns the new events to summarise (empty means stay silent).
    """
    raw = store.meta_get(_watch_mark_key(who, scope))
    try:
        mark = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        mark = 0
    fresh = [e for e in unacked(store, scope) if e.delivered_at is not None and e.id > mark]
    if not fresh:
        return []
    store.meta_set(_watch_mark_key(who, scope), str(max(e.id for e in fresh)))
    return fresh


# --- wakeup lines (L0) ---

# Stable English codes at line start (never translated).
EVENT_CODES: dict[Ev, str] = {
    Ev.DONE: "DONE",
    Ev.NEEDS_DECISION: "DECISION",
    Ev.ERROR: "ERROR",
    Ev.OWNER_MESSAGE: "OWNER",
    Ev.ANSWER: "ANSWER",
    Ev.ALARM: "ALARM",
}

def _reason(payload: dict) -> str:
    """The reason of a decision/error event in the reader's language — the stored blob is never shown."""
    return reasons.text(str(payload.get("reason") or ""))


def _money(p: dict) -> str:
    go, usd = p.get("cost_go") or 0, p.get("cost_usd") or 0
    parts = []
    if go:
        parts.append(f"${go:.2f}")
    if usd:
        parts.append(t("events.real", usd=f"{usd:.2f}"))
    return ", ".join(parts)


def format_line(ev: Event, task: Task | None) -> str:
    p = ev.payload or {}
    k = Ev(ev.kind)
    if task is not None:
        head = f"{task.label} {task.kind.value} «{ui.clip(task.title, 50)}»"
    else:
        head = f"T{ev.task_id}" if ev.task_id else ""
    if k is Ev.DONE:
        extra = []
        if p.get("report_bytes"):
            extra.append(t("events.report", kb=f"{p['report_bytes'] / 1024:.1f}"))
        if p.get("summary"):
            extra.append(ui.clip(p["summary"], 70))
        m = _money(p)
        if m:
            extra.append(m)
        line = f"{EVENT_CODES[k]} {head}" + (" — " + "; ".join(extra) if extra else "")
    elif k is Ev.NEEDS_DECISION:
        line = f"{EVENT_CODES[k]} {head} — {ui.clip(_reason(p), 100)}"
    elif k is Ev.ERROR:
        line = f"{EVENT_CODES[k]} {head} — {ui.clip(_reason(p), 100)}"
    elif k is Ev.OWNER_MESSAGE:
        line = f"{EVENT_CODES[k]} «{ui.clip(p.get('text', ''), 160)}»"
    elif k is Ev.ANSWER:
        line = (f"{EVENT_CODES[k]} #{p.get('question_id', '?')} «{ui.clip(p.get('question', ''), 60)}»"
                f" → {ui.clip(p.get('answer', ''), 60)}")
    elif k is Ev.ALARM:
        line = (f"{EVENT_CODES[k]}! " if ev.critical else f"{EVENT_CODES[k]} ") + ui.clip(p.get("text", ""), 150)
    else:
        line = f"{k.value.upper()} {head}"
    return line[:LINE_LIMIT]


def lines(store: Store, evs: list[Event]) -> list[str]:
    cache: dict[int, Task | None] = {}
    out = []
    for e in evs:
        t = None
        if e.task_id:
            if e.task_id not in cache:
                cache[e.task_id] = store.get_task(e.task_id)
            t = cache[e.task_id]
        out.append(format_line(e, t))
    return out


# --- presence ---
# One row per (who, project) in presence_project: an orchestrator session works in one repository, so a live
# session in A says nothing about B. The owner's scope (every project) stamps a row for every project.
# The pre-006 table `presence` (one row per who) is written too and read as a fallback — a process on the
# previous code still stamps only it, and must neither break nor look absent (migration 006 is additive).

NEW_SQL = ("INSERT INTO presence_project(who, project, last_seen, session_id, via) VALUES(?,?,?,?,?)"
           " ON CONFLICT(who, project) DO UPDATE SET last_seen=excluded.last_seen, via=excluded.via,"
           " session_id=CASE WHEN excluded.session_id!='' THEN excluded.session_id"
           " ELSE presence_project.session_id END")

LEGACY_SQL = ("INSERT INTO presence(who, project, last_seen, session_id, via) VALUES(?,?,?,?,?)"
              " ON CONFLICT(who) DO UPDATE SET project=CASE WHEN excluded.project!='' THEN excluded.project"
              " ELSE presence.project END, last_seen=excluded.last_seen, via=excluded.via,"
              " session_id=CASE WHEN excluded.session_id!='' THEN excluded.session_id"
              " ELSE presence.session_id END")


def touch(store: Store, who: str = DEFAULT_WHO, *, project: str = "", via: str = "", session_id: str = "",
          now: int | None = None) -> None:
    """Stamp the presence of one project — the legacy one-project entry point (`touch_scope` is the
    general one: a scope, the owner's names, both tables)."""
    touch_scope(store, Scope((project,)), who, via=via, session_id=session_id, now=now)


def presence_projects(scope: Scope | None = None) -> list[str]:
    """The names a scope stamps: its own project(s), or every project of the hub (the owner).

    Resolved once per wait/watch (`names=`) — not on every touch.
    """
    own = (scope or Scope()).projects
    if own:
        return list(own)
    try:
        projects, _ = config.load_projects()
    except (config.ConfigError, OSError):
        return [""]
    return [p.name for p in projects] or [""]


def touch_scope(store: Store, scope: Scope | None = None, who: str = DEFAULT_WHO, *, via: str = "",
                session_id: str = "", names: list[str] | None = None, now: int | None = None) -> None:
    """Presence for the scope's projects (or the given names).

    Legacy row is stamped in its own transaction first so that if presence_project is missing
    (e.g. pre-006 schema during migration), the legacy write succeeds.
    Never raises: a presence stamp must not kill a stream (`ahub watch`) — a failure goes to the log.
    """
    ts = now if now is not None else now_ms()
    target_names = presence_projects(scope) if names is None else names
    try:
        with store.tx() as c:
            for name in target_names:
                c.execute(LEGACY_SQL, (who, name, ts, session_id, via))
    except sqlite3.Error as e:
        _log.warning("legacy presence touch failed: %s", e)
    try:
        with store.tx() as c:
            for name in target_names:
                c.execute(NEW_SQL, (who, name, ts, session_id, via))
    except sqlite3.Error as e:
        _log.warning("presence touch failed: %s", e)


def presence(store: Store, who: str = DEFAULT_WHO, project: str | None = None) -> dict | None:
    """The presence row of one project; project=None — the freshest of any project.

    Reads both presence_project and the old table and takes the freshest last_seen. Its row with
    project='' is that code's owner-mode stream — it counts for every project.
    """
    sql = "SELECT * FROM presence_project WHERE who=?"
    args: list = [who]
    if project is not None:
        sql += " AND project=?"
        args.append(project)
    with store.read() as c:
        try:
            p_row = c.execute(sql + " ORDER BY last_seen DESC, project LIMIT 1", args).fetchone()
        except sqlite3.Error:
            p_row = None
        leg_row = _legacy_presence(c, who, project)
        if p_row is not None and leg_row is not None:
            row = p_row if p_row["last_seen"] >= leg_row["last_seen"] else leg_row
        else:
            row = p_row if p_row is not None else leg_row
    return dict(row) if row else None


def _legacy_presence(c: sqlite3.Connection, who: str, project: str | None) -> sqlite3.Row | None:
    """The pre-006 row of `who`: one row per who, so it answers "is anyone live" and where that one worked last.

    That code stamped one row, so an owner-mode stream left project='' — that session covers every project and
    must not look absent in any of them during the transition; a row with a name answers only for that project.
    """
    try:
        row = c.execute("SELECT * FROM presence WHERE who=?", (who,)).fetchone()
    except sqlite3.Error:
        return None
    if row is not None and project is not None:
        name = str(row["project"])
        if name and name != project:
            return None
    return row


def present(store: Store, who: str = DEFAULT_WHO, *, project: str | None = None, now: int | None = None,
            fresh_ms: int = PRESENT_MS) -> bool:
    """A live session: in that project, or (project=None) in any of them."""
    p = presence(store, who, project)
    ts = now if now is not None else now_ms()
    return bool(p) and ts - int(p["last_seen"]) < fresh_ms


# --- waiting ---

def wait(store: Store, *, timeout_s: float, scope: Scope | None = None, who: str = DEFAULT_WHO,
         poll_s: float = 1.0, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Block until an event batch of the scope or timeout. Handed-out events are marked delivered."""
    deadline = clock() + timeout_s
    last_touch = -1e9
    names = presence_projects(scope)  # the owner's projects are read from the config once, not per touch
    while True:
        now_c = clock()
        if now_c - last_touch >= PRESENCE_TOUCH_S:
            touch_scope(store, scope, who, via="wait", names=names)
            last_touch = now_c
        batch = ready_batch(store, scope=scope)
        if batch:
            mark_delivered(store, [e.id for e in batch])
            return lines(store, batch)
        if now_c >= deadline:
            return []
        sleep(min(poll_s, max(0.0, deadline - now_c)))
