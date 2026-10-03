"""`ahub top` — human screen (architecture §10). Data — ahub.tui.data; actions — ahub.accept/drafts.

"View / Control" toggle (c): no actions in view mode. Refresh every 2 s in the background
(thread; a new refresh never starts before the previous one finishes). The table shows the current
work, grouped by project (a header row opens each project — several repositories share one hub),
`h` adds the history, `o` narrows the table to one project and back to all; `enter`/`t` — a live
transcript of the task (ahub.tui.live): it reads the log and, in control mode, `m` messages the worker.
Every table key waits behind that screen.
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, Static

from ahub import accept, config, drafts, transitions
from ahub.i18n import t as _t
from ahub.service import PAUSE_KEY
from ahub.store import Store
from ahub.tui import data
from ahub.tui.live import LiveView

TABLE_ONLY = ("toggle", "new", "stop", "accept", "reject", "rework", "nudge", "model", "budget", "pause",
              "history", "transcript", "project")  # the keys of the table — they wait behind the transcript
              # screen (that screen has its own m: a message to the worker of the task it shows)


class Confirm(ModalScreen[bool]):
    BINDINGS = [("y", "yes", _t("tui.bind_yes")), ("n", "no", _t("tui.bind_no")),
                ("escape", "no", _t("tui.bind_no"))]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Vertical(Label(self.text), Label(_t("tui.confirm_hint")), id="dialog")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class Ask(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", _t("tui.bind_cancel"))]

    def __init__(self, prompt: str, placeholder: str = "") -> None:
        super().__init__()
        self.prompt = prompt
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Vertical(Label(self.prompt), Input(placeholder=self.placeholder, id="ask"), Label(_t("tui.ask_hint")),
                       id="dialog")

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Help(ModalScreen[None]):
    BINDINGS = [("escape", "close", _t("tui.bind_close")), ("question_mark", "close", _t("tui.bind_close")),
                ("q", "close", _t("tui.bind_close"))]

    def compose(self) -> ComposeResult:
        yield Vertical(Static(_t("tui.help")), id="dialog")

    def action_close(self) -> None:
        self.dismiss(None)


class Prompt(ModalScreen[None]):
    """`p` — the full prompt of the last turn of the session (in the log it is cut to a few lines)."""

    BINDINGS = [("escape", "close", _t("tui.bind_close")), ("q", "close", _t("tui.bind_close"))]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="prompt-box"):
            yield Static(self.text, markup=False, id="prompt-text")

    def action_close(self) -> None:
        self.dismiss(None)


class Transcript(Screen[None]):
    """Live transcript of a task: the lines of the session (ahub/tui/live.py), followed as they come.

    Reads; the only thing it changes is a message to the worker (`m`, control mode). `r` — the other role
    of the round, `[`/`]` — the previous/next round, `p` — the full prompt, `f` — the tail back to the end
    after a scroll up, escape/q — back.
    """

    BINDINGS = [Binding("escape", "back", _t("tui.bind_back")), Binding("q", "back", _t("tui.bind_back")),
                Binding("r", "role", _t("tui.bind_role")),
                Binding("bracketleft", "round_prev", _t("tui.bind_round_prev"), key_display="["),
                Binding("bracketright", "round_next", _t("tui.bind_round_next"), key_display="]"),
                Binding("m", "nudge", _t("tui.bind_nudge")),
                Binding("p", "prompt", _t("tui.bind_prompt")), Binding("f", "follow", _t("tui.bind_follow"))]
    POLL_S = 1.5

    def __init__(self, store: Store, task_id: int) -> None:
        super().__init__()
        self.view = LiveView(store, task_id)
        self._busy = False
        self._head = ""
        self._resume = False  # `f` — the tail goes back to the end

    def compose(self) -> ComposeResult:
        yield Static("", id="live-head")
        with VerticalScroll(id="live-box"):
            yield Static("", markup=False, id="live-log")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(self.POLL_S, self.refresh_live)
        self._paint(True)

    @work(thread=True, exclusive=True, group="live")
    def refresh_live(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            changed = self.view.update()
        except OSError:  # the log or the database is not readable right now — try at the next tick
            changed = False
        finally:
            self._busy = False
        self.app.call_from_thread(self._paint, changed)

    def _pulses(self) -> dict:
        app = self.app
        return app.pulses() if isinstance(app, TopApp) else {}

    def _paint(self, changed: bool) -> None:
        box = self.query_one("#live-box", VerticalScroll)
        if self._resume:
            self._resume, self.view.following = False, True
        else:  # while the human reads above the end, the tail holds
            self.view.following = box.is_vertical_scroll_end
        head = self.view.header(self._pulses())
        if head != self._head:
            self._head = head
            self.query_one("#live-head", Static).update(head)
        if not changed:
            return
        self.query_one("#live-log", Static).update("\n".join(self.view.lines) or _t("tui.loading"))
        if self.view.following:
            box.call_after_refresh(box.scroll_end, animate=False)

    def action_back(self) -> None:
        self.dismiss(None)

    def action_role(self) -> None:
        if self.view.switch_role():
            self._paint(True)

    def action_round_prev(self) -> None:
        if self.view.step_round(-1):
            self._paint(True)

    def action_round_next(self) -> None:
        if self.view.step_round(1):
            self._paint(True)

    def action_prompt(self) -> None:
        self.app.push_screen(Prompt(self.view.prompt()))

    def action_nudge(self) -> None:
        app = self.app
        if isinstance(app, TopApp):
            app.ask_nudge(self.view.task_id)

    def action_follow(self) -> None:
        self._resume = True
        self._paint(True)


class TopApp(App):
    CSS = """
    #header { height: 2; background: $boost; }
    #mode { height: 1; }
    #body { height: 1fr; }
    #tasks { width: 3fr; }
    #detail { width: 2fr; border-left: solid $primary; padding: 0 1; }
    #feed { height: 8; border-top: solid $primary; }
    #dialog { width: 80; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #live-head { height: 1; background: $boost; }
    #live-box { height: 1fr; border: round $primary; }
    #live-log { width: 100%; }
    #prompt-box { width: 90%; height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    #prompt-text { width: 100%; }
    Confirm, Ask, Help { align: center middle; }
    Prompt { align: center middle; }
    Transcript { align: center middle; }
    """
    BINDINGS = [("q", "quit", _t("tui.bind_quit")), ("question_mark", "help", _t("tui.bind_help")),
                ("c", "toggle", _t("tui.bind_toggle")), ("n", "new", _t("tui.bind_new")),
                ("s", "stop", _t("tui.bind_stop")), ("a", "accept", _t("tui.bind_accept")),
                ("x", "reject", _t("tui.bind_reject")), ("r", "rework", _t("tui.bind_rework")),
                ("m", "nudge", _t("tui.bind_nudge")),
                Binding("M", "model", _t("tui.bind_model")),
                ("b", "budget", _t("tui.bind_budget")),
                ("p", "pause", _t("tui.bind_pause")), ("h", "history", _t("tui.bind_history")),
                ("o", "project", _t("tui.bind_project")),
                ("t", "transcript", _t("tui.bind_transcript"))]

    def __init__(self, store: Store | None = None, projects: list[config.ProjectConfig] | None = None,
                 control: bool = False) -> None:
        super().__init__()
        self.store = store or Store()
        self._projects = projects
        self.control = control
        self.history = False  # the table by default shows the current work, not the finished ones
        self.project = ""  # "" — every project; `o` narrows the table to one of them
        self._busy = False
        self._live: dict = {}
        self._pulses: dict = {}
        self._ids: list[int] = []
        self._names: list[str] = []  # the projects of the last refresh — the order of the `o` key

    def pulses(self) -> dict:
        """The pulses of the last refresh (the transcript screen shows the pulse in its header)."""
        return self._pulses

    def projects(self) -> list[config.ProjectConfig]:
        if self._projects is None:
            self._projects, _ = config.load_projects()
        return self._projects

    def compose(self) -> ComposeResult:
        yield Static(_t("tui.loading"), id="header")
        yield Static("", id="mode")
        with Horizontal(id="body"):
            yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
            yield Static("", id="detail")
        yield Static("", id="feed")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#tasks", DataTable)
        for col in ("", _t("tui.col_task"), _t("tui.col_kind"), _t("tui.col_title"), _t("tui.col_state"),
                    _t("tui.col_phase"), _t("tui.col_model"), _t("tui.col_round"), _t("tui.col_age"), "$"):
            t.add_column(col)
        self._show_mode()
        self.refresh_data()
        self.set_interval(2.0, self.refresh_data)

    def _show_mode(self) -> None:
        text = (_t("tui.mode_control") if self.control else _t("tui.mode_view"))
        if self.project:
            text += " · " + _t("tui.filter_project", name=self.project)
        self.query_one("#mode", Static).update(text)

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            screen, live, pulses = data.snapshot(self.store, self.projects(), history=self.history,
                                                 only=self.project)
        finally:
            self._busy = False
        self.call_from_thread(self._apply, screen, live, pulses)

    def _apply(self, screen: data.Screen, live: dict, pulses: dict) -> None:
        self._live, self._pulses = live, pulses
        self._names = screen.projects
        self.query_one("#header", Static).update(screen.header)
        table = self.query_one("#tasks", DataTable)
        cur = self.selected()
        table.clear()
        self._ids = []
        for r in screen.rows:
            table.add_row(r.mark, r.label, r.kind, r.title[:40], r.state, r.phase, r.model, str(r.round),
                          r.age, r.cost)
            self._ids.append(r.task_id)
        if cur in self._ids:
            table.move_cursor(row=self._ids.index(cur))
        self.query_one("#feed", Static).update("\n".join(screen.feed[-7:]) or _t("tui.no_events"))
        self._show_detail()

    def selected(self) -> int | None:
        table = self.query_one("#tasks", DataTable)
        if not self._ids or table.cursor_row is None or table.cursor_row >= len(self._ids):
            return None
        return self._ids[table.cursor_row]

    def _show_detail(self) -> None:
        tid = self.selected()
        text = data.detail(self.store, tid, self._live, self._pulses) if tid else _t("tui.no_tasks")
        self.query_one("#detail", Static).update(text)

    def on_data_table_row_highlighted(self, ev) -> None:
        self._show_detail()

    def on_data_table_row_selected(self, ev) -> None:  # enter on a row
        self.action_transcript()

    # --- actions ---

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_history(self) -> None:
        self.history = not self.history
        self.refresh_data()

    def action_project(self) -> None:
        """`o` — the table of one project; again — the next one, and after the last back to all."""
        order = [""] + self._names
        if len(order) == 1:  # nothing to narrow to
            return
        cur = order.index(self.project) if self.project in order else 0
        self.project = order[(cur + 1) % len(order)]
        self._show_mode()
        self.refresh_data()

    def action_transcript(self) -> None:
        tid = self.selected()
        if tid:  # a project header row (0) has no task to open
            self.push_screen(Transcript(self.store, tid))

    def action_toggle(self) -> None:
        self.control = not self.control
        self._show_mode()

    def _on_table(self) -> bool:
        """False while the transcript screen is on top: the table actions wait behind it."""
        return not isinstance(self.screen, Transcript)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """The keys of the table are not offered and not run while the transcript screen is open."""
        return False if action in TABLE_ONLY and not self._on_table() else True

    def _guard(self) -> bool:
        """Every action that changes something: the control mode is on and the transcript screen is closed."""
        if not self._on_table():
            return False
        if not self.control:
            self.notify(_t("tui.view_only"), severity="warning")
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
            self._confirm_then(_t("tui.confirm_stop", tid=tid),
                               lambda: transitions.request_stop(self.store, tid, by="human"))

    def action_accept(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            p = self._project_of(tid)
            self._confirm_then(_t("tui.confirm_accept", tid=tid),
                               lambda: accept.accept(self.store, p, tid, by="human"))

    def action_reject(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            p = self._project_of(tid)
            self._confirm_then(_t("tui.confirm_reject", tid=tid), lambda: accept.reject(self.store, p, tid, by="human"))

    def _ask_then(self, prompt: str, fn, placeholder: str = "") -> None:
        def done(val: str | None) -> None:
            if val:
                self._do(lambda: fn(val))
        self.push_screen(Ask(prompt, placeholder), done)

    def action_rework(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            self._ask_then(_t("tui.ask_rework", tid=tid), lambda v: accept.rework(self.store, tid, v, by="human"))

    def action_model(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            p = self._project_of(tid)
            self._ask_then(_t("tui.ask_model", tid=tid),
                           lambda v: accept.change_model(self.store, p, tid, v, by="human"),
                           "spark / mimo-flash / deepseek-flash")

    def ask_nudge(self, tid: int) -> None:
        """A message to a working task (`m` — from the table and from the transcript screen)."""
        if not self.control:
            self.notify(_t("tui.view_only"), severity="warning")
            return

        def send(text: str) -> str:
            transitions.request_nudge(self.store, tid, text=text, by="human")
            return _t("task.nudge_requested", label=f"T{tid}")

        self._ask_then(_t("tui.ask_nudge", tid=tid), send)

    def action_nudge(self) -> None:
        tid = self.selected()
        if tid is not None:
            self.ask_nudge(tid)

    def action_budget(self) -> None:
        tid = self.selected()
        if tid and self._guard():
            self._ask_then(_t("tui.ask_budget", tid=tid),
                           lambda v: accept.extend_budget(self.store, tid, add=float(v.replace(",", ".")), by="human"),
                           "0.5")

    def action_pause(self) -> None:
        if not self._guard():
            return
        if self.store.meta_get(PAUSE_KEY) == "1":
            self.store.meta_del(PAUSE_KEY)
            self.notify(_t("service.paused_off"))
        else:
            self.store.meta_set(PAUSE_KEY, "1")
            self.notify(_t("service.paused_on"))

    def action_new(self) -> None:
        if not self._guard():
            return
        names = [p.name for p in self.projects()]

        def got_project(name: str | None) -> None:
            proj = next((p for p in self.projects() if p.name == (name or names[0])), None)
            if proj is None:
                self.notify(_t("tui.no_project", name=name), severity="error")
                return
            self.push_screen(Ask(_t("tui.ask_task_for", name=proj.name)), lambda text: self._draft(proj, text))

        if len(names) == 1:
            got_project(names[0])
        else:
            self.push_screen(Ask(_t("tui.ask_project"), " / ".join(names)), got_project)

    def _draft(self, project: config.ProjectConfig, text: str | None) -> None:
        if not text:
            return
        self.notify(_t("tui.draft_writing"), timeout=10)
        self._make_draft(project, text)

    @work(thread=True, group="draft")
    def _make_draft(self, project: config.ProjectConfig, text: str) -> None:
        did = drafts.create(self.store, project, text, source="top")
        preview = drafts.preview(self.store, did)
        self.call_from_thread(self._offer, project, did, preview)

    def _offer(self, project: config.ProjectConfig, did: int, preview: str) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self._do(lambda: _t("draft.queued", tid=drafts.start(self.store, project, did)))
            else:
                drafts.cancel(self.store, did)
                self.notify(_t("tui.draft_cancelled"))
        # drafts preview text may be localized: readiness — by status code, not by text
        if ": failed" in preview or ": drafting" in preview or ": cancelled" in preview:
            self.notify(preview[:300], severity="error", timeout=10)
            return
        self.push_screen(Confirm(preview + "\n\n" + _t("tui.confirm_run")), done)


def main(control: bool = False) -> int:
    TopApp(control=control).run()
    return 0
