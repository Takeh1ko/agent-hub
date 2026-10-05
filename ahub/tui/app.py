"""Dialogs shared with the console (Transcript/Prompt/Confirm/Ask/Help) and the TopApp table screen.

`ahub top` opens the console (ahub.tui.console), not TopApp: --control starts it with the
tasks pane focused. TopApp is legacy with no CLI entry (kept for its tests in test_tui and
test_projects_cost); the console in ahub.tui.console is the interactive UI."""

from __future__ import annotations

from rich.text import Text
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


def _plain(text: str) -> Text:
    """Hub text for a Static: ANSI colours kept, never parsed as markup (a '[' in a report is just a bracket)."""
    return Text.from_ansi(text)


REFRESH_GAP_S = 0.05  # a kept refresh request runs this long after the one that was in flight
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
        self._fit_width()
        self._paint(True)

    def on_resize(self) -> None:
        self._fit_width()

    def _fit_width(self) -> None:
        """The lines are rendered for the width of the pane, not for a fixed 120 columns."""
        box = self.query_one("#live-log", Static)
        self.view.set_width(box.size.width or self.app.size.width)

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
        return app.pulses() if hasattr(app, "pulses") else {}

    def _paint(self, changed: bool) -> None:
        box = self.query_one("#live-box", VerticalScroll)
        if self._resume:
            self._resume, self.view.following = False, True
        else:  # while the human reads above the end, the tail holds
            self.view.following = box.is_vertical_scroll_end
        head = self.view.header(self._pulses())
        if head != self._head:
            self._head = head
            self.query_one("#live-head", Static).update(_plain(head))
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
    """Legacy table screen, no CLI entry (the console in ahub.tui.console is the UI)."""

    CSS = """
    #header { height: 2; background: ansi_default; }
    #mode { height: 1; background: ansi_default; }
    #body { height: 1fr; background: ansi_default; }
    #tasks { width: 3fr; background: ansi_default; }
    #detail { width: 2fr; border-left: solid #ff8700; padding: 0 1; background: ansi_default; }
    #feed { height: 8; border-top: solid #ff8700; background: ansi_default; }
    #dialog { width: 80; height: auto; border: thick #ff8700; background: ansi_default; padding: 1 2; }
    #live-head { height: 1; background: ansi_default; }
    #live-box { height: 1fr; border: round #ff8700; background: ansi_default; }
    #live-log { width: 100%; background: ansi_default; }
    #prompt-box { width: 90%; height: 80%; border: thick #ff8700; background: ansi_default; padding: 1 2; }
    #prompt-text { width: 100%; background: ansi_default; }
    Confirm, Ask, Help { align: center middle; background: ansi_default; }
    Prompt { align: center middle; background: ansi_default; }
    Transcript { align: center middle; background: ansi_default; }
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
        self._pending = False  # a refresh was asked for while one was running — it runs right after
        self._live: dict = {}
        self._pulses: dict = {}
        self._ids: list[int] = []
        self._rows: list[data.Row] = []  # the rows of the last refresh — the cursor's row, header too
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
        self.query_one("#mode", Static).update(_plain(text))

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_data(self) -> None:
        """Rebuild the table. A refresh already running is not cancelled (it is mid-write on the screen):
        the request is remembered and served the moment it ends — `o` must not leave the table stale."""
        if self._busy:
            self._pending = True
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
        self.query_one("#header", Static).update(_plain(screen.header))
        table = self.query_one("#tasks", DataTable)
        row_at = table.cursor_row
        cur = self.selected()
        table.clear()
        self._ids = []
        self._rows = list(screen.rows)
        for r in screen.rows:
            table.add_row(r.mark, r.label, r.kind, r.title[:40], r.state, r.phase, r.model,
                          "" if r.header else str(r.round), r.age, r.cost)
            self._ids.append(r.task_id)
        if cur:  # the same task stays picked when the rows move under it
            if cur in self._ids:
                table.move_cursor(row=self._ids.index(cur))
        elif self._ids and row_at:  # the cursor is on a project header row — keep its place, not the
            table.move_cursor(row=min(row_at, len(self._ids) - 1))  # first group (an empty table: the top)
        self.query_one("#feed", Static).update(_plain("\n".join(screen.feed[-7:]) or _t("tui.no_events")))
        self._show_detail()
        if self._pending:  # a request that arrived while this refresh was running — serve it now
            self._pending = False
            self.set_timer(REFRESH_GAP_S, self.refresh_data)

    def selected(self) -> int | None:
        row = self._row_at_cursor()
        return row.task_id if row is not None and not row.header else None

    def _row_at_cursor(self) -> data.Row | None:
        """The row under the cursor — a project header row too (it carries the name of the group)."""
        table = self.query_one("#tasks", DataTable)
        if not self._ids or table.cursor_row is None or table.cursor_row >= len(self._ids):
            return None
        return self._rows[table.cursor_row] if table.cursor_row < len(self._rows) else None

    def _show_detail(self) -> None:
        row = self._row_at_cursor()
        text = data.detail(self.store, row.task_id, self._live, self._pulses) if row and not row.header else (
            _t("tui.group_header", name=row.label, tasks=row.title) if row else _t("tui.no_tasks"))
        self.query_one("#detail", Static).update(_plain(text))

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
        if tid:  # a project header row (0) has no worker to write to
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
        status = drafts.status(self.store, did)
        self.call_from_thread(self._offer, project, did, preview, status)

    def _offer(self, project: config.ProjectConfig, did: int, preview: str, status: str) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self._do(lambda: _t("draft.queued", tid=drafts.start(self.store, project, did)))
            else:
                drafts.cancel(self.store, did)
                self.notify(_t("tui.draft_cancelled"))
        # the preview text may be localized and may quote the words themselves — readiness by the status
        # code of the row, never by the text of the preview
        if status != drafts.READY:
            self.notify(preview[:300], severity="error", timeout=10)
            return
        self.push_screen(Confirm(preview + "\n\n" + _t("tui.confirm_run")), done)


def main(control: bool = False) -> int:
    TopApp(control=control).run()
    return 0
