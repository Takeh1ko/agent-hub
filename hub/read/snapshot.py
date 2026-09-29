"""Картина работы агентов: задачи + сессии + пульс + $."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
    s = str(text or "").splitlines()
    s = s[0] if s else ""
    s = s.strip()
    if not s or s == "-":
        return s or "-"
    # Убрать markdown: код, жирность, заголовки, ссылки, цитаты.
    s = re.sub(r"```.*?```", " ", s)
    s = re.sub(r"`+", "", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", s)
    s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", s)
    s = re.sub(r"^#+\s*", "", s)
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^>\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rstrip()
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
    total_go: float
    total_usd: float
    now_ms: int

    def to_json(self) -> str:
        return json.dumps({
            "now_ms": self.now_ms,
            "total_go": round(self.total_go, 4),
            "total_usd": round(self.total_usd, 4),
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

    def to_text(self, limit: int = 1500) -> str:
        """Компактно, ≤ limit байт: усечение снизу."""
        head = f"$ сегодня: go {self.total_go:.2f} / usd {self.total_usd:.2f}"
        lines = [head]
        for t in self.tasks:
            cost = t.cost_go + t.cost_usd
            sess = ",".join(f"{s.role}:{s.model}" for s in t.sessions[:2]) or "-"
            last = t.last_activity[:28]
            lines.append(f"{t.pulse} {t.id} {t.stage} {sess} ${cost:.2f} {last}")
        text = "\n".join(lines)
        encoded = len(text.encode("utf-8"))
        if encoded <= limit:
            return text
        # Усекаем: сначала режем last_activity, потом число задач.
        tasks = list(self.tasks)
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

    def roster_text(self) -> str:
        """Модель → роль → задача → этап → пульс → $."""
        rows = []
        for t in self.tasks:
            for s in t.sessions:
                rows.append((s.model, s.role, t.id, t.stage, s.pulse, s.cost))
        rows.sort()
        lines = [f"{m} {r} {tid} {st} {p} ${c:.3f}" for m, r, tid, st, p, c in rows]
        return "\n".join(lines) if lines else "(пусто)"


def _day_start_ms(now_ms: int) -> int:
    local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def build(store, now_ms: int, opencode_db: str | Path | None = None,
          proc_root: str | Path = "/proc") -> Snapshot:
    """Собрать картину. store — hub.store.Store."""
    tasks = store.list_tasks(active_only=False)
    oc_by_id: dict[str, oc.OcSession] = {}
    totals_go = totals_usd = 0.0
    if opencode_db is not None and Path(opencode_db).exists():
        for s in oc.sessions(opencode_db, 0):
            oc_by_id[s.id] = s
        day0 = _day_start_ms(now_ms)
        for s in oc_by_id.values():
            if s.started_ms >= day0:
                if s.provider == "opencode-go":
                    totals_go += s.cost
                else:
                    totals_usd += s.cost
    try:
        live = pr.agent_procs(proc_root)
    except OSError:
        live = []
    snaps: list[TaskSnap] = []
    for t in tasks:
        links = store.list_sessions(t["id"])
        wt = str(t.get("worktree") or "")
        alive = [p for p in live if wt and (p.cwd == wt or p.cwd.startswith(wt + "/"))]
        pytest_kid = any(p.kind in ("pytest", "flock") for p in alive)
        # Пульс задачи — по всем её сессиям (исполнитель + ревьюеры):
        # свежий пульс — max, объяснение — если хоть одна сессия активна.
        task_oc = [oc_by_id[e["external_id"]] for e in links if e["external_id"] in oc_by_id]
        if task_oc:
            pulse_ms = max(s.pulse_ms for s in task_oc)
            active = next((s for s in task_oc if s.active_tool), None)
        else:
            pulse_ms = int(t.get("updated_at") or 0)
            active = None
        explained = bool(active) or pytest_kid
        pulse = _pulse_mark(str(t.get("stage") or ""), now_ms - pulse_ms,
                            explained, alive, pytest_kid)
        ss: list[SessionSnap] = []
        for e, s in zip(links, [oc_by_id.get(e["external_id"]) for e in links]):
            if s is None:
                ss.append(SessionSnap(e["external_id"], e["role"], e.get("model") or "?",
                                      "", pulse_ms, pulse, 0.0, False, 0, "-"))
                continue
            go = s.provider == "opencode-go"
            smark = _pulse_mark(str(t.get("stage") or ""), now_ms - s.pulse_ms,
                                bool(s.active_tool) or pytest_kid, alive, pytest_kid)
            ss.append(SessionSnap(s.id, e["role"], s.model, s.provider, s.pulse_ms,
                                  smark, s.cost, go, s.context_tokens,
                                  clean_activity(s.last_activity)))
        cost_go = sum(s.cost for s in ss if s.go)
        cost_usd = sum(s.cost for s in ss if not s.go)
        ctx = max([s.context_tokens for s in ss] + [0])
        last = next((s.last_activity for s in ss if s.last_activity != "-"), "-")
        snaps.append(TaskSnap(
            id=str(t["id"]), project=str(t.get("project") or ""),
            stage=str(t.get("stage") or ""), round=int(t.get("round") or 0),
            pulse=pulse, cost_go=cost_go, cost_usd=cost_usd,
            context=ctx, last_activity=clean_activity(last), sessions=ss,
        ))
    return Snapshot(tasks=snaps, total_go=totals_go, total_usd=totals_usd, now_ms=now_ms)


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
