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
    """Кто работает над задачей прямо сейчас: «Spark Go», «Spark Go, MiMo Flash»."""
    names: list[str] = []
    for s in sorted(task.sessions or [], key=lambda x: -x.pulse_ms):
        if now_ms - s.pulse_ms > WORKING_MS:
            continue
        name = hm.model_name(s.model, s.provider)
        if name not in names:
            names.append(name)
    return ", ".join(names) if names else "—"


def task_cells(task: TaskSnap, now_ms: int) -> list:
    view = hm.stage_view(task.stage, task.round, task.max_rounds, task.reason, task.reviewers)
    style = ROW_STYLE.get(view.style, "")
    since = hm.ago(now_ms - task.stage_since_ms) if task.stage_since_ms else "—"
    cells = [
        str(task.pulse or ""),
        code_of(task.id),
        hm.fit(task.short or task.title or task.id, 38),
        hm.fit(task.project or "—", 12),
        hm.fit(view.now, 38),
        hm.fit(working_now(task, now_ms), 24),
        since,
        hm.money((task.cost_go or 0.0) + (task.cost_usd or 0.0)),
    ]
    return [cells[0]] + [Text(str(c), style=style) for c in cells[1:]]


class TaskTable(DataTable):
    """Таблица задач с обновлением по ключу (без полного пересоздания)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Курсор — строка: подсветка строки сразу показывает её подробности.
        self.cursor_type = "row"
        self.zebra_stripes = True
        self._cols_ready = False
        self._keys: set[str] = set()

    def _ensure_columns(self) -> None:
        if self._cols_ready:
            return
        for label, key in zip(COL_LABELS, COL_KEYS):
            self.add_column(label, key=key)
        self._cols_ready = True

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
                    self.add_row(*task_cells(wanted[tid], snap.now_ms), key=tid)
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
            for col_key, val in zip(COL_KEYS, task_cells(task, snap.now_ms)):
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


# События, которые дублируют смену этапа (для TG-бота), в ленте не повторяем.
FEED_SKIP = ("ready", "arbiter", "failed")


class EventFeed(RichLog):
    """Лента событий: «00:21  H13  Spark Go пишет код»."""

    def __init__(self, *args, max_lines: int | None = 200, **kwargs) -> None:
        # Без лимита лента растёт бесконечно за долгую сессию top.
        super().__init__(*args, max_lines=max_lines, wrap=True, **kwargs)

    def push(self, events: list[dict], tasks: dict[str, TaskSnap] | None = None) -> None:
        """Добавить строки событий; tasks — задачи снимка (для имён моделей)."""
        tasks = tasks or {}
        for ev in events or []:
            try:
                kind = str(ev.get("kind") or "")
                if kind in FEED_SKIP:
                    continue
                tid = str(ev.get("task_id") or "")
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
