"""Живой экран hub top: задачи агентов словами — что делаем, что сейчас, кто, сколько.

Сверху — шапка (сколько работает, деньги, бот), таблица задач, под ней —
подробности задачи под курсором (зачем, этап, путь, кто работал, замечания),
внизу — лента событий. «?» — что значат значки и как идёт задача.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from hub import time as ht
from hub.read import human as hm
from hub.read import procs
from hub.read import snapshot
from hub.store import Store
from hub.tui.widgets import COMPACT_WIDTH, WORKING_MS, EventFeed, TaskTable, code_of

if TYPE_CHECKING:
    from hub.read.snapshot import Snapshot, TaskSnap

try:  # H04: только импорт, без копирования логики
    from hub.read.findings import latest_round, load_findings  # type: ignore
except ImportError:  # H04 ещё не применён
    latest_round = load_findings = None  # type: ignore

log = logging.getLogger(__name__)


GO_LIMIT = 60.0
FEED_HISTORY = 12   # сколько последних событий показать при запуске
FEED_TAIL = 50
FINAL_STAGES = ("merged", "dropped")
FINDINGS_MAX = 8

HELP_TEXT = """\
[b]Как идёт задача[/b]
  очередь → исполнитель пишет код → тесты → проверка кода (1–2 модели)
  → готово → Claude проверяет и сливает.
  Проверка нашла ошибки → исполнитель исправляет (следующий круг).
  Круги кончились или проверяющие не ответили → «Нужно решение Claude».

[b]Значок слева — живость задачи[/b]
  🟢 работает — агент что-то делал в последние 2 мин
  🟡 давно тихо — думает над большим шагом или ждёт тесты
  🔴 похоже, зависла — давно ни одного действия
  ⚫ процесса нет — ждёт очереди; если не в очереди — Claude разберётся
  ✅ готово   ⚖️ ждёт решения Claude   ❌ ошибка   ⏹ остановлена

[b]Модели[/b]
  Spark Go — Muse Spark 1.3 по подписке Go ($60/мес), основной исполнитель и проверяющий
  Spark бесплатный — тот же Spark бесплатно, медленнее; если долго молчит (обычно 15 мин),
                     задача сама переходит на Spark Go
  MiMo Flash — второй проверяющий (дёшево)
  DeepSeek — рутина (сводки, наблюдатель)

[b]Цвет строки[/b]  жёлтый — нужно решение, красный — ошибка, голубой — готово, серый — ждёт.

[b]Нужно ли что-то делать вам?[/b]
  Обычно нет: жёлтые и красные строки разбирает Claude (он получает их сам).
  Вам — только «вопросы вам» в шапке: ответ кнопкой в TG-боте.

[b]Клавиши[/b]
  ↑ ↓ — выбрать задачу (подробности появляются сразу под таблицей)
  f — обновить замечания проверки    ? — эта справка    q — выход
  Окно меньше 80×24 — подробности и лента скрыты: растяните окно
  s — остановить задачу, m — слить готовую (обе с подтверждением; обычно это делает Claude)

Esc или ? — закрыть"""


class HelpScreen(ModalScreen[None]):
    """Что значат значки, цвета и как идёт задача."""

    BINDINGS = [("escape", "close", "закрыть"), ("question_mark", "close", "закрыть"),
                ("q", "close", "закрыть")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 96; max-width: 95%; height: auto; max-height: 90%; border: round $accent;
            padding: 1 2; background: $panel; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(HELP_TEXT, markup=True), id="help")

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmAction(ModalScreen[bool]):
    """Подтверждение остановки/слияния: случайное нажатие ничего не делает."""

    BINDINGS = [("y", "confirm", "да"), ("n", "cancel", "нет"), ("escape", "cancel", "нет")]
    DEFAULT_CSS = """
    ConfirmAction { align: center middle; }
    #confirm-text { width: 70; max-width: 95%; height: auto; border: round $warning; padding: 1 2;
                    background: $panel; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        yield Static(f"{self._question}\n\ny — да, n — нет", id="confirm-text")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmMerge(ConfirmAction):
    """Подтверждение слияния задачи."""

    def __init__(self, task_id: str, label: str = "") -> None:
        super().__init__(f"Слить задачу {label or task_id} в рабочую ветку?")
        self._task_id = task_id


class HubApp(App):
    """Экран top. Источник данных — только snapshot.build, опрос в потоке."""

    TITLE = "Пульт агентов"
    CSS = """
    #top-header { height: auto; padding: 0 1; background: $boost; }
    #tasks { height: auto; max-height: 45%; }
    #details { height: 1fr; min-height: 8; border: round $primary; padding: 0 1; }
    #events { height: 9; border: round $secondary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("question_mark", "help", "что значат значки"),
        Binding("q", "quit_app", "выход"),
        Binding("s", "stop_task", "остановить", show=False),
        Binding("m", "merge_task", "слить", show=False),
        Binding("f", "show_findings", "замечания", show=False),
        Binding("enter", "show_details", "подробнее", show=False),
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
        self._questions: int = 0
        # Замечания выбранной задачи: {worktree: (mtime .agent, результат)} — без чтения
        # файлов ревью каждые 2 с.
        self._findings_cache: dict[str, tuple[int, tuple[int, list[str]]]] = {}

    def compose(self) -> ComposeResult:
        yield Static("Пульт агентов — собираю картину (несколько секунд)…", id="top-header")
        yield TaskTable(id="tasks")
        # Подробности — в прокрутке: текст бывает длиннее панели.
        yield VerticalScroll(Static("Выберите задачу стрелками ↑ ↓", id="details-text"),
                             id="details")
        yield EventFeed(id="events")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.query_one("#details").border_title = "Подробности"
            self.query_one("#events").border_title = "События"
        except Exception:
            log.exception("нет панелей")
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
            tid = self._current_task_id()
            if tid:
                await self._do_show_details(tid)
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
        if not self._events_primed:
            # Первый тик: не пустая лента, а последние FEED_HISTORY событий —
            # сразу видно, что происходило до запуска экрана.
            self._events_primed = True
            try:
                evs = store.recent_events(FEED_HISTORY)
            except Exception:
                log.exception("не читается история событий")
                return []
            if evs:
                self._last_event_id = max(int(e.get("id", 0) or 0) for e in evs)
            return evs
        try:
            return store.events_since(self._last_event_id)
        except Exception:
            log.exception("не читаются события")
            return []

    def _fetch_all(self) -> tuple[Snapshot | None, bool, list[dict]]:
        """Всё блокирующее чтение тика — одним куском в потоке воркера."""
        store = self._store()
        try:
            self._questions = store.count_open_questions() if store is not None else 0
        except Exception:
            log.exception("не читаются вопросы")
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
                by_id = {t.id: t for t in snap.tasks}
                self.query_one("#events", EventFeed).push(events[-FEED_TAIL:], by_id)
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

    def _header_text(self, snap: Snapshot, bot_alive: bool | None = None,
                     questions: int | None = None) -> Text:
        if bot_alive is None:
            bot_alive = self._bot_alive_flag
        if questions is None:
            questions = self._questions
        live = [t for t in snap.tasks if t.stage not in FINAL_STAGES]
        waiting = ("queued", "ready", "arbiter", "failed", "stopped")
        active = [t for t in live if t.stage not in waiting]
        # «Работают» — только с живым процессом; этап «пишет код» без процесса — «тихо».
        working = sum(1 for t in active if hm.is_alive(t.pulse))
        silent = len(active) - working
        queued = sum(1 for t in live if t.stage == "queued")
        claude = sum(1 for t in live if t.stage in ("ready", "arbiter", "failed"))
        month = snap.month_go or 0.0
        frac = min(1.0, month / GO_LIMIT) if GO_LIMIT > 0 else 0.0
        bar = "█" * int(frac * 10) + "░" * (10 - int(frac * 10))
        head = Text()
        head.append("Пульт агентов", style="bold")
        head.append(f" · {hm.clock(snap.now_ms)} · ")
        if not (working or silent or queued or claude):
            head.append("сейчас ничего не работает")
        else:
            head.append(f"работают {working}", style="bold green" if working else "")
            if silent:
                head.append(" · ")
                head.append(f"тихо или зависли {silent}", style="bold red")
            head.append(f" · в очереди {queued} · ")
            head.append(f"ждут Claude {claude}", style="bold yellow" if claude else "")
        if questions:
            head.append(" · ")
            head.append(f"вопросы вам: {questions} (ответ в TG)", style="bold magenta")
        head.append(" · бот TG: ")
        # Нет процесса hub_bot — «не вижу», а не «упал»: бот мог быть запущен иначе.
        head.append("работает" if bot_alive else "не вижу", style="green" if bot_alive else "yellow")
        head.append("\nДеньги: сегодня задачи ")
        head.append(hm.money((snap.total_go or 0) + (snap.total_usd or 0)), style="bold")
        head.append(f" · все проекты Go {hm.money(snap.all_go)}")
        head.append(f" · реальные деньги {hm.money(snap.all_usd)}")
        pct = snapshot.limit_pct(month)
        head.append(f" · месяц Go {hm.money(month)} из $60 [{bar}] ")
        head.append(pct, style="bold red" if "превышен" in pct else "")
        if snap.agy_runs:  # Gemini выключен владельцем — показываем, только если работал
            head.append(f" · Gemini: {snapshot.gemini_window(snap.agy_runs, snap.agy_steps)}")
        head.append("    ? — что значат значки", style="dim")
        return head

    # --- узкий режим ---

    def _apply_narrow_mode(self, size=None) -> None:  # type: ignore[no-untyped-def]
        try:
            sz = size if size is not None else self.size
            narrow = sz.width <= 80 or sz.height <= 24
        except Exception:
            log.exception("нет размера экрана")
            return
        self._narrow = narrow
        try:
            table = self.query_one("#tasks", TaskTable)
            if table.set_compact(sz.width < COMPACT_WIDTH) and self._snap is not None:
                table.update(self._filtered(self._snap))
        except Exception:
            log.exception("не переключилась компактная таблица")
        for wid_id in ("#details", "#events"):
            try:
                self.query_one(wid_id).display = not self._narrow
            except Exception:
                log.exception("не переключился узкий режим")

    # --- подробности ---

    def _task_label(self, task_id: str) -> str:
        """«H13 · Hub: повтор при сбое…» для подтверждений."""
        snap = self._snap
        task = next((t for t in (snap.tasks if snap else []) if str(t.id) == task_id), None)
        name = (task.short or task.title) if task else ""
        return f"{code_of(task_id)} · {name}" if name else task_id

    def _current_task_id(self) -> str | None:
        try:
            return self.query_one("#tasks", TaskTable).selected_task_id()
        except Exception:
            log.exception("нет таблицы")
            return None

    def _build_detail(self, task_id: str, snap: Snapshot | None = None) -> Text:
        """Подробности задачи. Блокирующий I/O (замечания) — звать только из воркера.

        snap — снимок, снятый в UI-потоке: задача и «сейчас» из одного тика.
        """
        snap = snap if snap is not None else self._snap
        task = None
        if snap is not None:
            task = next((t for t in snap.tasks if str(t.id) == task_id), None)
        out = Text()
        if task is None:
            out.append(f"Задача {task_id}\n(нет в снимке)")
            return out
        for n, (label, text) in enumerate(hm.describe(task, snap.now_ms, WORKING_MS)):
            if not label:
                out.append(text + "\n", style="bold" if n == 0 else "dim")
            elif "\n" in text or label == "Сейчас работают":
                out.append(f"{label}:\n", style="bold")
                for line in text.splitlines():
                    out.append(f"  {line}\n")
            else:
                out.append(f"{label}: ", style="bold")
                out.append(text + "\n")
        rnd, lines = self._finding_lines(task_id)
        if rnd:
            out.append(f"Замечания проверки (круг {rnd}):", style="bold")
            out.append("\n" if lines else " нет\n")
            for ln in lines:
                out.append(ln + "\n")
        return out

    def _build_detail_text(self, task_id: str) -> str:
        return self._build_detail(task_id).plain

    async def _do_show_details(self, task_id: str) -> None:
        snap = self._snap  # снят в UI-потоке: поток воркера не видит смену тика посередине
        text = await asyncio.to_thread(self._build_detail, task_id, snap)
        self._details_task = task_id
        try:
            self.query_one("#details-text", Static).update(text)
            # В узком режиме подробности скрыты — не выпячиваем их из-под шторки.
            self.query_one("#details").display = not self._narrow
        except Exception:
            log.exception("не отрисовались подробности")

    def _finding_lines(self, task_id: str) -> tuple[int, list[str]]:
        """Замечания ПОСЛЕДНЕГО круга проверки без дублей: (номер круга, строки)."""
        if load_findings is None or latest_round is None:
            return 0, []
        try:
            store = self._store()
            row = store.get_task(task_id) if store is not None else None
            wt = str((row or {}).get("worktree") or "")
        except Exception:
            log.exception("не читается задача для замечаний")
            return 0, []
        if not wt or not Path(wt).is_dir():
            return 0, []
        try:
            mtime = (Path(wt) / ".agent").stat().st_mtime_ns
        except OSError:
            mtime = 0
        hit = self._findings_cache.get(wt)
        if hit and hit[0] == mtime and mtime:
            return hit[1]
        try:
            rnd, items = latest_round(Path(wt))
        except Exception:
            log.exception("не читаются замечания")
            return 1, ["  (файлы замечаний не читаются — подробности: hub findings)"]
        sev = {"high": "важное", "medium": "среднее", "low": "мелочь"}
        out = []
        for f in items[:FINDINGS_MAX]:
            try:
                where = f"{Path(f.file).name}:{f.line}" if f.file and f.file != "?" else ""
                who = f" ({hm.model_name(f.author)})" if f.author else ""
                out.append(f"  • [{sev.get(f.severity, f.severity)}] {where} {hm.fit(f.issue, 110)}{who}")
            except Exception:
                log.exception("битое замечание")
        if len(items) > FINDINGS_MAX:
            out.append(f"  … и ещё {len(items) - FINDINGS_MAX} (hub findings {task_id})")
        self._findings_cache[wt] = (mtime, (rnd, out))
        return rnd, out

    # --- клавиши и события ---

    def on_data_table_row_highlighted(self, event) -> None:  # type: ignore[no-untyped-def]
        """Курсор на строке — подробности сразу, без Enter."""
        try:
            tid = str(event.row_key.value or "")
        except Exception:
            return
        if tid and tid != self._details_task:
            self.run_worker(self._do_show_details(tid), group="cmd", exclusive=True,
                            exit_on_error=False)

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

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        self.exit()

    def action_stop_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("Сначала выберите задачу", severity="warning")
            return

        def _done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._run_hub_cmd(["stop", tid]), group="cmd",
                                exclusive=True, exit_on_error=False)

        self.push_screen(ConfirmAction(f"Остановить задачу {self._task_label(tid)}?"), _done)

    def action_merge_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("Сначала выберите задачу", severity="warning")
            return

        def _done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._run_hub_cmd(["merge", tid]), group="cmd",
                                exclusive=True, exit_on_error=False)

        self.push_screen(ConfirmMerge(tid, self._task_label(tid)), _done)

    def action_show_findings(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("Сначала выберите задачу", severity="warning")
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
