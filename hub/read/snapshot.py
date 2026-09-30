"""Картина работы агентов: задачи + сессии + пульс + $."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hub.read import agy as ag
from hub.read import human as hm
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
    # Для людей (hub top, roster, TG): из карточки и task-строки hub.db.
    title: str = ""          # название задачи из заголовка карточки
    short: str = ""          # коротко для таблицы
    goal: str = ""           # первое предложение «Цели»
    executor: str = ""       # короткое имя исполнителя (muse, musefree…)
    reviewers: list[str] = field(default_factory=list)
    max_rounds: int = 0      # сколько кругов исправлений разрешено
    reason: str = ""         # причина текущего этапа (task.stage_reason)
    stage_since_ms: int = 0  # когда начался текущий этап (последнее событие stage)


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
    month_go: float = 0.0    # с 1-го числа, все сессии opencode-go — против лимита $60/мес

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
        text = (f"Сегодня: задачи хаба ${self.total_go + self.total_usd:.2f} · все проекты Go "
                f"${self.all_go:.2f} · реальные деньги сегодня ${self.all_usd:.2f}"
                f" · за месяц Go ${self.month_go:.2f} из лимита $60/мес ({limit_pct(self.month_go)})")
        if self.agy_runs:
            text += f" · Gemini: {gemini_window(self.agy_runs, self.agy_steps)}"
        return text

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

    def roster_text(self, recent_ms: int = 10 * 60_000, include_done: bool = False) -> str:
        """Для владельца: задача словами, что сейчас, кто работает, что дальше (hm.describe).

        Без include_done — только активные (как раньше); с include_done —
        и слитые/брошенные (для `roster --all` с деньгами/сессиями).
        """
        shown = list(self.tasks) if include_done else self.active_tasks()
        blocks = ["\n".join(hm.roster_lines(t, self.now_ms)) for t in shown]
        return "\n\n".join(blocks) if blocks else "Активных задач нет."


GO_LIMIT_USD = 60.0


def limit_pct(month_go: float) -> str:
    """«16%» или честно «126% — лимит превышен» (одинаково в шапке top и в TG)."""
    pct = int(round(100 * hm.to_float(month_go) / GO_LIMIT_USD))
    return f"{pct}%" if pct <= 100 else f"{pct}% — лимит превышен"


def gemini_window(runs: int, steps: int) -> str:
    return (f"{hm.plural(runs, 'запуск', 'запуска', 'запусков')} / "
            f"{hm.plural(steps, 'шаг', 'шага', 'шагов')} за 5 ч")


def _in_wt(path: str, wt: str) -> bool:
    return bool(wt) and (path == wt or path.startswith(wt.rstrip("/") + "/"))


def _task_arg_hit(args: list | None, tid: str) -> bool:
    """id задачи в аргументах процесса: точное совпадение токена.

    Подстрока запрещена: процесс чужой T10 (`hub review T10`) не даёт
    живость задаче T1. Форма `--флаг=ID` тоже считается.
    """
    t = str(tid or "").strip()
    if not t:
        return False
    for a in (args or []):
        s = str(a or "").strip()
        if not s:
            continue
        if s == t:
            return True
        if s.startswith("-") and "=" in s:
            _, _, tail = s.rpartition("=")
            if tail.strip() == t:
                return True
    return False


def _run_one_pids(proc_root: str | Path, task_id: str,
                  _scan: list[dict] | None = None) -> list[tuple[int, int]]:
    """Живые `--run-one <id>`: [(pid, started_ms)] прямым чтением cmdline.

    Дочерний слот очереди запускается как `python -m hub.commands.queue
    --run-one ID` — в agent_procs у него kind None (не opencode/agy/hub),
    поэтому через `live` он не виден. Ищем точное значение флага, а не
    подстроку: `grep T1` с чужим T10 не должен давать ложную живость.
    При _scan — только память, без чтения /proc.
    """
    out: list[tuple[int, int]] = []
    tid = str(task_id or "").strip()
    if not tid:
        return out
    if _scan is not None:
        for info in _scan:
            try:
                args = list(info.get("args") or [])
                pid = int(info.get("pid"))
                started = int(info.get("started") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if "--run-one" not in args:
                continue
            try:
                idx = args.index("--run-one")
            except ValueError:
                continue
            if idx + 1 >= len(args):
                continue
            if str(args[idx + 1]).strip() != tid:
                continue
            out.append((pid, started))
        return out
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return out
    for e in entries:
        if not e.name.isdigit():
            continue
        try:
            raw = (e / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        args = [a for a in raw.split("\x00") if a]
        if "--run-one" not in args:
            continue
        try:
            idx = args.index("--run-one")
        except ValueError:
            continue
        if idx + 1 >= len(args):
            continue
        if str(args[idx + 1]).strip() != tid:
            continue
        try:
            pid = int(e.name)
        except (TypeError, ValueError):
            continue
        try:
            started = int((e / "stat").stat().st_mtime * 1000)
        except OSError:
            started = 0
        out.append((pid, started))
    return out


def _hub_task_procs(live: list, proc_root: str | Path, task_id: str,
                     now_ms: int, _scan: list[dict] | None = None) -> list:
    """Процессы, ведущие задачу: hub_task по id + прямые --run-one.

    Работает и без worktree (ручной запуск): id задачи в аргументах
    процесса достаточно. Дубли по pid не возвращаем.
    При _scan --run-one ищется в памяти, без чтения /proc.
    """
    tid = str(task_id or "")
    found: list = []
    seen: set[int] = set()
    for p in live:
        try:
            kind = p.kind
            args = list(p.args or [])
            pid = int(p.pid)
        except (AttributeError, TypeError, ValueError):
            continue
        if kind == "hub_task" and _task_arg_hit(args, tid):
            if pid not in seen:
                seen.add(pid)
                found.append(p)
    for pid, started in _run_one_pids(proc_root, tid, _scan=_scan):
        if pid in seen:
            continue
        seen.add(pid)
        found.append(pr.Proc(pid=pid, kind="hub_task", cwd="",
                              args=["--run-one", tid],
                              started_ms=started or now_ms, children=[]))
    return found


def _exec_session_id(wt: str) -> str:
    try:
        return str(json.loads((Path(wt) / ".agent" / "state.json").read_text()).get("executor_session") or "")
    except (OSError, ValueError):
        return ""


def _day_start_ms(now_ms: int) -> int:
    local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _month_start_ms(now_ms: int) -> int:
    local = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(TZ)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp() * 1000)


def _default_agy_root() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "conversations"


def _agy_activity(conv) -> str:
    return clean_activity(f"{conv.steps} шагов, ошибок {conv.errors}")


def _done_minimal(t: dict, marks: dict, now_ms: int) -> TaskSnap:
    """Дешёвый снимок merged/dropped: без сессий и процессов."""
    tid = str(t.get("id"))
    wt = str(t.get("worktree") or "")
    try:
        info = hm.card_info(wt, str(t.get("card_path") or ""), tid)
    except (OSError, ValueError, AttributeError):
        from collections import namedtuple as _nt

        info = _nt("I", ["title", "short", "goal"])(title="", short="", goal="")
    mark = marks.get(tid) or (0, "", "")
    raw_stage = str(t.get("stage") or "").strip()
    since_ms = mark[0] if mark[2] == raw_stage else 0
    since_reason = mark[1] if mark[2] == raw_stage else ""
    pulse = FINAL_PULSE.get(raw_stage.lower(), DONE_MARK)
    return TaskSnap(
        id=tid, project=str(t.get("project") or ""),
        stage=raw_stage, round=hm.to_int(t.get("round")),
        pulse=pulse, cost_go=0.0, cost_usd=0.0,
        context=0, last_activity="-", sessions=[],
        title=info.title, short=info.short, goal=info.goal,
        executor=str(t.get("executor") or ""),
        reviewers=hm.parse_reviewers(t.get("reviewers_json")),
        max_rounds=hm.to_int(t.get("rounds")) or 2,
        reason=str(t.get("stage_reason") or "") or since_reason,
        stage_since_ms=hm.to_int(since_ms),
    )


def _header_totals(opencode_db: str | Path, day0: int, month0: int,
                   ) -> tuple[float, float, float]:
    """Шапка без деталей сессий: (all_go, all_usd, month_go) одним запросом.

    Нужна и без активных задач (пустой store всё равно показывает деньги).
    """
    import sqlite3 as _sq

    path = str(opencode_db)
    try:
        con = _sq.connect(f"file:{path}?mode=ro", uri=True)
    except _sq.Error:
        return 0.0, 0.0, 0.0
    try:
        con.row_factory = _sq.Row
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except _sq.Error:
            return 0.0, 0.0, 0.0
        if any(t not in tables for t in ("session",)):
            return 0.0, 0.0, 0.0
        try:
            rows = con.execute(
                "SELECT model, cost, time_created FROM session WHERE time_created >= ?",
                (month0,),
            ).fetchall()
        except _sq.Error:
            return 0.0, 0.0, 0.0
        all_go = all_usd = month_go = 0.0
        for model_raw, cost, started in rows:
            try:
                import json as _js

                info = _js.loads(model_raw or "{}")
            except (ValueError, TypeError):
                info = {}
            prov = str(info.get("providerID") or "")
            try:
                c = float(cost or 0.0)
                st = int(started or 0)
            except (TypeError, ValueError):
                continue
            if prov == "opencode-go" and st >= month0:
                month_go += c
            if st >= day0:
                if prov == "opencode-go":
                    all_go += c
                else:
                    all_usd += c
        return all_go, all_usd, month_go
    finally:
        try:
            con.close()
        except _sq.Error:
            pass


def build(store, now_ms: int, opencode_db: str | Path | None = None,
          proc_root: str | Path = "/proc",
          agy_root: str | Path | None = None,
          include_done: bool = False) -> Snapshot:
    """Собрать картину. store — hub.store.Store.

    Без include_done задачи merged/dropped даются дешёво (без сессий
    и процессов): детали сессий и /proc ради них не читаются, шапка
    считается лёгким запросом.
    """
    tasks_all = store.list_tasks(active_only=False)
    if include_done:
        tasks = list(tasks_all)
    else:
        tasks = [t for t in tasks_all if str(t.get("stage") or "") not in DONE_STAGES]
    oc_by_id: dict[str, oc.OcSession] = {}
    all_go = all_usd = month_go = 0.0
    day0 = _day_start_ms(now_ms)
    month0 = _month_start_ms(now_ms)
    try:
        marks = store.stage_marks() if hasattr(store, "stage_marks") else {}
    except Exception:  # чужая/старая схема — картина без «времени в этапе»
        marks = {}
    def _session_dirs() -> dict[str, str]:
        """id → directory всех сессий (лёгкий запрос для поиска непривязанных)."""
        import sqlite3 as _sq

        try:
            con = _sq.connect(f"file:{opencode_db}?mode=ro", uri=True)
        except _sq.Error:
            return {}
        try:
            try:
                return {str(r[0]): str(r[1] or "")
                        for r in con.execute("SELECT id, directory FROM session").fetchall()}
            except _sq.Error:
                return {}
        finally:
            try:
                con.close()
            except _sq.Error:
                pass

    if opencode_db is not None and Path(opencode_db).exists():
        # Шапка — всегда лёгким запросом (деньги видны и без активных задач).
        try:
            all_go, all_usd, month_go = _header_totals(opencode_db, day0, month0)
        except OSError:
            pass
        if tasks:
            # Детали — только нужных сессий: линки активных + непривязанные
            # в их worktree (старый run_task). Чужие/закрытые не тянем.
            linked_ids: set[str] = set()
            active_wts: list[str] = []
            try:
                for t in tasks:
                    try:
                        for e in store.list_sessions(str(t.get("id"))):
                            linked_ids.add(str(e.get("external_id") or ""))
                    except (OSError, ValueError, KeyError):
                        continue
                    wt = str(t.get("worktree") or "")
                    if wt:
                        active_wts.append(wt)
            except (OSError, ValueError):
                pass
            try:
                dirs = _session_dirs()
            except OSError:
                dirs = {}
            need: set[str] = set(linked_ids)
            for sid, d in dirs.items():
                if sid in need:
                    continue
                if any(_in_wt(d, wt) for wt in active_wts if wt and d):
                    need.add(sid)
            if need:
                try:
                    for s in oc.sessions(opencode_db, 0, session_ids=sorted(need)):
                        oc_by_id[s.id] = s
                except OSError:
                    pass
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
    # /proc — один проход за build; дальше всё в памяти.
    scan: list[dict] = []
    live: list = []
    if tasks:
        try:
            scan = pr.scan_all(proc_root)
        except OSError:
            scan = []
        try:
            live = pr.agent_procs(proc_root, _scan=scan)
        except OSError:
            live = []
    snaps: list[TaskSnap] = []
    totals_go = totals_usd = 0.0
    for t in tasks_all:
        if not include_done and str(t.get("stage") or "") in DONE_STAGES:
            try:
                snaps.append(_done_minimal(t, marks, now_ms))
            except (OSError, ValueError, AttributeError):
                continue
            continue
        links = [dict(e) for e in store.list_sessions(t["id"])]
        wt = str(t.get("worktree") or "")
        # Сессии без линка (старый run_task, панель «panel»): opencode-сессия в каталоге worktree — этой задачи.
        linked = {e["external_id"] for e in links}
        exec_sid = _exec_session_id(wt) if wt else ""
        for s in oc_by_id.values():
            if s.id not in linked and _in_wt(s.directory or "", wt):
                # Номер сессии исполнителя старый конвейер пишет только после её конца: пока
                # этап «exec», непривязанная сессия — исполнитель.
                in_exec = str(t.get("stage") or "").startswith("exec")
                role = "executor" if (s.id == exec_sid or (in_exec and not exec_sid)) else "reviewer"
                links.append({"external_id": s.id, "role": role, "model": s.model})
        # Жив: процесс агента работает в worktree или получил его аргументом (--dir/--worktree).
        # Плюс процессы, ведущие задачу (hub_task с id, ручной --run-one):
        # они видны и без worktree, иначе ручной запуск даёт ложные ⚫/🔴.
        tid = str(t["id"])
        alive = [p for p in live if wt and (_in_wt(p.cwd, wt) or any(_in_wt(a, wt) for a in p.args)
                                             or (p.kind == "hub_task" and _task_arg_hit(p.args, tid)))]
        hub_procs = _hub_task_procs(live, proc_root, tid, now_ms, _scan=scan)
        _alive_pids = {int(p.pid) for p in alive
                       if isinstance(getattr(p, "pid", None), int)}
        for p in hub_procs:
            try:
                pid = int(p.pid)
            except (TypeError, ValueError):
                continue
            if pid not in _alive_pids:
                _alive_pids.add(pid)
                alive.append(p)
        hub_alive = bool(hub_procs)
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
            + [int(p.started_ms) for p in hub_procs if int(getattr(p, "started_ms", 0) or 0) > 0]
        )
        if pulses:
            pulse_ms = max(pulses)
            active = next((s for s in task_oc if s.active_tool), None)
        else:
            pulse_ms = int(t.get("updated_at") or 0)
            active = None
        # Ведущий процесс (hub_task/--run-one) — объяснение живости:
        # ручной запуск без сессий иначе даёт ложный 🔴 при старом пульсе.
        explained = bool(active) or pytest_kid or bool(agy_alive) or hub_alive
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
        info = hm.card_info(wt, str(t.get("card_path") or ""), tid)
        mark = marks.get(tid) or (0, "", "")
        raw_stage = str(t.get("stage") or "").strip()
        # Время этапа — только если последнее событие stage про ЭТОТ этап; иначе «—», а не
        # время любого апдейта задачи.
        since_ms = mark[0] if mark[2] == raw_stage else 0
        since_reason = mark[1] if mark[2] == raw_stage else ""
        snaps.append(TaskSnap(
            id=str(t["id"]), project=str(t.get("project") or ""),
            stage=stage, round=hm.to_int(t.get("round")),
            pulse=pulse, cost_go=cost_go, cost_usd=cost_usd,
            context=ctx, last_activity=clean_activity(last), sessions=ss,
            title=info.title, short=info.short, goal=info.goal,
            executor=str(t.get("executor") or ""),
            reviewers=hm.parse_reviewers(t.get("reviewers_json")),
            max_rounds=hm.to_int(t.get("rounds")) or 2,  # как pipeline.common: нет колонки — 2 круга
            reason=str(t.get("stage_reason") or "") or since_reason,
            stage_since_ms=hm.to_int(since_ms),
        ))
    return Snapshot(tasks=snaps, total_go=totals_go, total_usd=totals_usd, now_ms=now_ms,
                    all_go=all_go, all_usd=all_usd,
                    agy_runs=agy_runs, agy_steps=agy_steps, month_go=month_go)


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
