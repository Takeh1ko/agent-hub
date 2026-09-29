"""Понятный русский для людей: этапы, модели, здоровье, события, время.

Один словарь на все экраны (hub top, roster, TG): владелец видит одни и те же
слова везде. Только чистые функции без I/O, кроме чтения карточки задачи
(`card_info`, с кэшем по mtime).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Yekaterinburg")


def to_int(v, default: int = 0) -> int:
    """Число из чего угодно (БД/proc/JSON): битое → default, без исключений."""
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, OverflowError):
        return default

# --- модели ---

# Короткие имена из .hub.toml / task.executor → как их зовёт владелец.
_SHORT = {
    "muse": "Spark Go",
    "musefree": "Spark бесплатный",
    "mimoflash": "MiMo Flash",
    "mimopro": "MiMo Pro",
    "mimo": "MiMo Pro",
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "gemini": "Gemini",
    "agy": "Gemini",
}


def model_name(model: str, provider: str = "") -> str:
    """«opencode-go/muse-spark-1.3-contributor» / «muse» → «Spark Go»."""
    raw = str(model or "").strip()
    if raw.lower() in _SHORT:
        return _SHORT[raw.lower()]
    m = raw.lower()
    prov = str(provider or "").lower()
    if "muse-spark" in m or m.startswith("spark"):
        free = "free" in m or prov == "opencode"
        return "Spark бесплатный" if free else "Spark Go"
    if "mimo" in m:
        return "MiMo Flash" if "flash" in m else "MiMo Pro"
    if "deepseek" in m:
        return "DeepSeek"
    if "glm" in m:
        return "GLM"
    if "gemini" in m:
        return "Gemini"
    return raw or "?"


ROLE = {
    "executor": "пишет код",
    "reviewer": "проверяет код",
    "critic": "критикует",
    "scout": "разведка",
    "repair": "чинит отчёт",
}


def role_name(role: str) -> str:
    return ROLE.get(str(role or ""), str(role or "?"))


def team(executor: str, reviewers: list[str] | None) -> str:
    """«Spark Go → проверка: Spark Go, MiMo Flash»."""
    who = model_name(executor) if executor else "—"
    revs = ", ".join(model_name(r) for r in (reviewers or []))
    return f"{who} → проверка: {revs}" if revs else who


def parse_reviewers(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        val = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


# --- этапы ---

@dataclass(frozen=True)
class StageView:
    icon: str      # значок этапа (не пульс)
    now: str       # что происходит сейчас
    next: str      # что будет дальше
    style: str     # цвет строки: ok / wait / attention / error / done


# Причины из конвейера → слова. Неизвестная причина показывается как есть.
_REASONS = {
    "панель молчит": "проверяющие не ответили",
    "панель arbiter": "проверяющие не договорились",
    "круги кончились": "кончились круги исправлений",
    "stop requested": "по команде",
    "stop владельца": "по команде",
    "no-worktree": "нет рабочей копии",
    "no-base": "нет базовой ветки",
    "no-card": "нет карточки",
    "no-head": "исполнитель не сделал коммит",
    "no-diff": "исполнитель ничего не изменил",
}

_ROUND_RE = re.compile(r"^(exec|gate|review)(?:\s+r(\d+))?$")


def reason_text(reason: str) -> str:
    r = str(reason or "").strip()
    if not r:
        return ""
    for key, text in _REASONS.items():
        if r == key or r.startswith(key + ":"):
            tail = r[len(key) + 1:].strip() if r.startswith(key + ":") else ""
            return f"{text}: {tail}" if tail else text
    if r.startswith("gate-fail"):
        return "не прошли тесты или правила: " + r.partition(":")[2].strip()
    if r.startswith("executor-fail"):
        return "исполнитель упал: " + r.partition(":")[2].strip()
    if r.startswith("preflight-fail"):
        return "не прошла проверка перед запуском: " + r.partition(":")[2].strip()
    if r.startswith("repair"):
        return "не удалось починить отчёт исполнителя: " + r.partition(":")[2].strip()
    return r


def stage_view(stage: str, round_no: int = 0, max_rounds: int = 0,
               reason: str = "", reviewers: list[str] | None = None) -> StageView:
    s = str(stage or "").strip()
    revs = ", ".join(model_name(r) for r in (reviewers or [])) or "проверяющие"
    max_rounds = to_int(max_rounds)
    of = f" из {max_rounds}" if max_rounds else ""
    m = _ROUND_RE.match(s)
    if m:
        kind = m.group(1)
        n = to_int(m.group(2)) if m.group(2) else to_int(round_no)
        circle = f" (круг {n}{of})" if n > 0 else ""  # круг неизвестен — без «круг 0»
        if kind == "exec":
            now = "Пишет код" if n <= 1 else "Исправляет замечания проверки"
            return StageView("✍", now + circle, "потом тесты и проверка кода", "ok")
        if kind == "gate":
            return StageView("🧪", "Прогоняет тесты" + circle,
                             f"потом проверка кода: {revs}", "ok")
        last = bool(max_rounds) and n >= max_rounds
        nxt = ("замечаний нет — готово; есть — решает Claude" if last
               else "замечаний нет — готово; есть — исполнитель исправляет")
        return StageView("🔍", "Проверка кода" + circle, nxt, "ok")
    why = reason_text(reason)
    table = {
        "queued": StageView("⏳", "Ждёт в очереди", "запустится, когда освободится место", "wait"),
        "preflight": StageView("🚀", "Готовится к запуску", "потом исполнитель пишет код", "ok"),
        "ready": StageView("✅", "Готово, ждёт слияния", "Claude проверит и сольёт", "done"),
        "arbiter": StageView("⚖", "Нужно решение Claude" + (f": {why}" if why else ""),
                             "Claude разберёт и решит", "attention"),
        "failed": StageView("❌", "Ошибка" + (f": {why}" if why else ""),
                            "Claude разберёт", "error"),
        "stopped": StageView("⏹", "Остановлена" + (f": {why}" if why else ""), "—", "wait"),
        "merged": StageView("✔", "Слита", "—", "done"),
        "dropped": StageView("✖", "Отменена", "—", "done"),
    }
    return table.get(s, StageView("•", s or "—", "—", "wait"))


_STEPS = ("очередь", "код", "тесты", "проверка", "готово", "слита")


def progress(stage: str) -> str:
    """Путь задачи: «✓ очередь → ✓ код → ● тесты → ○ проверка → ○ готово → ○ слита».

    Для arbiter/failed/stopped/dropped — пусто: путь прерван, это видно в «Сейчас».
    """
    s = str(stage or "").strip()
    m = _ROUND_RE.match(s)
    if m:
        idx = {"exec": 1, "gate": 2, "review": 3}[m.group(1)]
    else:
        idx = {"queued": 0, "preflight": 0, "ready": 4, "merged": len(_STEPS)}.get(s, -1)
    if idx < 0:
        return ""
    marks = ("✓" if i < idx else "●" if i == idx else "○" for i in range(len(_STEPS)))
    return " → ".join(f"{mk} {name}" for mk, name in zip(marks, _STEPS))


# --- здоровье (пульс из snapshot) ---

HEALTH = {
    "🟢": "работает",
    "🟡": "давно тихо — думает или ждёт тесты",
    "🔴": "похоже, зависла — давно ни одного действия",
    "⚫": "процесса нет — Claude разберётся",
    "✅": "готово",
    "⚖️": "ждёт решения",
    "❌": "ошибка",
    "⏹": "остановлена",
}


ALIVE = ("🟢", "🟡")  # процесс агента жив (работает или думает)


def is_alive(pulse: str) -> bool:
    """Сессия/задача с живым процессом. ⚫ (процесса нет) и 🔴 (зависла) — не «работает»."""
    return str(pulse or "") in ALIVE


def health_text(pulse: str, stage: str = "") -> str:
    if pulse == "⚫" and str(stage or "").strip() in ("queued", "preflight", "stopped"):
        return "ждёт"
    return HEALTH.get(str(pulse or ""), "")


# --- действия агента ---

_TOOLS = {
    "bash": "команда",
    "read": "читает",
    "edit": "правит",
    "write": "пишет файл",
    "grep": "ищет",
    "glob": "ищет файлы",
    "list": "смотрит каталог",
    "todowrite": "план",
    "todoread": "план",
    "task": "подзадача",
    "webfetch": "читает сайт",
}


def activity_text(text: str) -> str:
    """«bash: pytest -q» → «запускает тесты»; «read hub/x.py» → «читает hub/x.py»."""
    s = str(text or "").strip()
    if not s or s == "-":
        return ""
    if s == "думает":
        return "думает"
    low = s.lower()
    if low.startswith("bash"):
        cmd = s.partition(":")[2].strip()
        if "pytest" in cmd:
            return "запускает тесты"
        if cmd.startswith("git commit"):
            return "делает коммит"
        if cmd.startswith("git "):
            return "работает с git"
        return f"команда: {cmd}" if cmd else "команда"
    tool, sep, rest = s.partition(" ")
    tool = tool.rstrip(":").lower()
    if tool in _TOOLS:
        rest = rest.lstrip(": ").strip()
        return f"{_TOOLS[tool]} {rest}".strip()
    return s


# --- время и деньги ---

def ago(ms: int) -> str:
    """Возраст в словах: «сейчас», «3 мин», «1 ч 05 мин», «2 дн»."""
    mins = max(to_int(ms) // 60_000, 0)
    if mins < 1:
        return "сейчас"
    if mins < 60:
        return f"{mins} мин"
    if mins < 24 * 60:
        return f"{mins // 60} ч {mins % 60:02d} мин"
    return f"{mins // (24 * 60)} дн"


def clock(ts_ms: int) -> str:
    """Местное время «HH:MM» (Екатеринбург, как у владельца)."""
    ts = to_int(ts_ms)
    if ts <= 0:
        return "--:--"
    try:
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(TZ)
    except (OverflowError, OSError, ValueError):
        return "--:--"
    return dt.strftime("%H:%M")


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 запуск», «3 запуска», «5 запусков»."""
    n = to_int(n)
    k = abs(n)
    word = (one if k % 10 == 1 and k % 100 != 11
            else few if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14 else many)
    return f"{n} {word}"


def fit(text: str, limit: int) -> str:
    """Обрезать до limit символов с «…» (для узких колонок)."""
    t = str(text or "")
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def money(x: float) -> str:
    return f"${to_float(x):.2f}"


# --- карточка задачи ---

@dataclass(frozen=True)
class CardInfo:
    title: str    # полное название задачи
    short: str    # коротко для таблицы
    goal: str     # первое предложение «Цели»


_CARD_CACHE: dict[tuple[str, str], tuple[float, CardInfo]] = {}
_CARD_CACHE_MAX = 256
_H1_RE = re.compile(r"^#\s+(.+)$", re.M)
_GOAL_RE = re.compile(r"\*\*Цель\.?\*\*\s*(.+?)(?:\n\s*\n|\Z)", re.S)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _plain(text: str) -> str:
    """Без markdown-разметки карточки: `код`, **жирный**."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", str(text or "").replace("`", ""))


def short_title(title: str, limit: int = 38) -> str:
    """Первая смысловая часть названия (до «, », « (», « + », «; »), не длиннее limit."""
    t = str(title or "").strip()
    for sep in (" (", ", ", " + ", "; "):
        head = t.split(sep, 1)[0]
        if 12 <= len(head) < len(t):
            t = head
    t = _cap(t)
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def parse_card(text: str, task_id: str = "") -> CardInfo:
    m = _H1_RE.search(text or "")
    head = m.group(1).strip() if m else ""
    # «H13 — hub: повтор …» → «hub: повтор …» (id задачи на экране и так есть).
    head = re.sub(r"^[A-Za-zА-Яа-я0-9_.-]+\s+[—–-]\s+", "", head)
    title = _cap(_plain(head)) or task_id
    g = _GOAL_RE.search(text or "")
    goal = ""
    if g:
        body = _plain(re.sub(r"\s+", " ", g.group(1)).strip())
        # Первое предложение: точка + пробел + заглавная/скобка/кавычка/цифра.
        first = re.split(r"(?<=[.!?])\s+(?=[(«\"A-ZА-ЯЁ0-9])", body, maxsplit=1)[0]
        goal = first if len(first) <= 300 else first[:299].rstrip() + "…"
    return CardInfo(title=title, short=short_title(title), goal=goal)


def card_info(worktree: str, card_path: str, task_id: str = "") -> CardInfo:
    """Название и цель задачи из карточки (кэш по mtime). Нет файла — id задачи."""
    fallback = CardInfo(title=task_id, short=task_id, goal="")
    if not card_path:
        return fallback
    p = Path(card_path)
    if not p.is_absolute():
        if not worktree:
            return fallback
        p = Path(worktree) / p
    # Ключ — путь И задача: worktree с тем же путём у новой задачи не отдаст чужое название.
    key = (str(p), str(task_id))
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return fallback
    hit = _CARD_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        info = parse_card(p.read_text(encoding="utf-8", errors="replace"), task_id)
    except OSError:
        return fallback
    if len(_CARD_CACHE) >= _CARD_CACHE_MAX:
        _CARD_CACHE.clear()
    _CARD_CACHE[key] = (mtime, info)
    return info


# --- события ---

def event_payload(ev: dict) -> dict:
    raw = ev.get("payload_json", ev.get("payload"))
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def event_text(ev: dict, executor: str = "", reviewers: list[str] | None = None) -> str:
    """Событие хаба одной фразой; executor/reviewers — короткие имена задачи."""
    kind = str(ev.get("kind") or "")
    p = event_payload(ev)
    who = model_name(executor) if executor else "исполнитель"
    revs = ", ".join(model_name(r) for r in (reviewers or [])) or "проверяющие"
    if kind == "stage":
        stage = str(p.get("stage") or "").strip()
        reason = str(p.get("reason") or "")
        m = _ROUND_RE.match(stage)
        if m:
            k = m.group(1)
            n = to_int(m.group(2)) if m.group(2) else to_int(p.get("round"))
            if k == "exec":
                return f"{who} пишет код" if n <= 1 else f"{who} исправляет замечания (круг {n})"
            if k == "gate":
                return f"тесты и правила (круг {n})"
            return f"проверка кода: {revs} (круг {n})"
        simple = {
            "queued": "поставлена в очередь",
            "preflight": "готовится к запуску",
            "ready": "готово — проверка пройдена",
            "merged": "слита в рабочую ветку",
            "dropped": "отменена",
        }
        if stage in simple:
            return simple[stage]
        if stage == "arbiter":
            why = reason_text(reason)
            return "нужно решение Claude" + (f": {why}" if why else "")
        if stage == "failed":
            why = reason_text(reason)
            return "ошибка" + (f": {why}" if why else "")
        if stage == "stopped":
            return "остановлена"
        return stage or "смена этапа"
    if kind == "stuck":
        why = str(p.get("reason") or "").strip()
        return "давно тихо — возможно, зависла" + (f" ({why})" if why else "")
    if kind == "crashed":
        return "процесс агента пропал"
    if kind == "ready":
        return "готово — ждёт слияния"
    if kind == "arbiter":
        return "ждёт решения Claude"
    if kind == "failed":
        return "ошибка"
    if kind in ("budget_soft", "budget_hard"):
        what = "почти исчерпан бюджет" if kind == "budget_soft" else "бюджет исчерпан — остановлена"
        return f"{what} ({p.get('text', '')})".replace(" ()", "")
    if kind == "owner_message":
        text = re.sub(r"\s+", " ", str(p.get("text") or "")).strip()
        return f"сообщение владельца: «{text[:70]}{'…' if len(text) > 70 else ''}»"
    return kind or "событие"


# --- история задачи (экран top и TG /task) ---

def describe(task, now_ms: int, working_ms: int = 10 * 60_000) -> list[tuple[str, str]]:
    """Задача словами: [(подпись, текст)], подпись "" — строка без подписи.

    task — TaskSnap (или любой объект с теми же полями). Без I/O.
    """
    tid = str(getattr(task, "id", "") or "")
    code = tid.split("-", 1)[0] or tid
    view = stage_view(task.stage, task.round, task.max_rounds, task.reason, task.reviewers)
    out: list[tuple[str, str]] = [
        ("", f"{code} · {task.title or tid}"),
        ("", f"({tid}, проект {task.project or '—'})"),
    ]
    if task.goal:
        out.append(("Зачем", task.goal))
    now = view.now
    since = to_int(task.stage_since_ms)
    if since > 0:
        now += f" · {ago(to_int(now_ms) - since)} в этапе"
    health = health_text(task.pulse, task.stage)
    if health:
        now += f" · {task.pulse} {health}"
    out.append(("Сейчас", now))
    path = progress(task.stage)
    if path:
        out.append(("Путь", path))
    if view.next and view.next != "—":
        # Процесса нет или завис посреди работы — продолжение не гарантировано.
        stuck = (str(task.pulse or "") in ("⚫", "🔴")
                 and str(task.stage or "").strip() not in ("queued", "preflight")
                 and view.style == "ok")
        out.append(("Дальше", ("если перезапустится: " if stuck else "") + view.next))
    if task.executor or task.reviewers:
        out.append(("Команда", team(task.executor, task.reviewers)))
    fresh = []
    for x in sorted(task.sessions or [], key=lambda x: -to_int(x.pulse_ms)):
        age_ms = to_int(now_ms) - to_int(x.pulse_ms)
        # «Работает» — только свежая сессия с живым процессом: умерший 3 мин назад агент
        # не должен выглядеть работающим.
        if age_ms > working_ms or not is_alive(x.pulse):
            continue
        act = activity_text(x.last_activity)
        age = ago(age_ms)
        fresh.append(f"{x.pulse} {model_name(x.model, x.provider)} — {role_name(x.role)}"
                     + (f": {fit(act, 90)}" if act else "")
                     + f" ({'только что' if age == 'сейчас' else age + ' назад'})")
    if fresh:
        out.append(("Сейчас работают", "\n".join(fresh)))
    else:
        out.append(("Сейчас работают", "никто"))  # почему — уже сказано в «Сейчас»
    spent = money(to_float(task.cost_go) + to_float(task.cost_usd))
    agg: dict[str, list[float]] = {}
    for x in task.sessions or []:
        cur = agg.setdefault(model_name(x.model, x.provider), [0.0, 0])
        cur[0] += to_float(x.cost)
        cur[1] += 1
    if agg:
        parts = [f"{name} {money(c)} ({plural(int(n), 'запуск', 'запуска', 'запусков')})"
                 for name, (c, n) in sorted(agg.items(), key=lambda kv: -kv[1][0])]
        spent += " — " + ", ".join(parts)
    out.append(("Потрачено", spent))
    return out


def roster_lines(task, now_ms: int) -> list[str]:
    """Коротко для TG /roster — те же слова, что describe (одна история на все экраны)."""
    story = describe(task, now_ms)
    tid = str(getattr(task, "id", "") or "")
    code = tid.split("-", 1)[0] or tid
    pairs = {label: text for label, text in story if label}
    money_total = money(to_float(task.cost_go) + to_float(task.cost_usd))
    lines = [f"{task.pulse} {code} · {task.short or task.title or tid}",
             f"   {pairs.get('Сейчас', '')} · {money_total}"]
    lines.extend(f"   {ln}" for ln in pairs.get("Сейчас работают", "").splitlines())
    if pairs.get("Дальше"):
        lines.append(f"   Дальше: {pairs['Дальше']}")
    return lines
