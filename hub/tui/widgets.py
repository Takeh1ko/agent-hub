"""Виджеты TUI: таблица задач и лента событий — словами для людей."""

from __future__ import annotations

import logging

from rich.text import Text  # транзитивная зависимость textual, новых записей в pyproject нет
from textual.widgets import DataTable, RichLog

from hub.read import human as hm
from hub.read.snapshot import Snapshot, TaskSnap

log = logging.getLogger(__name__)

COL_KEYS = ["pulse", "id", "title", "project", "now", "who", "since", "cost"]
COL_LABELS = ["", "Задача", "Что делаем", "Проект", "Что сейчас", "Кто работает", "В этапе", "$"]
# Узкий экран (< COMPACT_WIDTH колонок): без «Проект», короче тексты — «Что сейчас» видна без прокрутки.
COMPACT_WIDTH = 130
COMPACT_KEYS = ["pulse", "id", "title", "now", "who", "since", "cost"]
WIDE_FIT = {"title": 38, "project": 12, "now": 38, "who": 24}
COMPACT_FIT = {"title": 24, "now": 30, "who": 14}

# Цвет строки по смыслу этапа (hm.StageView.style).
ROW_STYLE = {
    "ok": "",
    "wait": "dim",
    "attention": "bold yellow",
    "error": "bold red",
    "done": "cyan",
}

# Сессия «работает сейчас», если её пульс свежее этого.
WORKING_MS = 10 * 60_000


def code_of(task_id: str) -> str:
    """«H13-hub-resilience» → «H13»: так задачи зовёт владелец."""
    return str(task_id or "").split("-", 1)[0] or str(task_id or "")


def working_now(task: TaskSnap, now_ms: int) -> str:
    """Кто работает над задачей прямо сейчас: «Spark Go», «Spark Go, MiMo Flash», «никто»."""
    names: list[str] = []
    for s in sorted(task.sessions or [], key=lambda x: -hm.to_int(x.pulse_ms)):
        if hm.to_int(now_ms) - hm.to_int(s.pulse_ms) > WORKING_MS:
            continue
        name = hm.model_name(s.model, s.provider)
        if name not in names:
            names.append(name)
    return ", ".join(names) if names else "никто"


def task_cells(task: TaskSnap, now_ms: int, compact: bool = False) -> list:
    """Ячейки строки в порядке COL_KEYS (compact — COMPACT_KEYS)."""
    view = hm.stage_view(task.stage, task.round, task.max_rounds, task.reason, task.reviewers)
    style = ROW_STYLE.get(view.style, "")
    since_ms = hm.to_int(task.stage_since_ms)
    fit = COMPACT_FIT if compact else WIDE_FIT
    values = {
        "id": code_of(task.id),
        "title": hm.fit(task.short or task.title or task.id, fit["title"]),
        "project": hm.fit(task.project or "—", WIDE_FIT["project"]),
        "now": hm.fit(view.now, fit["now"]),
        "who": hm.fit(working_now(task, now_ms), fit["who"]),
        "since": hm.ago(hm.to_int(now_ms) - since_ms) if since_ms > 0 else "—",
        "cost": hm.money(hm.to_float(task.cost_go) + hm.to_float(task.cost_usd)),
    }
    keys = COMPACT_KEYS if compact else COL_KEYS
    return [str(task.pulse or "")] + [Text(values[k], style=style) for k in keys[1:]]


def _broken_cells(task_id: str, compact: bool) -> list:
    keys = COMPACT_KEYS if compact else COL_KEYS
    cells = {"id": code_of(task_id), "now": "не читается — см. hub status"}
    return ["?"] + [Text(cells.get(k, "—"), style="red") for k in keys[1:]]


class TaskTable(DataTable):
    """Таблица задач с обновлением по ключу (без полного пересоздания)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Курсор — строка: подсветка строки сразу показывает её подробности.
        self.cursor_type = "row"
        self.zebra_stripes = True
        self._cols_ready = False
        self._keys: set[str] = set()
        self.compact = False

    @property
    def keys_now(self) -> list[str]:
        return COMPACT_KEYS if self.compact else COL_KEYS

    def set_compact(self, compact: bool) -> None:
        """Сменить набор колонок; строки перерисуются следующим update."""
        if compact == self.compact:
            return
        self.compact = compact
        self.clear(columns=True)
        self._cols_ready = False
        self._keys.clear()

    def _ensure_columns(self) -> None:
        if self._cols_ready:
            return
        labels = dict(zip(COL_KEYS, COL_LABELS))
        for key in self.keys_now:
            self.add_column(labels[key], key=key)
        self._cols_ready = True

    def _cells_safe(self, task: TaskSnap, now_ms: int) -> list:
        # Битые данные одной задачи не роняют всю таблицу — строка с пометкой.
        try:
            return task_cells(task, now_ms, self.compact)
        except Exception:
            log.exception("битая задача %s", getattr(task, "id", "?"))
            return _broken_cells(str(getattr(task, "id", "?")), self.compact)

    def update(self, snap: Snapshot) -> None:  # type: ignore[override]
        """Обновить строки по id задачи; порядок — как в снимке (свежие вверх)."""
        self._ensure_columns()
        wanted = {str(t.id): t for t in snap.tasks}
        order = list(wanted)
        current = [str(getattr(r.key, "value", r.key)) for r in self.ordered_rows]
        if current != order:
            # Состав или порядок поменялся — перестраиваем (строк единицы),
            # курсор остаётся на той же задаче.
            keep = self.selected_task_id()
            self.clear()
            self._keys.clear()
            for tid in order:
                try:
                    self.add_row(*self._cells_safe(wanted[tid], snap.now_ms), key=tid)
                    self._keys.add(tid)
                except Exception:
                    log.exception("не добавилась строка %s", tid)
            if keep in self._keys:
                try:
                    self.move_cursor(row=self.get_row_index(keep))
                except Exception:
                    log.exception("не вернулся курсор")
            return
        for tid, task in wanted.items():
            for col_key, val in zip(self.keys_now, self._cells_safe(task, snap.now_ms)):
                try:
                    # Ширину пересчитываем, иначе длинные значения
                    # обрезаются по первому кадру.
                    self.update_cell(tid, col_key, val, update_width=True)
                except Exception:
                    log.exception("не обновилась ячейка %s/%s", tid, col_key)

    def selected_task_id(self) -> str | None:
        try:
            rows = self.ordered_rows
            idx = int(self.cursor_row)
            if 0 <= idx < len(rows):
                key = rows[idx].key
                val = getattr(key, "value", key)
                return str(val) if val else None
        except Exception:
            log.exception("нет текущей строки")
        return None


# ready/arbiter/failed пишутся ВДОБАВОК к событию stage (для TG-бота). В ленте повторяем их,
# только если такой этап задачи ещё не показан (события могут прийти разными тиками).
FEED_DUPES = ("ready", "arbiter", "failed")


class EventFeed(RichLog):
    """Лента событий: «00:21  H13  Spark Go пишет код»."""

    def __init__(self, *args, max_lines: int | None = 200, **kwargs) -> None:
        # Без лимита лента растёт бесконечно за долгую сессию top.
        super().__init__(*args, max_lines=max_lines, wrap=True, **kwargs)
        self._shown_stage: dict[str, str] = {}

    def push(self, events: list[dict], tasks: dict[str, TaskSnap] | None = None) -> None:
        """Добавить строки событий; tasks — задачи снимка (для имён моделей)."""
        tasks = tasks or {}
        for ev in events or []:
            try:
                kind = str(ev.get("kind") or "")
                tid = str(ev.get("task_id") or "")
                if kind in FEED_DUPES and self._shown_stage.get(tid) == kind:
                    continue
                if kind == "stage" or kind in FEED_DUPES:
                    self._shown_stage[tid] = str(hm.event_payload(ev).get("stage") or kind).strip()
                t = tasks.get(tid)
                text = hm.event_text(ev, t.executor if t else "", t.reviewers if t else None)
                style = ("bold yellow" if ("Claude" in text or kind in ("stuck", "crashed"))
                         else "bold red" if text.startswith("ошибка") else "")
                line = Text(f"{hm.clock(int(ev.get('ts') or 0))}  ", style="dim")
                line.append(f"{code_of(tid) or 'владелец':<6}", style="bold")
                line.append(" " + text, style=style)
            except Exception:
                log.exception("битое событие")
                continue
            try:
                self.write(line)
            except Exception:
                log.exception("не записалась строка ленты")
