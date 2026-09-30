"""`ahub top` — экран для человека (architecture §10). Данные — ahub.tui.data; действия — ahub.accept/drafts.

Тумблер «Просмотр / Управление» (c): в просмотре действия недоступны. Обновление — каждые 2 с в фоне
(поток; пока предыдущее не закончилось — новое не начинается).
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static

from ahub import accept, config, drafts, transitions
from ahub.service import PAUSE_KEY
from ahub.store import Store
from ahub.tui import data


class Confirm(ModalScreen[bool]):
    BINDINGS = [("y", "yes", "да"), ("n", "no", "нет"), ("escape", "no", "нет")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Vertical(Label(self.text), Label("y — да, n — нет"), id="dialog")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class Ask(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "отмена")]

    def __init__(self, prompt: str, placeholder: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Vertical(Label(self.prompt), Input(placeholder=self.placeholder, id="ask"), Label("Enter — ок, Esc — отмена"),
                       id="dialog")

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Help(ModalScreen[None]):
    BINDINGS = [("escape", "close", "закрыть"), ("question_mark", "close", "закрыть"), ("q", "close", "закрыть")]

    def compose(self) -> ComposeResult:
        yield Vertical(Static(data.HELP), id="dialog")

    def action_close(self) -> None:
        self.dismiss(None)


class TopApp(App):
    CSS = """
    #header { height: 2; background: $boost; }
    #mode { height: 1; }
    #body { height: 1fr; }
    #tasks { width: 3fr; }
    #detail { width: 2fr; border-left: solid $primary; padding: 0 1; }
    #feed { height: 8; border-top: solid $primary; }
    #dialog { width: 80; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    Confirm, Ask, Help { align: center middle; }
    """
    BINDINGS = [("q", "quit", "выход"), ("question_mark", "help", "справка"), ("c", "toggle", "просмотр/управление"),
                ("n", "new", "новая"), ("s", "stop", "стоп"), ("a", "accept", "принять"), ("x", "reject", "отклонить"),
                ("r", "rework", "доработать"), ("m", "model", "модель"), ("b", "budget", "бюджет"),
                ("p", "pause", "пауза")]

    def __init__(self, store: Store | None = None, projects: list[config.ProjectConfig] | None = None,
                 control: bool = False) -> None:
        super().__init__()
        self.store = store or Store()
        self._projects = projects
        self.control = control
        self._busy = False
        self._live: dict = {}
        self._pulses: dict = {}
        self._ids: list[int] = []

    def projects(self) -> list[config.ProjectConfig]:
        if self._projects is None:
            self._projects, _ = config.load_projects()
        return self._projects

    def compose(self) -> ComposeResult:
        yield Static("загрузка…", id="header")
        yield Static("", id="mode")
        with Horizontal(id="body"):
            yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
            yield Static("", id="detail")
        yield Static("", id="feed")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#tasks", DataTable)
        for col in ("", "задача", "тип", "что делаем", "сейчас", "фаза", "модель", "круг", "давно", "$"):
            t.add_column(col)
        self._show_mode()
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    def _show_mode(self) -> None:
        text = ("🔓 УПРАВЛЕНИЕ — действия доступны (c — вернуть просмотр)" if self.control
                else "👁 ПРОСМОТР — только смотреть (c — включить управление, ? — справка)")
        self.query_one("#mode", Static).update(text)

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            screen, live, pulses = data.snapshot(self.store, self.projects())
        finally:
            self._busy = False
        self.call_from_thread(self._apply, screen, live, pulses)

    def _apply(self, screen: data.Screen, live: dict, pulses: dict) -> None:
        self._live, self._pulses = live, pulses
        self.query_one("#header", Static).update(screen.header)
        table = self.query_one("#tasks", DataTable)
        cur = self.selected()
        table.clear()
        self._ids = []
        for r in screen.rows:
            table.add_row(r.mark, r.label, r.kind, r.title[:40], r.state, r.phase, r.model, str(r.round), r.age, r.cost)
            self._ids.append(r.task_id)
        if cur in self._ids:
            table.move_cursor(row=self._ids.index(cur))
        self.query_one("#feed", Static).update("\n".join(screen.feed[-7:]) or "событий нет")
        self._show_detail()

    def selected(self) -> int | None:
        table = self.query_one("#tasks", DataTable)
        if not self._ids or table.cursor_row is None or table.cursor_row >= len(self._ids):
            return None
        return self._ids[table.cursor_row]

    def _show_detail(self) -> None:
        tid = self.selected()
        text = data.detail(self.store, tid, self._live, self._pulses) if tid else "нет задач"
        self.query_one("#detail", Static).update(text)

    def on_data_table_row_highlighted(self, ev) -> None:
        self._show_detail()

    # --- действия ---

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_toggle(self) -> None:
        self.control = not self.control
        self._show_mode()

    def _guard(self) -> bool:
        if not self.control:
            self.notify("режим просмотра: действия выключены (c — управление)", severity="warning")
            return False
        return True

    def _project_of(self, tid: int) -> config.ProjectConfig | None:
        t = self.store.get_task(tid)
        return next((p for p in self.projects() if t and p.name == t.project), None)

    def _do(self, fn, ok_text: str | None = None) -> None:
        try:
            msg = fn()
        except (accept.DecisionError, transitions.TransitionError, transitions.ConflictError, ValueError) as e:
            self.notify(str(e)[:300], severity="error", timeout=8)
            return
        self.notify(ok_text or str(msg)[:300])
        self.refresh_data()

    def _confirm_then(self, text: str, fn) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self._do(fn)
        self.push_screen(Confirm(text), done)

    def action_stop(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            self._confirm_then(f"Остановить T{tid}?", lambda: transitions.request_stop(self.store, tid, by="human"))

    def action_accept(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            p = self._project_of(tid)
            self._confirm_then(f"Принять T{tid}? (код будет слит в рабочую ветку)",
                               lambda: accept.accept(self.store, p, tid, by="human"))

    def action_reject(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            p = self._project_of(tid)
            self._confirm_then(f"Отклонить T{tid}?", lambda: accept.reject(self.store, p, tid, by="human"))

    def _ask_then(self, prompt: str, fn, placeholder: str = "") -> None:
        def done(val: str | None) -> None:
            if val:
                self._do(lambda: fn(val))
        self.push_screen(Ask(prompt, placeholder), done)

    def action_rework(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            self._ask_then(f"Что доработать в T{tid}?", lambda v: accept.rework(self.store, tid, v, by="human"))

    def action_model(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            p = self._project_of(tid)
            self._ask_then(f"Модель для T{tid}:", lambda v: accept.change_model(self.store, p, tid, v, by="human"),
                           "spark / mimo-flash / deepseek-flash")

    def action_budget(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            self._ask_then(f"Добавить к бюджету T{tid}, $:",
                           lambda v: accept.extend_budget(self.store, tid, add=float(v.replace(",", ".")), by="human"),
                           "0.5")

    def action_pause(self) -> None:
        if not self._guard():
            return
        if self.store.meta_get(PAUSE_KEY) == "1":
            self.store.meta_del(PAUSE_KEY)
            self.notify("очередь снова работает")
        else:
            self.store.meta_set(PAUSE_KEY, "1")
            self.notify("очередь на паузе")

    def action_new(self) -> None:
        if not self._guard():
            return
        names = [p.name for p in self.projects()]

        def got_project(name: str | None) -> None:
            proj = next((p for p in self.projects() if p.name == (name or names[0])), None)
            if proj is None:
                self.notify(f"нет проекта {name}", severity="error")
                return
            self.push_screen(Ask(f"Задача для {proj.name} — своими словами:"), lambda text: self._draft(proj, text))

        if len(names) == 1:
            got_project(names[0])
        else:
            self.push_screen(Ask("Проект:", " / ".join(names)), got_project)

    def _draft(self, project: config.ProjectConfig, text: str | None) -> None:
        if not text:
            return
        self.notify("модель пишет черновик… (1–3 мин)", timeout=10)
        self._make_draft(project, text)

    @work(thread=True, group="draft")
    def _make_draft(self, project: config.ProjectConfig, text: str) -> None:
        did = drafts.create(self.store, project, text, source="top")
        preview = drafts.preview(self.store, did)
        self.call_from_thread(self._offer, project, did, preview)

    def _offer(self, project: config.ProjectConfig, did: int, preview: str) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self._do(lambda: f"T{drafts.start(self.store, project, did)} в очереди")
            else:
                drafts.cancel(self.store, did)
                self.notify("черновик отменён")
        if "не готов" in preview or ": failed" in preview:
            self.notify(preview[:300], severity="error", timeout=10)
            return
        self.push_screen(Confirm(preview + "\n\nЗапустить?"), done)


def main(control: bool = False) -> int:
    TopApp(control=control).run()
    return 0
