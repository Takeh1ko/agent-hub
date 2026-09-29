"""Живой экран hub top: задачи агентов словами — что делаем, что сейчас, кто, сколько.

Сверху — шапка (сколько работает, деньги, бот), таблица задач, под ней —
подробности задачи под курсором (зачем, этап, путь, кто работал, замечания),
внизу — лента событий. «?» — что значат значки и как идёт задача.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
from hub.tui.widgets import WORKING_MS, EventFeed, TaskTable, code_of

if TYPE_CHECKING:
    from hub.read.snapshot import Snapshot, TaskSnap

try:  # H04: только импорт, без копирования логики
    from hub.read.findings import load_findings  # type: ignore
except ImportError:  # H04 ещё не применён
    load_findings = None  # type: ignore

log = logging.getLogger(__name__)

_REVIEW_RE = re.compile(r"^review_r(\d+)(?:_.*)?\.json$")

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
  ⚫ процесса нет — ждёт очереди или упала
  ✅ готово   ⚖️ ждёт решения Claude   ❌ ошибка   ⏹ остановлена

[b]Модели[/b]
  Spark Go — Muse Spark 1.3 по подписке Go ($60/мес), основной исполнитель и проверяющий
  Spark бесплатный — тот же Spark бесплатно, медленнее; молчит 15 мин → сам переходит на Go
  MiMo Flash — второй проверяющий (дёшево)
  DeepSeek — рутина (сводки, наблюдатель)

[b]Цвет строки[/b]  жёлтый — нужно решение, красный — ошибка, голубой — готово, серый — ждёт.

[b]Клавиши[/b]
  ↑ ↓ — выбрать задачу (подробности появляются сразу под таблицей)
  ? — эта справка    q — выход
  s — остановить задачу, m — слить готовую (обе с подтверждением; обычно это делает Claude)

Esc или ? — закрыть"""


def _spent_by_model(task: TaskSnap) -> list[str]:
    """«Spark Go $0.16 (5 запусков)», по убыванию денег."""
    agg: dict[str, list[float]] = {}
    for s in task.sessions or []:
        name = hm.model_name(s.model, s.provider)
        cur = agg.setdefault(name, [0.0, 0])
        cur[0] += float(s.cost or 0.0)
        cur[1] += 1
    rows = sorted(agg.items(), key=lambda kv: -kv[1][0])
    return [f"{name} {hm.money(cost)} ({hm.plural(int(n), 'запуск', 'запуска', 'запусков')})"
            for name, (cost, n) in rows]


class HelpScreen(ModalScreen[None]):
    """Что значат значки, цвета и как идёт задача."""

    BINDINGS = [("escape", "close", "закрыть"), ("question_mark", "close", "закрыть"),
                ("q", "close", "закрыть")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 96; height: auto; max-height: 90%; border: round $accent;
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
    #confirm-text { width: 70; height: auto; border: round $warning; padding: 1 2;
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
    """Совместимость: подтверждение слияния задачи."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Слить задачу {task_id} в рабочую ветку?")
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

    def _header_text(self, snap: Snapshot, bot_alive: bool | None = None) -> Text:
        if bot_alive is None:
            bot_alive = self._bot_alive_flag
        live = [t for t in snap.tasks if t.stage not in FINAL_STAGES]
        working = sum(1 for t in live if t.stage not in ("queued", "ready", "arbiter",
                                                          "failed", "stopped"))
        queued = sum(1 for t in live if t.stage == "queued")
        claude = sum(1 for t in live if t.stage in ("ready", "arbiter", "failed"))
        month = snap.month_go or 0.0
        frac = min(1.0, month / GO_LIMIT) if GO_LIMIT > 0 else 0.0
        bar = "█" * int(frac * 10) + "░" * (10 - int(frac * 10))
        head = Text()
        head.append("Пульт агентов", style="bold")
        head.append(f" · {hm.clock(snap.now_ms)} · ")
        head.append(f"работают {working}", style="bold green" if working else "")
        head.append(f" · в очереди {queued} · ")
        head.append(f"ждут Claude {claude}", style="bold yellow" if claude else "")
        head.append(" · бот TG: ")
        # Нет процесса hub_bot — «не вижу», а не «упал»: бот мог быть запущен иначе.
        head.append("работает" if bot_alive else "не вижу", style="green" if bot_alive else "yellow")
        head.append("\nДеньги: сегодня задачи ")
        head.append(hm.money((snap.total_go or 0) + (snap.total_usd or 0)), style="bold")
        head.append(f" · все проекты Go {hm.money(snap.all_go)}")
        head.append(f" · месяц Go {hm.money(month)} из $60 [{bar}]")
        head.append(f" · реальные деньги {hm.money(snap.all_usd)}")
        if snap.agy_runs:  # Gemini выключен владельцем — показываем, только если работал
            head.append(f" · Gemini: {snap.agy_runs} запусков / {snap.agy_steps} шагов за 5 ч")
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
        for wid_id in ("#details", "#events"):
            try:
                self.query_one(wid_id).display = not self._narrow
            except Exception:
                log.exception("не переключился узкий режим")

    # --- подробности ---

    def _current_task_id(self) -> str | None:
        try:
            return self.query_one("#tasks", TaskTable).selected_task_id()
        except Exception:
            log.exception("нет таблицы")
            return None

    def _task_snap(self, task_id: str) -> TaskSnap | None:
        snap = self._snap
        if snap is None:
            return None
        return next((t for t in snap.tasks if str(t.id) == task_id), None)

    def _build_detail(self, task_id: str) -> Text:
        """Подробности задачи. Блокирующий I/O (замечания) — звать только из воркера."""
        task = self._task_snap(task_id)
        out = Text()
        if task is None:
            out.append(f"Задача {task_id}\n(нет в снимке)")
            return out
        now = self._snap.now_ms if self._snap else 0
        view = hm.stage_view(task.stage, task.round, task.max_rounds, task.reason, task.reviewers)
        out.append(f"{code_of(task.id)} · {task.title or task.id}\n", style="bold")
        out.append(f"({task.id}, проект {task.project or '—'})\n", style="dim")
        if task.goal:
            out.append("Зачем: ", style="bold")
            out.append(task.goal + "\n")
        out.append("Сейчас: ", style="bold")
        out.append(view.now)
        if task.stage_since_ms:
            out.append(f" · {hm.ago(now - task.stage_since_ms)} в этапе")
        health = hm.health_text(task.pulse, task.stage)
        if health:
            out.append(f" · {task.pulse} {health}")
        out.append("\n")
        path = hm.progress(task.stage)
        if path:
            out.append("Путь: ", style="bold")
            out.append(path + "\n")
        if view.next and view.next != "—":
            out.append("Дальше: ", style="bold")
            out.append(view.next + "\n")
        if task.executor or task.reviewers:
            out.append("Команда: ", style="bold")
            out.append(hm.team(task.executor, task.reviewers) + "\n")
        out.append("Сейчас работают:\n", style="bold")
        fresh = [s for s in sorted(task.sessions or [], key=lambda x: -x.pulse_ms)
                 if now - s.pulse_ms <= WORKING_MS]
        for s in fresh:
            act = hm.activity_text(s.last_activity)
            age = hm.ago(now - s.pulse_ms)
            out.append(f"  {s.pulse} {hm.model_name(s.model, s.provider)} — {hm.role_name(s.role)}"
                       + (f": {hm.fit(act, 90)}" if act else "")
                       + f" ({'только что' if age == 'сейчас' else age + ' назад'})\n")
        if not fresh:
            out.append("  никто — " + (hm.health_text(task.pulse, task.stage) or view.now.lower())
                       + "\n")
        out.append("Потрачено: ", style="bold")
        out.append(hm.money(task.cost_go + task.cost_usd))
        spent = _spent_by_model(task)
        if spent:
            out.append(" — " + ", ".join(spent))
        out.append("\n")
        rnd, findings = self._finding_lines(task_id)
        if findings:
            out.append(f"Замечания проверки (круг {rnd}):\n", style="bold")
            for ln in findings:
                out.append(ln + "\n")
        return out

    def _build_detail_text(self, task_id: str) -> str:
        return self._build_detail(task_id).plain

    async def _do_show_details(self, task_id: str) -> None:
        text = await asyncio.to_thread(self._build_detail, task_id)
        self._details_task = task_id
        try:
            self.query_one("#details-text", Static).update(text)
            # В узком режиме подробности скрыты — не выпячиваем их из-под шторки.
            self.query_one("#details").display = not self._narrow
        except Exception:
            log.exception("не отрисовались подробности")

    def _finding_lines(self, task_id: str) -> tuple[int, list[str]]:
        """Замечания ПОСЛЕДНЕГО круга проверки: (номер круга, строки)."""
        if load_findings is None:
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
        rounds = []
        for f in (Path(wt) / ".agent").glob("review_r*.json"):
            m = _REVIEW_RE.match(f.name)
            if m:
                rounds.append(int(m.group(1)))
        if not rounds:
            return 0, []
        rnd = max(rounds)
        try:
            items = load_findings(Path(wt), round=rnd)
        except Exception:
            log.exception("не читаются замечания")
            return rnd, ["  (не читаются)"]
        sev = {"high": "важное", "medium": "среднее", "low": "мелочь"}
        out = []
        for f in items[:FINDINGS_MAX]:
            try:
                where = f"{Path(f.file).name}:{f.line}" if f.file else ""
                out.append(f"  • [{sev.get(f.severity, f.severity)}] {where} {f.issue[:110]}"
                           f" ({hm.model_name(f.author)})")
            except Exception:
                log.exception("битое замечание")
        if len(items) > FINDINGS_MAX:
            out.append(f"  … и ещё {len(items) - FINDINGS_MAX} (hub findings {task_id})")
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

        self.push_screen(ConfirmAction(f"Остановить задачу {tid}?"), _done)

    def action_merge_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("Сначала выберите задачу", severity="warning")
            return

        def _done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._run_hub_cmd(["merge", tid]), group="cmd",
                                exclusive=True, exit_on_error=False)

        self.push_screen(ConfirmMerge(tid), _done)

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
