"""`ahub top` screen data — pure functions (tested without textual). Plain words, clear to non-programmers.

The table shows the current work (active, waiting for a decision, queued); the key `h` adds the recent
finished tasks (history) — the header says which view is on. The live transcript screen is ahub/tui/live.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ahub import archive, comms, config, events, pulse, reasons, ui, views
from ahub.i18n import Words
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION, Ev, State
from ahub.providers import opencode_db
from ahub.service import HEARTBEAT_KEY, PAUSE_KEY, live_workers
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms, to_local

_UNSET: object = object()  # marker "limit not passed — take from config"
PHASE = views.PHASE_WORDS
CURRENT = ACTIVE | WAITING_DECISION | {State.QUEUED}  # the table by default: what is going on now
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


@dataclass
class Screen:
    header: str
    rows: list[Row] = field(default_factory=list)
    feed: list[str] = field(default_factory=list)


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


def header(store: Store, live: dict[int, int], now: int, *, go_limit: float | None | object = _UNSET,
           history: bool = False) -> str:
    hb = store.meta_get(HEARTBEAT_KEY)
    svc = _t("tui.svc_on") if hb and now - int(hb) < 30_000 else _t("tui.svc_off")
    claude = _t("tui.claude_on") if events.present(store, now=now) else _t("tui.claude_off")
    active = store.list_tasks(states=ACTIVE)
    waiting = store.list_tasks(states=WAITING_DECISION)
    queued = store.list_tasks(states={State.QUEUED})
    alarms = len(comms.alarms(store))
    limit = _go_limit(go_limit)
    lt = to_local(now)
    day0 = int(lt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    month0 = int(lt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
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
    except Exception:
        money = _t("tui.money_none")
    parts = [_t("tui.view_history") if history else _t("tui.view_current"),
             svc, claude, _t("tui.working", n=len(active)), _t("tui.waiting", n=len(waiting)),
             _t("tui.queued", n=len(queued))]
    if store.meta_get(PAUSE_KEY) == "1":
        parts.append(_t("tui.paused"))
    if alarms:
        parts.append(_t("tui.alarms", n=alarms))
    return " · ".join(parts) + "\n" + money


def rows(store: Store, live: dict[int, int], pulses: dict, now: int, recent: int = 10, *,
         history: bool = False) -> list[Row]:
    """The table: by default the current work (active, waiting for a decision, queued); with history
    — the recent finished tasks as well (what the screen showed before the key `h`)."""
    active = store.list_tasks(states=CURRENT)
    done = [t for t in store.list_tasks(newest_first=True, limit=recent * 3)
            if t.state.value in ("accepted", "rejected")][:recent] if history else []
    out = []
    for t in active + done:
        pl = pulses.get(t.id)
        marks = {"done": "✅", "needs_decision": "❓", "error": "❌", "stopped": "⏹",
                 "queued": "⏳", "draft": "📝", "accepted": "✔", "rejected": "✖"}
        mark = pl.mark if pl else marks.get(t.state.value, " ")
        go, usd = archive.task_cost(store, t.id)
        state_word = archive.STATE_WORDS.get(t.state.value, t.state.value)
        out.append(Row(t.id, mark, t.label, t.kind.value, t.title, state_word,
                       PHASE.get(t.phase, "") if t.state in ACTIVE else "", t.executor, t.round,
                       _age(t.updated_at, now), f"{go + usd:.3f}"))
    return out


def feed(store: Store, limit: int = 12) -> list[str]:
    evs = store.events(after_id=max(0, store.last_event_id() - 80))
    out = []
    cache: dict[int, Task | None] = {}
    for e in evs:
        if e.kind in ("phase", "session"):
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
    return head + views.task_text(store, t, live=live)


def snapshot(store: Store, projects: list[config.ProjectConfig] | None = None, *,
             history: bool = False) -> tuple[Screen, dict, dict]:
    now = now_ms()
    live = live_workers()
    if projects is None:
        projects, _ = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    return (Screen(header(store, live, now, history=history),
                   rows(store, live, pulses, now, history=history), feed(store)), live, pulses)
