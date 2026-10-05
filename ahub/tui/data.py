"""`ahub top` screen data — pure functions (tested without textual). Plain words, clear to non-programmers.

The table shows the current work (active, waiting for a decision, queued), grouped by project — a header
row opens the group of each project when the hub serves more than one; the key `h` adds the recent finished
tasks (history) and `o` narrows the table to one project — the header says which view is on. The live
transcript screen is ahub/tui/live.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ahub import archive, comms, config, cost, events, pulse, reasons, ui, views
from ahub import log as hublog
from ahub.i18n import Words
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION, Ev, State
from ahub.providers import opencode_db
from ahub.scope import Scope
from ahub.service import HEARTBEAT_KEY, PAUSE_KEY, live_workers
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms, to_local

_log = hublog.get("tui")

_UNSET: object = object()  # marker "limit not passed — take from config"
PHASE = views.PHASE_WORDS
CURRENT = ACTIVE | WAITING_DECISION | {State.QUEUED}  # the table by default: what is going on now
GROUP_MARK = "▌"  # the header row of a project group (one row, no task under it)
EV_WORDS: Words = Words("tui.ev_", ("created", "retry", "silence", "nudge", "orphan", "budget_soft", "budget_hard",
                                    "orch_edit", "paths_extended", "model_changed", "budget_extended"))


@dataclass
class Row:
    task_id: int
    mark: str
    label: str
    kind: str
    title: str
    state: str
    phase: str
    model: str
    round: int
    age: str
    cost: str
    project: str = ""
    header: bool = False  # a project header row: the name and the money of the group, no task
    go: float = 0.0  # the raw numbers behind the cell — a group sums them, the cell is rounded once
    usd: float = 0.0


@dataclass
class Screen:
    header: str
    rows: list[Row] = field(default_factory=list)
    feed: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)  # the projects of the table — the `o` key cycles them


def _age(ms: int, now: int) -> str:
    m = max(0, (now - ms) // 60000)
    if m < 60:
        return _t("tui.age_min", m=m)
    if m < 1440:
        return _t("tui.age_h", h=m // 60)
    return _t("tui.age_d", d=m // 1440)


def _go_limit(hub_limit: float | None | object) -> float | None:
    """Month limit: explicit → it; _UNSET → from config; broken config — no limit."""
    if hub_limit is not _UNSET:
        return hub_limit  # type: ignore[return-value]
    try:
        return config.load_hub().go_month_limit
    except config.ConfigError:
        return None


def header(store: Store, now: int, *, go_limit: float | None | object = _UNSET,
           history: bool = False, only: str = "") -> str:
    """The header line: what is going on and what the machine has spent.

    `only` — the project the table is narrowed to: the counts follow it (one scope per screen), the
    hub-wide alarms are in it as everywhere. The money line does not: it is the machine's opencode.db
    against the Go month limit, and that database knows nothing about the projects of the hub.
    """
    hb = store.meta_get(HEARTBEAT_KEY)
    svc = _t("tui.svc_on") if hb and now - int(hb) < 30_000 else _t("tui.svc_off")
    claude = _t("tui.claude_on") if events.present(store, now=now) else _t("tui.claude_off")
    project = only or None
    active = store.list_tasks(states=ACTIVE, project=project)
    waiting = store.list_tasks(states=WAITING_DECISION, project=project)
    queued = store.list_tasks(states={State.QUEUED}, project=project)
    alarms = len(comms.alarms(store, scope=Scope((only,)) if only else None))
    limit = _go_limit(go_limit)
    lt = to_local(now)
    day0 = int(lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    month0 = cost.month_start(now)
    try:
        today = opencode_db.totals(day0)
        month = opencode_db.totals(month0)
        go_m = month.cost_go or 0.0
        day_go = today.cost_go or 0.0
        if limit is None:
            money = _t("tui.money", day=f"{day_go:.2f}", month=f"{go_m:.2f}")
        else:
            money = _t("tui.money_limit", day=f"{day_go:.2f}", month=f"{go_m:.2f}",
                       limit=f"{limit:.0f}", pct=f"{go_m / limit * 100:.0f}")
            if go_m > limit:
                money += _t("tui.money_over")
        if today.cost_usd or month.cost_usd:
            money += _t("tui.money_real", usd=f"{month.cost_usd or 0:.2f}")
    except Exception as e:  # the screen shows "unknown", the log says why — otherwise it is invisible
        _log.warning("opencode totals are unknown: %s", e)
        money = _t("tui.money_none")
    parts = [_t("tui.view_history") if history else _t("tui.view_current"),
             svc, claude, _t("tui.working", n=len(active)), _t("tui.waiting", n=len(waiting)),
             _t("tui.queued", n=len(queued))]
    if store.meta_get(PAUSE_KEY) == "1":
        parts.append(_t("tui.paused"))
    if alarms:
        parts.append(_t("tui.alarms", n=alarms))
    try:
        from ahub import quota
        for t in store.list_tasks(states=ACTIVE):
            if quota.is_gemini_task(store, t):
                _prov, buckets = quota.get_model_buckets(store, t.executor or "gemini-flash")
                b_5h = next((b for b in buckets if b.group == "Gemini" and b.window == "5h"), None)
                if b_5h:
                    parts.append(quota.format_5h_line(b_5h))
                    break
    except (sqlite3.Error, KeyError, ValueError, OSError):
        pass
    return " · ".join(parts) + "\n" + money


def _task_row(store: Store, t: Task, pl, now: int) -> Row:
    go, usd = archive.task_cost(store, t.id)
    marks = {"done": "✅", "needs_decision": "❓", "error": "❌", "stopped": "⏹",
             "queued": "⏳", "draft": "📝", "accepted": "✔", "rejected": "✖"}
    mark = pl.mark if pl else marks.get(t.state.value, " ")
    state_word = archive.STATE_WORDS.get(t.state.value, t.state.value)
    try:
        from ahub import views as _views

        model_s = _views.display_ref(t, store)
    except Exception:
        model_s = t.executor
    return Row(t.id, mark, t.label, t.kind.value, t.title, state_word,
               PHASE.get(t.phase, "") if t.state in ACTIVE else "", model_s, t.round,
               _age(t.updated_at, now), f"{go + usd:.3f}", t.project, go=go, usd=usd)


def _group_row(project: str, items: list[Row]) -> Row:
    """The header row of a project: its name, how many tasks are under it and what they cost.

    The money is the sum of the sessions of the rows below — the hub sessions of what is shown, never
    the machine-wide opencode.db (that one lives in the header line, against the Go month limit). It is
    summed from the raw go/usd of those rows and rounded once here: a sum of the already rounded cells
    would drift from the real one.
    """
    go = sum(r.go for r in items)
    usd = sum(r.usd for r in items)
    return Row(0, GROUP_MARK, project, "", _t("tui.group_tasks", n=len(items)), "", "", "", 0, "",
               f"{go + usd:.3f}", project, header=True, go=go, usd=usd)


def rows(store: Store, pulses: dict, now: int, recent: int = 10, *,
         history: bool = False, only: str = "") -> list[Row]:
    """The table: by default the current work (active, waiting for a decision, queued); with history
    — the recent finished tasks as well (what the screen showed before the key `h`).

    Several projects in the data — a header row opens the group of each; one project — no header (the
    name would say nothing new). `only` — one project: the rows of that project alone.
    """
    active = store.list_tasks(states=CURRENT, project=only or None)
    done = [t for t in store.list_tasks(project=only or None, newest_first=True, limit=recent * 3)
            if t.state.value in ("accepted", "rejected")][:recent] if history else []
    groups: dict[str, list[Row]] = {}
    for t in active + done:
        groups.setdefault(t.project, []).append(_task_row(store, t, pulses.get(t.id), now))
    flat = [r for items in groups.values() for r in items]
    if len(groups) < 2:
        return flat
    out: list[Row] = []
    for project, items in groups.items():
        out.append(_group_row(project, items))
        out.extend(items)
    return out


def feed(store: Store, limit: int = 12, *, scope: Scope | None = None) -> list[str]:
    """The recent events under the table, as one line each; scope — the rows of that project only
    (hub-wide ones are in every scope), so a narrowed screen is one scope end to end."""
    evs = store.events(after_id=max(0, store.last_event_id() - 80))
    out = []
    cache: dict[int, Task | None] = {}
    for e in evs:
        if e.kind in ("phase", "session") or (scope is not None and e.project not in scope):
            continue
        t = None
        if e.task_id:
            if e.task_id not in cache:
                cache[e.task_id] = store.get_task(e.task_id)
            t = cache[e.task_id]
        when = fmt_local(e.ts)
        if e.needs_reaction:
            out.append(f"{when} {events.format_line(e, t)}")
        elif e.kind == Ev.STATE.value and e.payload.get("to"):
            to = archive.STATE_WORDS.get(e.payload["to"], e.payload["to"])
            why = reasons.text(e.payload.get("reason", ""))
            out.append(f"{when} T{e.task_id} → {to}" + (f" ({ui.clip(why, 60)})" if why else ""))
        elif e.kind in EV_WORDS:
            txt = e.payload.get("text") or EV_WORDS[e.kind]
            out.append(f"{when} T{e.task_id or '-'}: {ui.clip(txt, 90)}")
    return out[-limit:]


def detail(store: Store, task_id: int, live: dict[int, int], pulses: dict) -> str:
    t = store.get_task(task_id)
    if t is None:
        return ""
    pl = pulses.get(t.id)
    head = f"{pl.mark} {pl.reason or _t('tui.working_now')}\n" if pl else ""
    return head + views.task_text(store, t, live=live, pulses=pulses)


def snapshot(store: Store, projects: list[config.ProjectConfig] | None = None, *,
             history: bool = False, only: str = "") -> tuple[Screen, dict, dict]:
    """The whole screen: the header, the rows (grouped by project, `only` — one of them), the feed.

    The filter goes down into header(), rows() and feed() as the SQL condition of the task list and the
    project test of the events, so a filtered screen is one scope: the counts of the header, the rows and
    the feed agree (a history of one project is not cut by the newer tasks of another).
    `Screen.projects` is every project that has tasks — the `o` key of the screen cycles it; it comes
    from the task table, not from the rows, so a filtered screen keeps the whole list.
    """
    now = now_ms()
    live = live_workers()
    if projects is None:
        projects, _ = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    sc = Scope((only,)) if only else None
    screen = Screen(header(store, now, history=history, only=only),
                    rows(store, pulses, now, history=history, only=only),
                    feed(store, scope=sc), store.task_projects())
    return screen, live, pulses
