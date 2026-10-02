"""Данные экрана `ahub top` — чистые функции (тестируются без textual). Всё словами, понятно не программисту."""

from __future__ import annotations

from dataclasses import dataclass, field

from ahub import archive, comms, config, events, pulse, views
from ahub.model import ACTIVE, WAITING_DECISION, Ev, State
from ahub.providers import opencode_db
from ahub.service import HEARTBEAT_KEY, PAUSE_KEY, live_workers
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms, to_local

_UNSET: object = object()  # маркер «лимит не передан — взять из конфига»
PHASE = {"studying": "изучает", "writing": "пишет", "testing": "тесты", "waiting": "ждёт"}
EV_WORDS = {"created": "создана", "retry": "повтор после сбоя", "silence": "работник молчал", "orphan": "потерян процесс",
            "budget_soft": "потрачено 80 % бюджета", "budget_hard": "бюджет исчерпан", "orch_edit": "правка Claude",
            "paths_extended": "расширены файлы", "model_changed": "сменена модель", "budget_extended": "бюджет продлён"}


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
    return f"{m} мин" if m < 60 else (f"{m // 60} ч" if m < 1440 else f"{m // 1440} д")


def _go_limit(hub_limit: float | None | object) -> float | None:
    """Лимит месяца: явный → он; _UNSET → из конфига; битого конфига — нет лимита."""
    if hub_limit is not _UNSET:
        return hub_limit  # type: ignore[return-value]
    try:
        return config.load_hub().go_month_limit
    except config.ConfigError:
        return None


def header(store: Store, live: dict[int, int], now: int, *, go_limit: float | None | object = _UNSET) -> str:
    hb = store.meta_get(HEARTBEAT_KEY)
    svc = "🟢 сервис" if hb and now - int(hb) < 30_000 else "🔴 сервис не отвечает"
    claude = "🟢 Claude на связи" if events.present(store, now=now) else "⚪ Claude не в сессии"
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
        if limit is None:
            money = f"сегодня ${today.cost_go or 0:.2f} · месяц ${go_m:.2f}"
        elif limit > 0:
            money = (f"сегодня ${today.cost_go or 0:.2f} · месяц ${go_m:.2f} из ${limit:.0f}"
                     f" ({go_m / limit * 100:.0f} %)" + (" — лимит превышен!" if go_m > limit else ""))
        else:
            money = f"сегодня ${today.cost_go or 0:.2f} · месяц ${go_m:.2f} из ${limit:.0f}"
        if today.cost_usd or month.cost_usd:
            money += f" · реальные ${month.cost_usd or 0:.2f}"
    except Exception:
        money = "траты: нет данных"
    parts = [svc, claude, f"работают {len(active)}", f"ждут решения {len(waiting)}", f"в очереди {len(queued)}"]
    if store.meta_get(PAUSE_KEY) == "1":
        parts.append("⏸ очередь на паузе")
    if alarms:
        parts.append(f"🚨 тревог {alarms}")
    return " · ".join(parts) + "\n" + money


def rows(store: Store, live: dict[int, int], pulses: dict, now: int, recent: int = 10) -> list[Row]:
    active = store.list_tasks(states=ACTIVE | WAITING_DECISION | {State.QUEUED})
    done = [t for t in store.list_tasks(newest_first=True, limit=recent * 3)
            if t.state.value in ("accepted", "rejected")][:recent]
    out = []
    for t in active + done:
        pl = pulses.get(t.id)
        mark = pl.mark if pl else {"done": "✅", "needs_decision": "❓", "error": "❌", "stopped": "⏹",
                                   "queued": "⏳", "draft": "📝", "accepted": "✔", "rejected": "✖"}.get(t.state.value, " ")
        go, usd = archive.task_cost(store, t.id)
        out.append(Row(t.id, mark, t.label, t.kind.value, t.title, archive.STATE_WORDS.get(t.state.value, t.state.value),
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
            out.append(f"{when} T{e.task_id} → {to}" + (f" ({e.payload['reason'][:60]})" if e.payload.get("reason") else ""))
        elif e.kind in EV_WORDS:
            txt = e.payload.get("text") or EV_WORDS[e.kind]
            out.append(f"{when} T{e.task_id or '-'}: {str(txt)[:90]}")
    return out[-limit:]


def detail(store: Store, task_id: int, live: dict[int, int], pulses: dict) -> str:
    t = store.get_task(task_id)
    if t is None:
        return ""
    pl = pulses.get(t.id)
    head = f"{pl.mark} {pl.reason or 'работает'}\n" if pl else ""
    return head + views.task_text(store, t, live=live)


def snapshot(store: Store, projects: list[config.ProjectConfig] | None = None) -> tuple[Screen, dict, dict]:
    now = now_ms()
    live = live_workers()
    if projects is None:
        projects, _ = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    return Screen(header(store, live, now), rows(store, live, pulses, now), feed(store)), live, pulses


HELP = """Значки: 🟢 работает · 🟡 ждёт по делу (тесты, замок, инструмент) · 🔴 молчит дольше нормы · ⚫ процесс пропал ·
⚪ нет данных · ⏳ в очереди · ✅ готово (ждёт решения Claude) · ❓ нужно решение · ❌ ошибка · ⏹ остановлена · ✔ принята

Режимы: «Просмотр» — только смотреть. «Управление» (клавиша c) — можно действовать:
  n — новая задача своими словами (модель дописывает, вы видите предпросмотр и запускаете)
  s — остановить · a — принять (код — слить) · x — отклонить · r — доработать (указания)
  m — сменить модель · b — продлить бюджет · p — пауза/запуск очереди
Всегда: ↑/↓ — выбор задачи, ? — эта справка, q — выход.
Обычно действовать не нужно: задачи ведёт Claude. Вмешивайтесь, если видите 🔴/⚫ или тревогу."""
