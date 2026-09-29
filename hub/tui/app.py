"""Живой экран hub top: та же картина, что hub status, но таблицей."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from hub import time as ht
from hub.read import procs
from hub.read import snapshot
from hub.store import Store
from hub.tui.widgets import EventFeed, TaskTable

if TYPE_CHECKING:
    from hub.read.snapshot import Snapshot

try:  # H04: только импорт, без копирования логики
    from hub.read.findings import load_findings  # type: ignore
except ImportError:  # H04 ещё не применён
    load_findings = None  # type: ignore

log = logging.getLogger(__name__)

GO_LIMIT = 60.0
FEED_TAIL = 50
FINAL_STAGES = ("merged", "dropped")


class ConfirmMerge(ModalScreen[bool]):
    """Модальное подтверждение merge."""

    BINDINGS = [("y", "confirm", "да"), ("n", "cancel", "нет"), ("escape", "cancel", "нет")]

    def __init__(self, task_id: str) -> None:
        super().__init__()
        self._task_id = task_id

    def compose(self) -> ComposeResult:
        yield Static(f"Смержить {self._task_id}? (y — да, n — нет)", id="confirm-text")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HubApp(App):
    """Экран top. Источник данных — только snapshot.build каждые 2 с."""

    CSS = """
    #top-header { height: 3; }
    #tasks { height: 1fr; }
    #details { height: 12; border: solid #666; }
    #events { height: 8; }
    """

    BINDINGS = [
        ("s", "stop_task", "stop"),
        ("m", "merge_task", "merge"),
        ("f", "show_findings", "findings"),
        ("q", "quit_app", "выход"),
        ("enter", "show_details", "детали"),
    ]

    def __init__(self, store: Store | None = None,
                 opencode_db: str | None = None, proc_root: str = "/proc",
                 agy_root: str | None = None) -> None:
        super().__init__()
        self._store_param = store
        self._store_cache: Store | None = store
        self._opencode_db = opencode_db
        self._proc_root = proc_root
        self._agy_root = agy_root
        self.project_filter: str | None = None
        self._snap: Snapshot | None = None
        self._last_event_id: int = 0
        self._details_task: str | None = None
        self._narrow: bool = False
        self._bot_alive_flag: bool = False
        self._events_primed: bool = False
        self._refreshing: bool = False

    def compose(self) -> ComposeResult:
        yield Static("hub top — загрузка…", id="top-header")
        yield TaskTable(id="tasks")
        # Детали — в прокрутке: текст длиннее фиксированной высоты
        # (сессии, tool-строки, todo, замечания), Static обрезал бы хвост.
        yield VerticalScroll(Static("Детали: Enter по строке таблицы",
                                    id="details-text"), id="details")
        yield EventFeed(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._schedule_refresh)
        self._apply_narrow_mode()
        self._schedule_refresh()

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        # self.size в этот момент ещё старый (App._on_resize бежит позже
        # по MRO) — берём новый размер из события.
        self._apply_narrow_mode(getattr(event, "size", None))

    # --- опрос: весь блокирующий I/O — в потоке воркера ---

    def _schedule_refresh(self) -> None:
        # Снимок собирается дольше интервала (~5 с на 36 задачах): exclusive
        # отменял бы незаконченный опрос каждые 2 с и кадр не приходил никогда —
        # пока опрос идёт, тик пропускаем.
        if self._refreshing:
            return
        self._refreshing = True
        self.run_worker(self._do_refresh(), group="refresh", exit_on_error=False)

    async def _do_refresh(self) -> None:
        try:
            snap, bot_alive, events = await asyncio.to_thread(self._fetch_all)
            self._apply_snapshot(snap, bot_alive=bot_alive, events=events)
        finally:
            self._refreshing = False

    def _store_cached(self) -> Store | None:
        if self._store_cache is not None:
            return self._store_cache
        try:
            self._store_cache = Store()
        except Exception:
            log.exception("не открылось хранилище")
            return None
        return self._store_cache

    def _store(self) -> Store | None:
        if self._store_param is not None:
            return self._store_param
        return self._store_cached()

    def _resolve_opencode_db(self) -> str | None:
        """Путь к чужой БД для snapshot.build (сам TUI файлы не открывает)."""
        if self._opencode_db is not None:
            return self._opencode_db
        try:
            db = Path.home() / ".local/share/opencode/opencode.db"
        except Exception:
            log.exception("не резолвится путь БД")
            return None
        try:
            return str(db) if db.exists() else None
        except Exception:
            log.exception("не проверяется путь БД")
            return None

    def _resolve_agy_root(self) -> str | None:
        """Каталог чужих conversations для snapshot.build (сам TUI не открывает)."""
        if self._agy_root is not None:
            return self._agy_root
        try:
            conv = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
        except Exception:
            log.exception("не резолвится путь agy")
            return None
        try:
            return str(conv) if conv.is_dir() else None
        except Exception:
            log.exception("не проверяется путь agy")
            return None

    def _fetch_snapshot(self) -> Snapshot | None:
        store = self._store()
        if store is None:
            return None
        try:
            # Источник данных только snapshot.build(store, now_ms(), opencode_db, proc_root).
            return snapshot.build(store, ht.now_ms(),
                                  self._resolve_opencode_db(), self._proc_root,
                                  self._resolve_agy_root())
        except Exception:
            # Хранилище/снимок могут отдать ошибку при конкурентной записи —
            # пропускаем тик, экран остаётся на прошлом кадре.
            log.exception("не собрался снимок")
            return None

    def _check_bot(self) -> bool:
        """Жив ли hub bot: процесс вида hub_bot в agent_procs (см. _kind_of)."""
        try:
            live = procs.agent_procs(self._proc_root)
        except Exception:
            log.exception("не читаются процессы")
            return False
        return any(p.kind == "hub_bot" for p in live)

    def _fetch_events(self) -> list[dict]:
        store = self._store()
        if store is None:
            return []
        try:
            evs = store.events_since(self._last_event_id)
        except Exception:
            log.exception("не читаются события")
            return []
        if not self._events_primed:
            # Первый тик: лента начинается с пустого — всю историю
            # не вываливаем, только запоминаем последний id.
            self._events_primed = True
            if evs:
                try:
                    self._last_event_id = max(int(e.get("id", 0) or 0) for e in evs)
                except Exception:
                    log.exception("битый id события")
            return []
        return evs

    def _fetch_all(self) -> tuple[Snapshot | None, bool, list[dict]]:
        """Всё блокирующее чтение тика — одним куском в потоке воркера."""
        return (self._fetch_snapshot(), self._check_bot(), self._fetch_events())

    def _apply_snapshot(self, snap: Snapshot | None, bot_alive: bool = False,
                         events: list[dict] | None = None) -> None:
        """Только рисование готовых данных — без I/O."""
        # Узкий режим — всегда, даже при пустом тике: иначе при snap None
        # неверный режим после ресайза остался бы навсегда.
        self._apply_narrow_mode()
        if snap is None:
            return
        self._snap = snap
        self._bot_alive_flag = bot_alive
        try:
            self.query_one("#top-header", Static).update(
                self._header_text(snap, bot_alive=bot_alive))
        except Exception:
            log.exception("не обновилась шапка")
        try:
            self.query_one("#tasks", TaskTable).update(self._filtered(snap))
        except Exception:
            log.exception("не обновилась таблица")
        try:
            if events:
                self._last_event_id = max(
                    self._last_event_id,
                    max(int(e.get("id", 0) or 0) for e in events),
                )
                self.query_one("#events", EventFeed).push(events[-FEED_TAIL:])
        except Exception:
            log.exception("не обновилась лента")
        self._apply_narrow_mode()

    def _filtered(self, snap: Snapshot) -> Snapshot:
        # Как hub status: финальные этапы скрыты; фильтр проекта — только таблица.
        tasks = [t for t in snap.tasks if t.stage not in FINAL_STAGES]
        if self.project_filter:
            needle = self.project_filter
            tasks = [t for t in tasks if needle in (t.project or "")]
        return replace(snap, tasks=tasks)

    # --- шапка ---

    def _header_text(self, snap: Snapshot, bot_alive: bool | None = None) -> str:
        if bot_alive is None:
            bot_alive = self._bot_alive_flag
        active = sum(1 for t in snap.tasks if t.stage not in (*FINAL_STAGES, "queued"))
        queued = sum(1 for t in snap.tasks if t.stage == "queued")
        go = snap.total_go or 0.0
        usd = snap.total_usd or 0.0
        frac = min(1.0, go / GO_LIMIT) if GO_LIMIT > 0 else 0.0
        filled = int(frac * 10)
        bar = "█" * filled + "░" * (10 - filled)
        # Нет процесса hub_bot в agent_procs — честное 'бот ?', не 'бот нет'.
        bot = "бот жив" if bot_alive else "бот ?"
        runs = getattr(snap, "agy_runs", 0) or 0
        steps = getattr(snap, "agy_steps", 0) or 0
        return (
            f"активно {active} · в очереди {queued} · "
            f"$ сегодня go {go:.2f} usd {usd:.2f} · "
            # total_go — расход за сегодня, лимит 60 — месячный котёл (spec §5).
            f"Go-день [{bar}] {go:.2f}/60мес · "
            f"Gemini: {runs} запусков / {steps} шагов за 5 ч · {bot}"
        )

    # --- узкий режим ---

    def _apply_narrow_mode(self, size=None) -> None:  # type: ignore[no-untyped-def]
        try:
            sz = size if size is not None else self.size
            narrow = sz.width <= 80 or sz.height <= 24
        except Exception:
            log.exception("нет размера экрана")
            return
        self._narrow = narrow
        for wid_id in ("#details", "#events"):
            try:
                self.query_one(wid_id).display = not self._narrow
            except Exception:
                log.exception("не переключился узкий режим")

    # --- детали ---

    def _current_task_id(self) -> str | None:
        try:
            table = self.query_one("#tasks", TaskTable)
        except Exception:
            log.exception("нет таблицы")
            return None
        try:
            rows = table.ordered_rows
            idx = int(table.cursor_row)
            if 0 <= idx < len(rows):
                key = rows[idx].key
                val = getattr(key, "value", key)
                return str(val) if val else None
        except Exception:
            log.exception("нет текущей строки")
            return None
        return None

    def _build_detail_text(self, task_id: str) -> str:
        """Текст деталей. Блокирующий I/O (store/файлы) — звать только из воркера."""
        snap = self._snap
        task = None
        if snap is not None:
            for t in snap.tasks:
                if str(t.id) == task_id:
                    task = t
                    break
        lines: list[str] = [f"Задача {task_id}"]
        if task is None:
            lines.append("(нет в снимке)")
            return "\n".join(lines)
        lines.append(f"этап {task.stage} · раунд {task.round} · пульс {task.pulse}")
        lines.append("Сессии:")
        if task.sessions:
            for s in task.sessions:
                lines.append(
                    f" {s.role}:{s.model} {s.pulse} "
                    f"${s.cost:.3f} ctx {s.context_tokens} {s.last_activity}"[:100]
                )
        else:
            lines.append(" —")
        # В Snapshot H01 нет истории tool-вызовов — показываем честно
        # последнее действие сессий, а не выдуманные '20 вызовов'.
        lines.append("Последняя активность сессий:")
        shown = 0
        for s in task.sessions or []:
            if s.last_activity and s.last_activity != "-":
                lines.append(f" {s.last_activity}"[:80])
                shown += 1
        if shown == 0:
            lines.append(" —")
        lines.append("Tool-история (20 вызовов): — (нет источника в H01)")
        lines.append("Todo: — (нет источника в H01)")
        lines.append("Замечания:")
        lines.extend(self._finding_lines(task_id))
        return "\n".join(lines)

    async def _do_show_details(self, task_id: str) -> None:
        text = await asyncio.to_thread(self._build_detail_text, task_id)
        self._details_task = task_id
        try:
            self.query_one("#details-text", Static).update(text)
            # В узком режиме детали скрыты — не выпячиваем их из-под шторки.
            self.query_one("#details").display = not self._narrow
        except Exception:
            log.exception("не отрисовались детали")

    def _finding_lines(self, task_id: str) -> list[str]:
        if load_findings is None:
            return [" (findings H04 нет)"]
        wt = ""
        try:
            store = self._store()
            if store is not None:
                row = store.get_task(task_id)
                wt = str((row or {}).get("worktree") or "")
        except Exception:
            log.exception("не читается задача для findings")
            return [" (нет worktree)"]
        if not wt:
            return [" (нет worktree)"]
        try:
            items = load_findings(Path(wt))
        except Exception:
            log.exception("не читаются findings")
            return [" (не читаются)"]
        if not items:
            return [" —"]
        out = []
        for f in items[:10]:
            try:
                out.append(f"{f.file}:{f.line} [{f.severity}] {f.issue[:80]} ({f.author})")
            except Exception:
                log.exception("битое замечание")
                continue
        return out or [" —"]

    # --- клавиши ---

    def action_show_details(self) -> None:
        tid = self._current_task_id()
        if tid:
            self.run_worker(self._do_show_details(tid), group="cmd", exclusive=True,
                            exit_on_error=False)

    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        try:
            tid = str(event.row_key.value or "")
        except Exception:
            log.exception("битое событие выбора строки")
            return
        if tid:
            self.run_worker(self._do_show_details(tid), group="cmd", exclusive=True,
                            exit_on_error=False)

    def action_quit_app(self) -> None:
        self.exit()

    def action_stop_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("нет задачи", severity="warning")
            return
        self.run_worker(self._run_hub_cmd(["stop", tid]), group="cmd", exclusive=True,
                        exit_on_error=False)

    def action_merge_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("нет задачи", severity="warning")
            return

        def _done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._run_hub_cmd(["merge", tid]), group="cmd",
                                exclusive=True, exit_on_error=False)

        self.push_screen(ConfirmMerge(tid), _done)

    def action_show_findings(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("нет задачи", severity="warning")
            return
        if load_findings is not None:
            self.run_worker(self._do_show_details(tid), group="cmd", exclusive=True,
                            exit_on_error=False)
            return
        self.run_worker(self._run_hub_cmd(["findings", tid]), group="cmd",
                        exclusive=True, exit_on_error=False)

    async def _run_hub_cmd(self, argv: list[str]) -> None:
        # Тот же интерпретатор, что крутит TUI: 'hub' из PATH может не найтись.
        cmd = [sys.executable, "-m", "hub.cli", *argv]
        # merge гоняет приёмку под замком дольше минуты — таймаут с запасом.
        timeout = 600 if argv[:1] == ["merge"] else 60
        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=timeout,
            )
        except Exception as e:
            log.exception("команда hub не вышла")
            self.notify(f"не вышло: {e}"[:80], severity="error")
            self._schedule_refresh()
            return
        # Ошибка — показываем stderr (там причина, а не полускачанный stdout);
        # успех — последнюю строку stdout.
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            tail = (err or out).splitlines()
            self.notify((tail[-1][:80] if tail else f"код {proc.returncode}"),
                        severity="error")
        else:
            tail = out.splitlines()
            self.notify(tail[-1][:80] if tail else "готово")
        self._schedule_refresh()


if __name__ == "__main__":  # pragma: no cover
    HubApp().run()
