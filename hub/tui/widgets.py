"""Виджеты TUI: таблица задач, лента событий, спарклайн."""

from __future__ import annotations

import logging

from rich.text import Text  # транзитивная зависимость textual, новых записей в pyproject нет
from textual.widgets import DataTable, RichLog

from hub.read.snapshot import Snapshot

log = logging.getLogger(__name__)

BLOCKS = "▁▂▃▄▅▆▇█"

COL_KEYS = [
    "id", "project", "stage", "pulse", "role", "round",
    "time", "tokens", "context", "cache", "cost", "last",
]

COL_LABELS = [
    "ID", "проект", "этап", "пульс", "роль/модель", "раунд",
    "время", "токены/10мин", "контекст", "кэш %", "$", "последнее",
]

# Цвет этапа: только оформление, логика пульса — в snapshot.
STAGE_STYLE = {
    "queued": "dim",
    "preflight": "yellow",
    "exec": "green",
    "gate": "yellow",
    "review": "magenta",
    "ready": "cyan",
    "arbiter": "red",
    "failed": "red",
    "stopped": "yellow",
    "merged": "dim",
    "dropped": "dim",
}


def _stage_style(stage: str) -> str:
    s = (stage or "").lower()
    for prefix, style in STAGE_STYLE.items():
        if s.startswith(prefix):
            return style
    return ""


def sparkline(values: list[float], width: int = 10) -> str:
    """Лесенка из блоков ▁…█. Пустой вход → ""."""
    vals = [float(v) for v in values]
    if not vals:
        return ""
    if width <= 0:
        return ""
    if len(vals) > width:
        vals = vals[-width:]
    lo = min(vals)
    hi = max(vals)
    if hi <= lo:
        return "▅" * len(vals)
    n = len(BLOCKS) - 1
    out = []
    for v in vals:
        idx = int((v - lo) / (hi - lo) * n)
        if idx < 0:
            idx = 0
        if idx > n:
            idx = n
        out.append(BLOCKS[idx])
    return "".join(out)


def _role_model(task) -> str:
    parts = [f"{s.role}:{s.model}" for s in (task.sessions or [])[:2]]
    return ",".join(parts) if parts else "—"


def _cost(task) -> str:
    return f"${(task.cost_go or 0.0) + (task.cost_usd or 0.0):.2f}"


class TaskTable(DataTable):
    """Таблица задач с обновлением по ключу (без полного пересоздания)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Enter по строке даёт RowSelected (при cell-курсоре — CellSelected,
        # который тоже обрабатываем) — иначе детали по Enter не открываются.
        self.cursor_type = "row"
        self._cols_ready = False
        self._keys: set[str] = set()
        # Прокси до H02: в Snapshot H01 нет дельт session.tokens_* (spec §5),
        # поэтому динамика колонки 'токены/10мин' строится по task.context.
        self._hist: dict[str, list[float]] = {}

    def _ensure_columns(self) -> None:
        if self._cols_ready:
            return
        for label, key in zip(COL_LABELS, COL_KEYS):
            self.add_column(label, key=key)
        self._cols_ready = True

    def _cells(self, task, spark: str) -> list:
        stage = Text(str(task.stage or "—"), style=_stage_style(str(task.stage or "")))
        return [
            str(task.id),
            str(task.project or "—"),
            stage,
            str(task.pulse or ""),
            _role_model(task),
            str(task.round),
            # Полей времени/кэша нет в Snapshot H01 — честный прочерк.
            "—",
            spark,
            str(task.context or 0),
            "—",
            _cost(task),
            str(task.last_activity or "-")[:40],
        ]

    def update(self, snap: Snapshot) -> None:  # type: ignore[override]
        """Обновить строки по id задачи: изменить/додать/убрать лишние."""
        self._ensure_columns()
        wanted = {str(t.id): t for t in snap.tasks}
        for key in list(self._keys):
            if key not in wanted:
                try:
                    self.remove_row(key)
                except Exception:
                    log.exception("не удалась строка %s", key)
                self._keys.discard(key)
                self._hist.pop(key, None)
        for tid, task in wanted.items():
            hist = self._hist.setdefault(tid, [])
            try:
                hist.append(float(task.context or 0))
            except (TypeError, ValueError):
                hist.append(0.0)
            if len(hist) > 10:
                hist[:] = hist[-10:]
            cells = self._cells(task, sparkline(hist))
            if tid in self._keys:
                for col_key, val in zip(COL_KEYS, cells):
                    try:
                        # Ширину пересчитываем, иначе длинные значения
                        # обрезаются по первому кадру.
                        self.update_cell(tid, col_key, val, update_width=True)
                    except Exception:
                        log.exception("не обновилась ячейка %s/%s", tid, col_key)
            else:
                try:
                    self.add_row(*cells, key=tid)
                except Exception:
                    log.exception("не добавилась строка %s", tid)
                    continue
                self._keys.add(tid)


class EventFeed(RichLog):
    """Лента событий снизу."""

    def __init__(self, *args, max_lines: int | None = 200, **kwargs) -> None:
        # Без лимита лента растёт бесконечно за долгую сессию top.
        super().__init__(*args, max_lines=max_lines, **kwargs)

    def push(self, events: list[dict]) -> None:
        """Добавить строки событий (последние N решает вызывающий)."""
        for ev in events or []:
            try:
                eid = ev.get("id", "?")
                tid = ev.get("task_id", "-") or "-"
                kind = ev.get("kind", "?") or "?"
                payload = ev.get("payload_json", ev.get("payload", ""))
                text = str(payload) if payload is not None else ""
                if len(text) > 80:
                    text = text[:80]
                line = f"#{eid} {tid} {kind} {text}".rstrip()
            except Exception:
                log.exception("битое событие")
                continue
            try:
                self.write(line)
            except Exception:
                log.exception("не записалась строка ленты")
