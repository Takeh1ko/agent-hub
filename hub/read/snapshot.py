"""Картина работы агентов: задачи + сессии + пульс + $."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hub.read import agy as ag
from hub.read import opencode as oc
from hub.read import procs as pr

TZ = ZoneInfo("Asia/Yekaterinburg")

GREEN_MS = 2 * 60_000
EXEC_MS = 20 * 60_000
REVIEW_MS = 15 * 60_000
PYTEST_MS = 30 * 60_000

DONE_STAGES = ("merged", "dropped")
DONE_MARK = "✅"

# Финальные этапы — свои значки, без ложного 🔴.
FINAL_PULSE = {
    "ready": "✅",
    "merged": "✅",
    "dropped": "✅",
    "arbiter": "⚖️",
    "failed": "❌",
    "stopped": "⏹",
}
# Этапы без процесса в норме: 🔴 там — ложный, только ⚫.
QUIET_NO_RED = ("queued", "preflight")


def clean_activity(text: str, limit: int = 60) -> str:
    """Последнее действие без markdown, ≤ limit символов.

    Первая строка до смысла: парные маркеры снимаются,
    одиночные _/* внутри слов (snake_case, пути, арифметика) целы.
    """
    parts = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    s = parts[0] if parts else ""
    if s.endswith(":") and len(parts) > 1:  # «bash:» + команда на следующей строке
        s = f"{s} {parts[1]}"
    s = re.sub(r"^\d+[.)]\s+", "", s)  # «1. Сделано» → «Сделано»
    if not s or s == "-":
        return s or "-"
    # Убрать markdown: код, жирность, заголовки, ссылки, цитаты.
    s = re.sub(r"```.*?```", " ", s)
    s = re.sub(r"`+", "", s)
    # Парные маркеры — тоже только на границах слов и не после разделителя пути:
    # «hub/__init__.py», «2**3**2», «a__b__c» остаются как есть.
    s = re.sub(r"(?<![\w/\\])\*\*(\S(?:.*?\S)?)\*\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<![\w/\\])__(\S(?:.*?\S)?)__(?![\w.])", r"\1", s)
    s = re.sub(r"(?<![\w/\\])~~(\S(?:.*?\S)?)~~(?!\w)", r"\1", s)
    s = re.sub(r"(?<![\w/\\*])\*(?![*\s])(.+?)(?<![*\s])\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<![\w/\\_])_(?![_\s])(.+?)(?<![_\s])_(?![\w_.])", r"\1", s)
    s = re.sub(r"^#+\s*", "", s)
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^>\s*", "", s)
    # Непарные остатки разметки у края слова (не «2**3», не «__init__»).
    s = re.sub(r"(?<!\w)(\*\*|~~)|(\*\*|~~)(?!\w)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return s or "-"


def stage_threshold(stage: str) -> int:
    s = stage.lower()
    if s.startswith("review"):
        return REVIEW_MS
    if s.startswith("gate") or s.startswith("preflight"):
        # Там идут тесты приёмки — как pytest.
        return PYTEST_MS
    if "pytest" in s:
        return PYTEST_MS
    return EXEC_MS


@dataclass
class SessionSnap:
    external_id: str
    role: str
    model: str
    provider: str
    pulse_ms: int
    pulse: str
    cost: float
    go: bool
    context_tokens: int
    last_activity: str


@dataclass
class TaskSnap:
    id: str
    project: str
    stage: str
    round: int
    pulse: str
    cost_go: float
    cost_usd: float
    context: int
    last_activity: str
    sessions: list[SessionSnap] = field(default_factory=list)


@dataclass
class Snapshot:
    tasks: list[TaskSnap]
    total_go: float          # сегодня, только сессии задач hub (подписка Go, «по прайсу»)
    total_usd: float         # сегодня, только сессии задач hub (реальные деньги)
    now_ms: int
    all_go: float = 0.0      # сегодня, все сессии opencode (все проекты) — для лимита подписки
    all_usd: float = 0.0
    agy_runs: int = 0        # запусков agy за 5 ч (окно квоты, не деньги)
    agy_steps: int = 0       # шагов agy за 5 ч

    def to_json(self) -> str:
        return json.dumps({
            "now_ms": self.now_ms,
            "total_go": round(self.total_go, 4),
            "total_usd": round(self.total_usd, 4),
            "agy_runs": self.agy_runs,
            "agy_steps": self.agy_steps,
            "tasks": [{
                "id": t.id, "project": t.project, "stage": t.stage,
                "round": t.round, "pulse": t.pulse,
                "cost_go": round(t.cost_go, 4), "cost_usd": round(t.cost_usd, 4),
                "context": t.context, "last_activity": t.last_activity,
                "sessions": [{
                    "id": s.external_id, "role": s.role, "model": s.model,
                    "provider": s.provider, "pulse": s.pulse,
                    "cost": round(s.cost, 4), "context": s.context_tokens,
                    "last": s.last_activity,
                } for s in t.sessions],
            } for t in self.tasks],
        }, ensure_ascii=False)

    def head_text(self) -> str:
        return (f"Итого сегодня: задачи Go ${self.total_go:.2f} · все проекты Go ${self.all_go:.2f}"
                f" (лимит Go $60/мес) · реальные ${self.all_usd:.2f}"
                f" · Gemini: {self.agy_runs} запусков / {self.agy_steps} шагов за 5 ч")

    def active_tasks(self) -> list["TaskSnap"]:
        return [t for t in self.tasks if t.stage not in DONE_STAGES]

    def to_text(self, limit: int = 1500, include_done: bool = False) -> str:
        """Компактно, ≤ limit байт: усечение снизу. Слитые/брошенные — только при include_done."""
        head = self.head_text()
        lines = [head]
        shown = list(self.tasks) if include_done else self.active_tasks()
        for t in shown:
            cost = t.cost_go + t.cost_usd
            sess = ",".join(f"{s.role}:{s.model}" for s in t.sessions[:2]) or "-"
            last = t.last_activity[:28]
            lines.append(f"{t.pulse} {t.id} {t.stage} {sess} ${cost:.2f} {last}")
        text = "\n".join(lines)
        encoded = len(text.encode("utf-8"))
        if encoded <= limit:
            return text
        # Усекаем: сначала режем last_activity, потом число задач.
        tasks = list(shown)
        while tasks:
            probe = [head] + [
                f"{t.pulse} {t.id} {t.stage} "
                f"{','.join(f'{s.role}:{s.model}' for s in t.sessions[:2]) or '-'} "
                f"${t.cost_go + t.cost_usd:.2f}"
                for t in tasks
            ]
            text = "\n".join(probe)
            if len(text.encode("utf-8")) <= limit:
                return text
            tasks.pop()
        return head.encode("utf-8")[:limit].decode("utf-8", "ignore")

    def roster_text(self, recent_ms: int = 10 * 60_000) -> str:
        """«Сотрудники» по задачам: задача (пульс, этап, $), под ней — кто работал за последние recent_ms."""
        blocks = []
        for t in self.active_tasks():
            head = f"{t.pulse} {t.id} — {STAGE_RU(t.stage)} · ${t.cost_go + t.cost_usd:.2f}"
            rows = []
            for s in sorted(t.sessions, key=lambda x: -x.pulse_ms):
                age = self.now_ms - s.pulse_ms
                if age > recent_ms:
                    continue
                rows.append(f"   {pretty_model(s.model, s.provider)} · {ROLE_RU.get(s.role, s.role)} · {_ago(age)}"
                            + (f" · {s.last_activity}" if s.last_activity not in ("", "-") else ""))
            if not rows:
                last = max((s.pulse_ms for s in t.sessions), default=0)
                rows.append(f"   ждёт (тесты/замок/очередь), пульс {_ago(self.now_ms - last) if last else '—'}")
            blocks.append("\n".join([head, *rows]))
        return "\n\n".join(blocks) if blocks else "Активных задач нет."


def STAGE_RU(stage: str) -> str:
    s = str(stage or "")
    m = re.match(r"^(exec|review|gate) r(\d+)$", s)
    if m:
        return {"exec": "пишет код", "review": "ревью", "gate": "тесты"}[m.group(1)] + f", круг {m.group(2)}"
    return {"queued": "в очереди", "preflight": "предполёт", "ready": "готово к слиянию", "arbiter": "ждёт Claude",
            "failed": "провал", "stopped": "остановлена", "merged": "слита", "dropped": "брошена"}.get(s, s)


ROLE_RU = {"executor": "исполнитель", "reviewer": "ревьюер", "critic": "критик",
           "scout": "разведчик", "repair": "починка"}


def pretty_model(model: str, provider: str = "") -> str:
    m = (model or "?").lower()
    name = ("Spark 1.3" if "muse-spark" in m else "MiMo Flash" if "mimo" in m and "flash" in m
            else "MiMo Pro" if "mimo" in m else "DeepSeek" if "deepseek" in m
            else "GLM" if "glm" in m else "Gemini" if "gemini" in m else model or "?")
    if "free" in m or (provider == "opencode" and "spark" in name.lower()):
        name += " free"
    return name


def _ago(ms: int) -> str:
    mins = max(int(ms // 60_000), 0)
    return "сейчас" if mins < 1 else f"{mins} мин назад" if mins < 60 else f"{mins // 60} ч {mins % 60} мин назад"


def _in_wt(path: str, wt: str) -> bool:
    return bool(wt) and (path == wt or path.startswith(wt.rstrip("/") + "/"))


def _exec_session_id(wt: str) -> str:
    try:
        return str(json.loads((Path(wt) / ".agent" / "state.json").read_text()).get("executor_session") or "")
    except (OSError, ValueError):
        return ""


def _day_start_ms(now_ms: int) -> int:
    local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _default_agy_root() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "conversations"


def _agy_activity(conv) -> str:
    return clean_activity(f"{conv.steps} шагов, ошибок {conv.errors}")


def build(store, now_ms: int, opencode_db: str | Path | None = None,
          proc_root: str | Path = "/proc",
          agy_root: str | Path | None = None) -> Snapshot:
    """Собрать картину. store — hub.store.Store."""
    tasks = store.list_tasks(active_only=False)
    oc_by_id: dict[str, oc.OcSession] = {}
    all_go = all_usd = 0.0
    day0 = _day_start_ms(now_ms)
    if opencode_db is not None and Path(opencode_db).exists():
        for s in oc.sessions(opencode_db, 0):
            oc_by_id[s.id] = s
        for s in oc_by_id.values():
            if s.started_ms >= day0:
                if s.provider == "opencode-go":
                    all_go += s.cost
                else:
                    all_usd += s.cost
    # agy: окно 5 ч для шапки + привязка разговоров к задачам.
    # Метрика окна одна — window_usage (карточка H08 п.1), без дублей.
    agy_dir: str | Path | None = agy_root if agy_root is not None else _default_agy_root()
    if isinstance(agy_dir, str) and not agy_dir.strip():
        agy_dir = None
    agy_by_id: dict[str, ag.AgyConv] = {}
    agy_runs = agy_steps = 0
    try:
        if agy_dir is not None and Path(agy_dir).is_dir():
            for c in ag.conversations(agy_dir, 0):
                agy_by_id[c.id] = c
            agy_runs, agy_steps = ag.window_usage(agy_dir, now_ms, 5)
    except OSError:
        pass
    try:
        live = pr.agent_procs(proc_root)
    except OSError:
        live = []
    snaps: list[TaskSnap] = []
    totals_go = totals_usd = 0.0
    for t in tasks:
        links = [dict(e) for e in store.list_sessions(t["id"])]
        wt = str(t.get("worktree") or "")
        # Сессии без линка (старый run_task, панель «panel»): opencode-сессия в каталоге worktree — этой задачи.
        linked = {e["external_id"] for e in links}
        exec_sid = _exec_session_id(wt) if wt else ""
        for s in oc_by_id.values():
            if s.id not in linked and _in_wt(s.directory or "", wt):
                links.append({"external_id": s.id, "role": "executor" if s.id == exec_sid else "reviewer",
                              "model": s.model})
        # Жив: процесс агента работает в worktree или получил его аргументом (--dir/--worktree).
        alive = [p for p in live if wt and (_in_wt(p.cwd, wt) or any(_in_wt(a, wt) for a in p.args))]
        pytest_kid = any(p.kind in ("pytest", "flock") for p in alive)
        agy_alive = [p for p in alive if p.kind == "agy"]
        # Пульс задачи — по всем её сессиям (исполнитель + ревьюеры):
        # свежий пульс — max, объяснение — если хоть одна сессия активна.
        # Живой процесс в пульс не подмешивается (как в ветке opencode):
        # mtime файла — пульс (карточка H08), возраст живого, но молчащего
        # agy честно даёт 🟡, а не вечный 🟢.
        task_oc = [oc_by_id[e["external_id"]] for e in links if e["external_id"] in oc_by_id]
        task_agy = [agy_by_id[str(e["external_id"])] for e in links
                    if str(e["external_id"]) in agy_by_id]
        pulses = (
            [s.pulse_ms for s in task_oc]
            + [c.pulse_ms for c in task_agy]
            + [p.started_ms for p in agy_alive]
        )
        if pulses:
            pulse_ms = max(pulses)
            active = next((s for s in task_oc if s.active_tool), None)
        else:
            pulse_ms = int(t.get("updated_at") or 0)
            active = None
        explained = bool(active) or pytest_kid or bool(agy_alive)
        pulse = _pulse_mark(str(t.get("stage") or ""), now_ms - pulse_ms,
                            explained, alive, pytest_kid)
        ss: list[SessionSnap] = []
        for e in links:
            cid = str(e["external_id"])
            s = oc_by_id.get(cid)
            if s is not None:
                go = s.provider == "opencode-go"
                smark = _pulse_mark(str(t.get("stage") or ""), now_ms - s.pulse_ms,
                                    bool(s.active_tool) or pytest_kid, alive, pytest_kid)
                ss.append(SessionSnap(s.id, e["role"], s.model, s.provider, s.pulse_ms,
                                      smark, s.cost, go, s.context_tokens,
                                      clean_activity(s.last_activity)))
                continue
            c = agy_by_id.get(cid)
            if c is not None:
                smark = _pulse_mark(str(t.get("stage") or ""), now_ms - c.pulse_ms,
                                    bool(agy_alive) or pytest_kid, alive, pytest_kid)
                ss.append(SessionSnap(c.id, e["role"], "Gemini", "gemini", c.pulse_ms,
                                      smark, 0.0, False, 0, _agy_activity(c)))
                continue
            if str(e.get("tool") or "") == "agy":
                # Линк agy без файла разговора — модель известна, денег 0.
                smark = _pulse_mark(str(t.get("stage") or ""), now_ms - pulse_ms,
                                    bool(agy_alive) or pytest_kid, alive, pytest_kid)
                ss.append(SessionSnap(cid, e["role"], "Gemini", "gemini",
                                      pulse_ms, smark, 0.0, False, 0, "-"))
                continue
            ss.append(SessionSnap(e["external_id"], e["role"], e.get("model") or "?",
                                  "", pulse_ms, pulse, 0.0, False, 0, "-"))
        # agy-процесс в worktree без линка — тоже сессия задачи (модель Gemini).
        # Пульс синтетики — старт процесса из /proc (wall-time, Н6), не now_ms:
        # иначе живая, но молчащая задача вечно 🟢.
        have_agy = any(s.model == "Gemini" for s in ss)
        if agy_alive and not have_agy:
            for p in agy_alive:
                smark = _pulse_mark(str(t.get("stage") or ""), now_ms - p.started_ms,
                                    True, alive, pytest_kid)
                ss.append(SessionSnap(f"agy-{p.pid}", "executor", "Gemini", "gemini",
                                      p.started_ms, smark, 0.0, False, 0, "agy работает"))
            pulse_ms = max([pulse_ms] + [p.started_ms for p in agy_alive])
            pulse = _pulse_mark(str(t.get("stage") or ""), now_ms - pulse_ms,
                                explained, alive, pytest_kid)
        stage = str(t.get("stage") or "")
        if stage.startswith("exec") and ss:
            newest = max(ss, key=lambda x: x.pulse_ms)
            if newest.role == "reviewer" and now_ms - newest.pulse_ms < GREEN_MS * 5:
                stage = "review" + stage[4:]  # ревьюеры уже работают, лог появится в конце
        cost_go = sum(s.cost for s in ss if s.go)
        cost_usd = sum(s.cost for s in ss if not s.go)
        for e in links:
            s = oc_by_id.get(e["external_id"])
            if s is not None and s.started_ms >= day0:
                if s.provider == "opencode-go":
                    totals_go += s.cost
                else:
                    totals_usd += s.cost
        ctx = max([s.context_tokens for s in ss] + [0])
        last = next((s.last_activity for s in ss if s.last_activity != "-"), "-")
        snaps.append(TaskSnap(
            id=str(t["id"]), project=str(t.get("project") or ""),
            stage=stage, round=int(t.get("round") or 0),
            pulse=pulse, cost_go=cost_go, cost_usd=cost_usd,
            context=ctx, last_activity=clean_activity(last), sessions=ss,
        ))
    return Snapshot(tasks=snaps, total_go=totals_go, total_usd=totals_usd, now_ms=now_ms,
                    all_go=all_go, all_usd=all_usd,
                    agy_runs=agy_runs, agy_steps=agy_steps)


def _pulse_mark(stage: str, age_ms: int, explained: bool, alive: list,
                pytest_kid: bool = False) -> str:
    mark = FINAL_PULSE.get(str(stage or "").lower())
    if mark is not None:
        return mark
    if stage in DONE_STAGES:
        return DONE_MARK
    # Порог — по факту pytest/flock-ребёнка, не по имени этапа.
    threshold = PYTEST_MS if pytest_kid else stage_threshold(stage)
    if not alive:
        # Процесса нет и этап не финальный → упал, но свежий пульс
        # без процесса считаем зависшим, а не упавшим (процесс мог
        # завершиться штатно между опросами).
        # Тихие этапы (очередь/предполёт) без процесса — норма, не 🔴.
        if str(stage or "").lower() in QUIET_NO_RED:
            return "⚫"
        if age_ms >= threshold and not explained:
            return "🔴"
        return "⚫"
    if age_ms < GREEN_MS:
        return "🟢"
    if explained:
        return "🟡"
    if age_ms >= threshold:
        return "🔴"
    return "🟡"
