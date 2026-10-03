"""Event delivery to the orchestrator and its presence (contracts §4, §6).

- Only needs_reaction events wake anyone. At once — owner_message, answer, and critical ones; the rest — batched
  over the grouping window (from the oldest undelivered).
- "Delivered" is set by wait/watch handoff; "acked" — ack (explicit, or implicit on reading the task/inbox).
- Delivered but unacked is re-offered once after REDELIVER_MS — so nothing is lost
  if the orchestrator never picked it up, without waking it with the same thing in a loop.
- Presence: wait/watch stamp `presence` at least every PRESENCE_TOUCH_S; "present" — younger than PRESENT_MS.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ahub import reasons, ui
from ahub.i18n import t
from ahub.model import Ev
from ahub.store import Event, Store, Task
from ahub.time import now_ms

GROUP_WINDOW_MS = 120_000
REDELIVER_MS = 30 * 60_000
MAX_DELIVERIES = 3  # first delivery + 2 reminders; after that — status only ("unread")
PRESENT_MS = 180_000
PRESENCE_TOUCH_S = 60
LINE_LIMIT = 200
IMMEDIATE = frozenset({Ev.OWNER_MESSAGE, Ev.ANSWER})
TASK_REACTIONS = (Ev.DONE.value, Ev.NEEDS_DECISION.value, Ev.ERROR.value)
DEFAULT_WHO = "claude"


def _deliverable(store: Store, now: int, project: str | None) -> list[Event]:
    sql = ("SELECT * FROM event WHERE needs_reaction=1 AND acked_at IS NULL"
           " AND (delivered_at IS NULL OR (delivered_at<? AND deliveries<?))")
    args: list = [now - REDELIVER_MS, MAX_DELIVERIES]
    if project is not None:
        sql += " AND (project=? OR project='')"
        args.append(project)
    sql += " ORDER BY id"
    with store.read() as c:
        return [Event.from_row(r) for r in c.execute(sql, args)]


def is_immediate(ev: Event) -> bool:
    return ev.critical or ev.kind in IMMEDIATE


def ready_batch(store: Store, *, now: int | None = None, project: str | None = None,
                window_ms: int = GROUP_WINDOW_MS) -> list[Event]:
    """What to hand out now: everything available if urgent or the oldest window expired; else empty."""
    ts = now if now is not None else now_ms()
    evs = _deliverable(store, ts, project)
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
        task_id: int | None = None, project: str | None = None, now: int | None = None) -> int:
    """Ack: by id, by kind and/or task. No filters — everything unacked. Returns the count."""
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
    if project is not None:
        sql += " AND (project=? OR project='')"
        args.append(project)
    with store.tx() as c:
        return c.execute(sql, args).rowcount


def ack_task(store: Store, task_id: int) -> int:
    """Implicit ack: the orchestrator has read the task."""
    return ack(store, kinds=TASK_REACTIONS, task_id=task_id)


def unacked(store: Store, project: str | None = None) -> list[Event]:
    sql = "SELECT * FROM event WHERE needs_reaction=1 AND acked_at IS NULL"
    args: list = []
    if project is not None:
        sql += " AND (project=? OR project='')"
        args.append(project)
    with store.read() as c:
        return [Event.from_row(r) for r in c.execute(sql + " ORDER BY id", args)]


def _watch_mark_key(who: str, project: str | None) -> str:
    return f"watch_summary:{who}:{project or ''}"


def watch_start_summary(store: Store, *, who: str = DEFAULT_WHO,
                        project: str | None = None) -> list[Event]:
    """Start summary for watch: delivered-but-unacked events not summarised before.

    Remembers the highest summarised id per consumer (who + project) in store meta,
    so a restart does not re-announce the same old unread lines. Old events stay
    unread (inbox/status still list them), they are just not announced again.
    Returns the new events to summarise (empty means stay silent).
    """
    raw = store.meta_get(_watch_mark_key(who, project))
    try:
        mark = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        mark = 0
    fresh = [e for e in unacked(store, project) if e.delivered_at is not None and e.id > mark]
    if not fresh:
        return []
    store.meta_set(_watch_mark_key(who, project), str(max(e.id for e in fresh)))
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

def touch(store: Store, who: str = DEFAULT_WHO, *, project: str = "", via: str = "", session_id: str = "",
          now: int | None = None) -> None:
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        c.execute("INSERT INTO presence(who, project, last_seen, session_id, via) VALUES(?,?,?,?,?)"
                  " ON CONFLICT(who) DO UPDATE SET project=CASE WHEN excluded.project!='' THEN excluded.project"
                  " ELSE presence.project END, last_seen=excluded.last_seen, via=excluded.via,"
                  " session_id=CASE WHEN excluded.session_id!='' THEN excluded.session_id ELSE presence.session_id END",
                  (who, project, ts, session_id, via))


def presence(store: Store, who: str = DEFAULT_WHO) -> dict | None:
    with store.read() as c:
        row = c.execute("SELECT * FROM presence WHERE who=?", (who,)).fetchone()
    return dict(row) if row else None


def present(store: Store, who: str = DEFAULT_WHO, *, now: int | None = None, fresh_ms: int = PRESENT_MS) -> bool:
    p = presence(store, who)
    ts = now if now is not None else now_ms()
    return bool(p) and ts - int(p["last_seen"]) < fresh_ms


# --- waiting ---

def wait(store: Store, *, timeout_s: float, project: str | None = None, who: str = DEFAULT_WHO,
         poll_s: float = 1.0, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Block until an event batch or timeout. Handed-out events are marked delivered."""
    deadline = clock() + timeout_s
    last_touch = -1e9
    while True:
        now_c = clock()
        if now_c - last_touch >= PRESENCE_TOUCH_S:
            touch(store, who, project=project or "", via="wait")
            last_touch = now_c
        batch = ready_batch(store, project=project)
        if batch:
            mark_delivered(store, [e.id for e in batch])
            return lines(store, batch)
        if now_c >= deadline:
            return []
        sleep(min(poll_s, max(0.0, deadline - now_c)))
