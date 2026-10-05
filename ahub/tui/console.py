"""Interactive console: shell for K1 (layout, focus model, follow, bare-TTY gate target).

Layout top to bottom: welcome box, alert strip, task blocks pane (diffed refresh),
event transcript (capped), live status line, input box (always focused), footer.
Read-only commands here: /follow /status /history /help /quit (+ /project /all to switch scope).
Mutating commands (/accept etc.) arrive in the next task — unknown input shows /help.

Data comes from snapshots off the UI thread; a slow snapshot keeps the old view and
marks the footer stale. Every widget renders through _safe() so one failure shows
"✗ <widget>: <hint>" in place and the app keeps running.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

import ahub
from ahub import comms, config, cost, events, pulse, reasons, scope, ui, views
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION, Ev, State, parse_task_id
from ahub.service import HEARTBEAT_KEY, live_workers
from ahub.store import Store, Task
from ahub.time import now_ms

TRANSCRIPT_CAP = 500  # event transcript lines kept
REFRESH_S = 2.0
SNAPSHOT_DEADLINE_S = 1.5
QUIT_GAP_S = 2.0  # second Ctrl+C within this window exits
CURRENT = ACTIVE | WAITING_DECISION | {State.QUEUED}
FEED_KINDS = {Ev.DONE.value, Ev.NEEDS_DECISION.value, Ev.ERROR.value, Ev.ANSWER.value,
              Ev.OWNER_MESSAGE.value}


def _pane_w(width: int) -> int:
    """Content width of a bordered pane: margin 0 1 + round border + scrollbar slack."""
    return max(20, width - 6)


def _elapsed_inner(ms: int) -> str:
    """Elapsed without parens for embedding: ui.elapsed() is the one source."""
    return ui.elapsed(ms)[1:-1]


def _safe(widget: str, fn) -> str:
    """Render one widget; a failure shows "✗ <widget>: <hint>" in place and the console keeps running."""
    try:
        return fn()
    except Exception as e:
        return _t("console.widget_error", widget=widget, hint=str(e)[:100])


def status_of(task: Task) -> str:
    """Console status bucket for the ⏺ colour: working/success/waiting/error."""
    if task.state is State.ERROR:
        return "error"
    if task.state in WAITING_DECISION:
        return "waiting"
    if task.state in ACTIVE or task.state is State.QUEUED:
        return "working"
    return "success"


def task_lines(task: Task, pl, now: int, width: int) -> list[str]:
    """One task block as one-line rows, each clipped by display width.

    width is the pane content width (see _pane_w). ⏺ line: id + title; first ⎿:
    phase/tool · model · elapsed; second ⎿ only when the pulse is not green;
    waiting tasks add the exact Next line (views source).
    """
    width = max(20, width)
    body_w = width - 4
    if task.state is State.ERROR:
        mark = ui.styled("✗", "red")
    else:
        mark = ui.status_mark("⏺", status_of(task))
    head = ui.clip_width(f"{task.label}  {task.title}", body_w)
    out = [f"{mark} {head}"]
    tool = (pl.active_tool if pl is not None else "") or ""
    phase = tool or (views.PHASE_WORDS.get(task.phase, task.phase) if task.phase else "")
    if not phase and task.state in ACTIVE:
        phase = views.state_word(task.state)
    detail = " · ".join(x for x in (phase, task.executor or "—",
                                    _elapsed_inner(max(0, now - task.updated_at))) if x)
    if detail:
        out.append(f"  {ui.styled('⎿', 'dim')} {ui.styled(ui.clip_width(detail, body_w), 'dim')}")
    if pl is not None and pl.state != "working" and pl.reason:
        out.append(f"  {ui.styled('⎿', 'dim')} {ui.styled(ui.clip_width(pl.reason, body_w), 'dim')}")
    if task.state in WAITING_DECISION and views._offers_next(task):
        nxt = _t(views.next_key(task), label=task.label)
        out.append(f"  {ui.styled('⎿', 'dim')} {ui.styled(ui.clip_width(nxt, body_w), 'dim')}")
    return [ln for ln in out if ln]


def welcome_box(store: Store, sc: scope.Scope, now: int, width: int) -> str:
    """Rounded welcome box with accent borders: title, project, service, Go month line."""
    from ahub.tui import data as topdata

    try:
        projects, _ = config.load_projects()
        by_name = {p.name: p for p in projects}
    except Exception:
        by_name = {}
    if sc.all:
        proj_line = _t("console.welcome_all")
    else:
        name = sc.name
        path = by_name.get(name).root if name in by_name else ""
        proj_line = _t("console.welcome_project", name=name or "—", path=path or "—")
    hb = store.meta_get(HEARTBEAT_KEY)
    alive = bool(hb) and now - int(hb) < 30_000
    try:
        age = max(0, (now - int(hb)) // 1000) if hb else 0
    except (TypeError, ValueError):
        age = 0
    state = _t("console.svc_running") if alive else _t("console.svc_stopped")
    svc_line = _t("console.svc_tick", state=state, age=age)
    try:
        money = topdata.header(store, now).split("\n")[-1]
    except Exception:
        money = ""
    title = f"{ui.styled('✻', 'accent')} {ui.styled(f'ahub {ahub.__version__}', 'accent')}"
    clip = max(20, width - 6)  # the #welcome pane: margin 0 1, no border
    lines = [title, ui.styled(ui.clip_width(proj_line, clip), "dim"),
             ui.styled(ui.clip_width(svc_line, clip), "dim")]
    if money:
        lines.append(ui.styled(ui.clip_width(money, clip), "dim"))
    return ui.box(lines, w=width - 2, border="accent")


def alert_text(store: Store, sc: scope.Scope | None, width: int) -> str:
    """One dim alert line when alarms or open questions exist, else empty.

    Store failures propagate: snapshot() wraps this in _safe() so the strip
    renders "✗ alerts: <hint>" in place instead of silently vanishing.
    """
    alarms = comms.alarms(store, scope=sc)
    questions = comms.open_questions(store, scope=sc)
    if not alarms and not questions:
        return ""
    latest = ""
    try:
        if alarms:
            latest = str((alarms[-1].payload or {}).get("text") or "")[:120]
        if not latest and questions:
            latest = str(questions[-1].get("text") or "")[:120]
    except Exception:
        latest = ""
    line = _t("console.alerts", alarms=len(alarms), questions=len(questions),
              latest=ui.clip_width(latest or "—", max(10, width - 40)))
    return ui.styled(ui.clip_width(line, max(20, width - 2)), "dim")


def live_text(store: Store, sc: scope.Scope | None, now: int,
              width: int, frame: int = 0) -> str:
    """One live line for the most recently active task, empty when nothing runs."""
    try:
        tasks = store.list_tasks(states=ACTIVE, projects=(sc.projects if sc and not sc.all else None))
    except Exception:
        return ""
    if not tasks:
        return ""
    tasks.sort(key=lambda t: t.updated_at, reverse=True)
    t = tasks[0]
    marks = ui.SPINNER
    mark = ui.styled(marks[frame % len(marks)], "accent")
    verb = _t("console.live_verb")
    edad = _elapsed_inner(max(0, now - t.updated_at))
    line = _t("console.live", mark=mark, verb=verb, label=t.label, elapsed=edad,
              model=t.executor or "—")
    return ui.clip_width(line, max(20, width - 2))


def footer_text(store: Store, sc: scope.Scope, width: int) -> str:
    """Dim footer: left help, right working count + month money (Go and USD never summed)."""
    try:
        active = store.list_tasks(states=ACTIVE, projects=(None if sc.all else sc.projects or None))
        n = len(active)
    except Exception:
        n = 0
    try:
        money = cost.total(store, scope=sc, since=cost.month_start())
        go, usd = money.go, money.usd
    except Exception:
        go, usd = 0.0, 0.0
    scope_name = _t("console.footer_scope_all") if sc.all else (sc.name or "—")
    right = _t("console.footer_status", n=n, scope=scope_name, go=f"{go:.2f}", usd=f"{usd:.2f}")
    left = _t("console.footer_help")
    avail = max(20, width - 2)  # #footer has margin 0 1, no border
    if ui.plain_len(left) + 2 + ui.plain_len(right) <= avail:
        gap = avail - ui.plain_len(left) - ui.plain_len(right)
        return ui.styled(left + " " * gap + right, "dim")
    return ui.styled(ui.clip_width(left + " · " + right, avail), "dim")


def event_lines(ev, task: Task | None, width: int) -> list[str]:
    """One feed event as ⏺ statement + ⎿ evidence, each a single visual line.

    width is the pane content width (see _pane_w). The statement names the task;
    the evidence carries the reason/summary/answer/message — never twice.
    """
    width = max(20, width)
    body_w = width - 4
    p = ev.payload or {}
    try:
        k = Ev(ev.kind)
        code = events.EVENT_CODES.get(k, ev.kind.upper())
    except Exception:
        k = None
        code = str(ev.kind).upper()

    if task is not None:
        head = f"{task.label} {task.kind.value} «{ui.clip(task.title, 50)}»"
    else:
        head = f"T{ev.task_id}" if ev.task_id else ""

    stmt = f"{code} {head}".strip()
    evidence = ""
    try:
        if k is Ev.DONE:
            extra = []
            if p.get("summary"):
                extra.append(str(p["summary"]))
            if p.get("report_bytes"):
                extra.append(_t("events.report", kb=f"{p['report_bytes'] / 1024:.1f}"))
            m = events._money(p)
            if m:
                extra.append(m)
            evidence = "; ".join(extra)
        elif k in (Ev.NEEDS_DECISION, Ev.ERROR):
            evidence = reasons.text(str(p.get("reason") or ""))
        elif k is Ev.OWNER_MESSAGE:
            evidence = str(p.get("text") or "")
        elif k is Ev.ANSWER:
            q = str(p.get("question") or "")
            ans = str(p.get("answer") or "")
            qid = p.get("question_id", "?")
            stmt = f"{code} #{qid} «{ui.clip(q, 50)}»" if q else f"{code} #{qid}"
            evidence = ans
    except Exception:
        evidence = ""

    mark = ui.status_mark("⏺", "success")
    if ev.kind == Ev.ERROR.value:
        mark = ui.styled("✗", "red")
    elif ev.kind in (Ev.NEEDS_DECISION.value, Ev.ANSWER.value, Ev.OWNER_MESSAGE.value):
        mark = ui.status_mark("⏺", "waiting")
    out = [f"{mark} {ui.clip_width(stmt, body_w)}"]
    if evidence and str(evidence).strip():
        out.append(f"  {ui.styled('⎿', 'dim')} {ui.styled(ui.clip_width(evidence, body_w), 'dim')}")
    return out


@dataclass
class Snapshot:
    welcome: str = ""
    alerts: str = ""
    blocks: list[tuple[str, str]] = field(default_factory=list)  # (key, rendered block)
    live: str = ""
    footer: str = ""
    task_ids: list[int] = field(default_factory=list)
    pulses: dict = field(default_factory=dict)


def snapshot(store: Store, sc: scope.Scope, width: int, now: int, frame: int = 0) -> Snapshot:
    """Whole console data except the append-only transcript (pure, testable)."""
    snap = Snapshot()
    snap.welcome = _safe("welcome", lambda: welcome_box(store, sc, now, width))
    snap.alerts = _safe("alerts", lambda: alert_text(store, sc, width))
    try:
        live = live_workers()
    except Exception:
        live = {}
    try:
        projects, _ = config.load_projects()
    except Exception:
        projects = []
    try:
        pulses = pulse.all_pulses(store, live=live, projects=projects, now=now)
    except Exception:
        pulses = {}
    snap.pulses = pulses
    projs = None if sc.all else (sc.projects or None)
    try:
        tasks = store.list_tasks(states=CURRENT, projects=projs)
    except Exception:
        tasks = []
    # scope order: project heading groups in /all mode
    by_proj: dict[str, list[Task]] = {}
    for t in tasks:
        by_proj.setdefault(t.project, []).append(t)
    blocks: list[tuple[str, str]] = []
    task_ids: list[int] = []
    multi = sc.all and len(by_proj) > 1
    pw = _pane_w(width)
    for proj in sorted(by_proj):
        items = sorted(by_proj[proj], key=lambda t: t.id)
        if multi:
            blocks.append((f"head:{proj}", ui.styled(proj or "—", "dim")))
        for t in items:
            task_ids.append(t.id)
            lines = _safe(f"T{t.id}", lambda t=t: task_lines(t, pulses.get(t.id), now, pw))
            blocks.append((f"T{t.id}", "\n".join(lines)))
    if not tasks:
        blocks.append(("empty", ui.styled(_t("console.no_tasks"), "dim")))
    snap.blocks = blocks
    snap.task_ids = task_ids
    snap.live = _safe("live", lambda: live_text(store, sc, now, width, frame))
    snap.footer = _safe("footer", lambda: footer_text(store, sc, width))
    return snap


def parse_command(text: str) -> tuple[str, list[str]]:
    """Split an input line into (command, args); plain text is ('draft', [text])."""
    s = (text or "").strip()
    if not s:
        return ("", [])
    if not s.startswith("/"):
        return ("draft", [s])
    parts = s[1:].split()
    return (parts[0].lower() if parts else "", parts[1:])


# --- textual app ---

def _plain(text: str):
    from rich.text import Text as _Text

    return _Text.from_ansi(text)


class ConsoleInput(Input):
    """Input widget: '?' on empty input toggles shortcuts without inserting '?'; Up/Down history."""

    async def _on_key(self, event) -> None:
        app = self.app
        if isinstance(app, ConsoleApp):
            if event.key in ("up", "down"):
                event.prevent_default()
                event.stop()
                app.action_history_step(-1 if event.key == "up" else 1)
                return
            is_qm = event.key == "question_mark" or getattr(event, "character", None) == "?"
            if is_qm and not (self.value or "").strip():
                event.prevent_default()
                event.stop()
                app.action_toggle_shortcuts()
                return
        await super()._on_key(event)


class ConsoleApp(App):
    """Bare-TTY console: the same app for `ahub` with no args and `ahub top`."""

    CSS = """
    #welcome { height: auto; margin: 0 1; }
    #alerts { height: 1; margin: 0 1; }
    #tasks { height: 1fr; border: round #ff8700; margin: 0 1; }
    #transcript { height: 8; border: round #ff8700; margin: 0 1; }
    #live { height: 1; margin: 0 1; }
    #input { margin: 0 1; border: round #ff8700; }
    #footer { height: 1; margin: 0 1; }
    #shortcuts { height: auto; margin: 0 1; }
    #dialog { width: 80; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #live-head { height: 1; background: $boost; }
    #live-box { height: 1fr; border: round $primary; }
    #live-log { width: 100%; }
    #prompt-box { width: 90%; height: 80%; border: thick $primary; background: $surface; padding: 1 2; }
    #prompt-text { width: 100%; }
    Confirm, Ask, Help { align: center middle; }
    Prompt { align: center middle; }
    Transcript { align: center middle; }
    Transcript Footer { display: none; }
    """
    # the pane/input borders above are the accent (ui 38;5;208 = #ff8700):
    # ✻, box borders, the product name — nothing else.

    def __init__(self, store: Store | None = None, all_projects: bool = False,
                 project: str | None = None, control: bool = False) -> None:
        super().__init__()
        self.store = store or Store()
        if all_projects:
            self.scope = scope.Scope()
        elif project:
            self.scope = scope.Scope((project,))
        else:
            try:
                self.scope = scope.of_dir(Path.cwd())
            except Exception:
                self.scope = scope.Scope()
        self._blocks: dict[str, str] = {}
        self._order: list[str] = []
        self._task_ids: list[int] = []
        self._selected = 0
        self.focus_mode = "tasks" if control else "input"
        self._history: list[str] = []
        self._hist_at = 0
        self._transcript: list[str] = []
        self._last_event = 0
        self._frame = 0
        self._stale_s = 0
        self._last_w = 80
        self._show_shortcuts = False
        self._quit_at = 0.0
        self._busy = False
        self._last_rendered_tasks = ""
        self._last_footer = ""
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="console-snap")

    def on_unmount(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def pulses(self) -> dict:
        """Pulses of the last refresh (the Transcript screen reads them)."""
        return getattr(self, "_pulses", {})

    def compose(self) -> ComposeResult:
        yield Static("", id="welcome")
        yield Static("", id="alerts")
        with VerticalScroll(id="tasks"):
            yield Static("", id="tasks-inner", markup=False)
        with VerticalScroll(id="transcript"):
            yield Static("", id="transcript-inner", markup=False)
        yield Static("", id="live")
        yield Static("", id="shortcuts")
        yield ConsoleInput(placeholder=_t("console.input_placeholder"), id="input")
        yield Static("", id="footer")  # the dim console footer — the only one, no textual bar

    def on_mount(self) -> None:
        try:
            self._last_event = self.store.last_event_id()
        except Exception:
            self._last_event = 0
        self.refresh_data()
        self.set_interval(REFRESH_S, self.refresh_data)
        if self.focus_mode == "tasks":
            self.action_focus_tasks()
        else:
            try:
                self.query_one("#input", Input).focus()
            except Exception:
                pass

    def _width(self) -> int:
        try:
            w = self.size.width or 0
        except Exception:
            w = 0
        return max(40, w or ui.width())

    @work(thread=True, exclusive=True, group="console")
    def refresh_data(self) -> None:
        if self._busy:
            return
        self._busy = True
        started = time.monotonic()
        try:
            w = self._width()
            self._last_w = w
            now = now_ms()
            future = self._executor.submit(snapshot, self.store, self.scope, w, now, self._frame)
            try:
                snap = future.result(timeout=SNAPSHOT_DEADLINE_S)
            except (TimeoutError, FutureTimeoutError):
                took = time.monotonic() - started
                self._stale_s = max(1, int(took))
                self.call_from_thread(self._apply_stale, self._stale_s)
                return
            self._stale_s = 0
            try:
                new_lines = self._feed_lines(w)
            except Exception:
                new_lines = []
            self.call_from_thread(self._apply, snap, new_lines)
        except Exception as e:
            err = _t("console.widget_error", widget="refresh", hint=str(e)[:100])
            self.call_from_thread(self._apply_error, err)
        finally:
            self._busy = False

    def _safe_update(self, selector: str, widget_name: str, get_text, display: bool = True) -> None:
        """One widget update that never kills the app. A build failure renders
        "✗ <widget>: <hint>" in place; if update() itself rejects the renderable,
        retry once with plain text (no ANSI parsing), then give up quietly."""
        try:
            w = self.query_one(selector, Static)
        except Exception:
            return
        if not display:
            try:
                w.display = False
            except Exception:
                pass
            return
        try:
            text = get_text()
        except Exception as e:
            text = _t("console.widget_error", widget=widget_name, hint=str(e)[:100])
        try:
            w.update(_plain(text))
            w.display = True
        except Exception:
            try:
                w.update(_t("console.widget_error", widget=widget_name, hint="update failed"))
                w.display = True
            except Exception:
                pass

    def _apply_stale(self, stale_s: int) -> None:
        """Keep the existing view on snapshot deadline timeout, updating only the footer."""
        stale_note = _t("console.stale", n=stale_s)
        footer = f"{self._last_footer} · {stale_note}" if self._last_footer else ui.styled(stale_note, "dim")
        avail = max(20, (self._last_w or 80) - 2)
        text = footer if ui.plain_len(footer) <= avail else ui.clip_width(footer, avail)
        self._safe_update("#footer", "footer", lambda: text)

    def _render_tasks(self) -> None:
        sel_key = (
            f"T{self._task_ids[self._selected]}"
            if (self.focus_mode == "tasks" and self._task_ids and 0 <= self._selected < len(self._task_ids))
            else None
        )
        rendered = []
        for k in self._order:
            raw = self._blocks.get(k, "")
            if k == sel_key and raw:
                lines = raw.split("\n")
                lines[0] = f"{ui.styled('▌', 'accent')} {lines[0]}"
                rendered.append("\n".join(lines))
            else:
                rendered.append(raw)
        text = "\n".join(rendered)
        if text != self._last_rendered_tasks:
            self._last_rendered_tasks = text
            self._safe_update("#tasks-inner", "tasks", lambda: text)

    def _feed_lines(self, width: int) -> list[str]:
        """New DONE/DECISION/ERROR/ANSWER/OWNER lines since the last refresh."""
        pw = _pane_w(width)
        try:
            evs = self.store.events(after_id=self._last_event)
        except Exception:
            return []
        out: list[str] = []
        cache: dict[int, Task | None] = {}
        for e in evs:
            if e.kind not in FEED_KINDS:
                continue
            if self.scope is not None and e.project not in self.scope:
                continue
            t = None
            try:
                if e.task_id:
                    if e.task_id not in cache:
                        cache[e.task_id] = self.store.get_task(e.task_id)
                    t = cache[e.task_id]
            except Exception:
                t = None
            try:
                out.extend(event_lines(e, t, pw))
            except Exception:
                continue
        if evs:
            try:
                self._last_event = max(self._last_event, max(e.id for e in evs))
            except ValueError:
                pass
        return out

    def _apply(self, snap: Snapshot, new_lines: list[str]) -> None:
        # never touches the input's focus or content
        try:
            input_has_focus = self.focus_mode == "input"
        except Exception:
            input_has_focus = True
        self._safe_update("#welcome", "welcome", lambda: snap.welcome)
        self._safe_update("#alerts", "alerts", lambda: snap.alerts, display=bool(snap.alerts))
        # diffed task blocks
        self._blocks = dict(snap.blocks)
        self._order = [k for k, _ in snap.blocks]
        self._task_ids = list(snap.task_ids)
        self._pulses = snap.pulses
        self._render_tasks()
        if new_lines:
            self._transcript.extend(new_lines)
            if len(self._transcript) > TRANSCRIPT_CAP:
                self._transcript = self._transcript[-TRANSCRIPT_CAP:]
        self._paint_transcript()
        self._safe_update("#live", "live", lambda: snap.live, display=bool(snap.live))
        self._frame += 1
        self._last_footer = snap.footer
        self._safe_update("#footer", "footer", lambda: snap.footer)
        self._safe_update("#shortcuts", "shortcuts", lambda: _t("console.shortcuts"), display=self._show_shortcuts)
        if input_has_focus and self.focus_mode == "input":
            try:
                self.query_one("#input", Input).focus()
            except Exception:
                pass

    def _paint_transcript(self) -> None:
        try:
            box = self.query_one("#transcript", VerticalScroll)
            at_end = box.is_vertical_scroll_end
        except Exception:
            at_end = True
            box = None
        lines = list(self._transcript[-TRANSCRIPT_CAP:])
        if not lines:
            text = ui.styled(_t("console.transcript_empty"), "dim")
        else:
            head = (
                ui.styled(_t("console.transcript_earlier"), "dim") + "\n"
                if len(self._transcript) >= TRANSCRIPT_CAP
                else ""
            )
            text = head + "\n".join(lines)
        pw = _pane_w(self._width())
        text = "\n".join(ln if ui.plain_len(ln) <= pw else ui.clip_width(ln, pw)
                         for ln in text.split("\n"))
        self._safe_update("#transcript-inner", "transcript", lambda: text)
        if at_end and box is not None:
            try:
                box.call_after_refresh(box.scroll_end, animate=False)
            except Exception:
                pass

    def _apply_error(self, err: str) -> None:
        try:
            self.query_one("#footer", Static).update(_plain(err))
        except Exception:
            pass

    # --- focus model ---

    def action_focus_tasks(self) -> None:
        """Tab: move focus into the task blocks pane (repeated Tab keeps the selection)."""
        if not self._on_main():
            return
        if self.focus_mode != "tasks":
            self.focus_mode = "tasks"
            self._selected = 0
        self._render_tasks()
        try:
            self.query_one("#tasks", VerticalScroll).focus()
        except Exception:
            pass

    def action_focus_input(self) -> None:
        """Esc: back to the input."""
        if not self._on_main():
            return
        self.focus_mode = "input"
        self._render_tasks()
        try:
            self.query_one("#input", Input).focus()
        except Exception:
            pass

    def action_cycle_project(self) -> None:
        """Ctrl+O: cycle the project (config list, then all)."""
        if not self._on_main():
            return
        try:
            projects, _ = config.load_projects()
            names = [p.name for p in projects]
        except Exception:
            names = []
        order = names + ["all"]
        if not order:
            return
        cur = "all" if self.scope.all else self.scope.name
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        self.scope = scope.Scope() if nxt == "all" else scope.Scope((nxt,))
        self._selected = 0
        self._blocks, self._order = {}, []
        self.refresh_data()

    def action_history_step(self, delta: int) -> None:
        """Up/Down in the input: walk the input history, past the end it clears the line."""
        if not self._history:
            return
        self._hist_at = max(0, min(len(self._history), self._hist_at + delta))
        try:
            inp = self.query_one("#input", Input)
        except Exception:
            return
        inp.value = self._history[self._hist_at] if self._hist_at < len(self._history) else ""

    def action_quit_twice(self) -> None:
        now = time.monotonic()
        if now - self._quit_at < QUIT_GAP_S:
            self.exit()
        else:
            self._quit_at = now
            self._say([_t("console.quit_confirm")])

    def _say(self, lines: list[str]) -> None:
        self._transcript.extend(lines)
        if len(self._transcript) > TRANSCRIPT_CAP:
            self._transcript = self._transcript[-TRANSCRIPT_CAP:]
        self._paint_transcript()

    # --- commands ---

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        text = ev.value
        try:
            ev.input.value = ""
        except Exception:
            pass
        if text.strip():
            self._history.append(text)
            self._hist_at = len(self._history)
        self.run_command(text)

    def run_command(self, text: str) -> None:
        cmd, args = parse_command(text)
        if cmd == "":
            return
        if cmd == "draft":
            self._say([ui.styled(_t("console.draft_hint"), "dim")])
            return
        if cmd in ("quit", "exit", "q"):
            self._say([_t("console.quit")])
            self.exit()
            return
        if cmd == "help":
            self._show_shortcuts = True
            self._apply_shortcuts()
            self._say([_t("console.help")])
            return
        if cmd == "follow":
            self.cmd_follow(args)
            return
        if cmd == "status":
            self.cmd_status(args)
            return
        if cmd == "history":
            self.cmd_history(args)
            return
        if cmd in ("project", "all"):
            self.cmd_project(cmd, args)
            return
        self._say([_t("console.unknown", cmd="/" + cmd)])

    def action_toggle_shortcuts(self) -> None:
        """Toggle the shortcuts pane ("?" on an empty input)."""
        self._show_shortcuts = not self._show_shortcuts
        self._apply_shortcuts()

    def _apply_shortcuts(self) -> None:
        try:
            if self._show_shortcuts:
                self.query_one("#shortcuts", Static).update(_plain(_t("console.shortcuts")))
                self.query_one("#shortcuts").display = True
            else:
                self.query_one("#shortcuts").display = False
        except Exception:
            pass

    def _resolve_task(self, ref: str) -> Task | None:
        try:
            tid = parse_task_id(ref)
        except ValueError:
            return None
        try:
            t = self.store.get_task(tid)
        except Exception:
            return None
        if t is None:
            return None
        if self.scope is not None and scope.foreign(self.scope, t.project):
            return None
        return t

    def cmd_follow(self, args: list[str]) -> None:
        if not args:
            self._say([_t("console.follow_usage")])
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say([_t("console.no_task", ref=args[0])])
            return
        try:
            from ahub.tui.app import Transcript

            self.push_screen(Transcript(self.store, t.id))
        except Exception as e:  # a transcript crash returns to the console
            self._say([_t("console.widget_error", widget="follow", hint=str(e)[:100])])

    @work(thread=True)
    def cmd_status(self, args: list[str]) -> None:
        try:
            w = self._width()
            if args:
                t = self._resolve_task(args[0])
                if t is None:
                    self.call_from_thread(self._say, [_t("console.no_task", ref=args[0])])
                    return
                text = views.task_text(self.store, t, live={}, w=w)
            else:
                text = views.status_text(self.store, scope=self.scope, w=w)
        except Exception as e:
            text = _t("console.widget_error", widget="status", hint=str(e)[:100])
        self.call_from_thread(self._say, text.splitlines() or [""])

    @work(thread=True)
    def cmd_history(self, args: list[str]) -> None:
        del args
        try:
            w = self._width()
            text = views.history_text(self.store, scope=self.scope, w=w)
        except Exception as e:
            text = _t("console.widget_error", widget="history", hint=str(e)[:100])
        self.call_from_thread(self._say, text.splitlines() or [""])

    def cmd_project(self, cmd: str, args: list[str]) -> None:
        """Switch the scope: /project NAME|all, /all. Unknown names keep the scope."""
        if cmd == "all" or (args and args[0] == "all"):
            self.scope = scope.Scope()
        elif not args:
            self._say([_t("console.project_usage")])
            return
        else:
            from ahub.cliutil import CliError

            try:
                name = scope.checked(args[0])
            except CliError as e:
                self._say([str(e)])
                return
            self.scope = scope.Scope((name,))
        self._selected = 0
        self._blocks, self._order = {}, []
        self.refresh_data()

    # --- keys ---

    BINDINGS = [
        Binding("tab", "focus_tasks", "tasks", priority=True),
        Binding("escape", "focus_input", "input", priority=True),
        Binding("ctrl+o", "cycle_project", "project"),
        Binding("ctrl+c", "quit_twice", "quit"),
    ]

    def _on_main(self) -> bool:
        """False while another screen is on top (Transcript, Prompt, modals): its keys win."""
        try:
            return len(self.screen_stack) <= 1
        except Exception:
            return True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("focus_tasks", "cycle_project") and not self._on_main():
            return False
        if action == "focus_input" and not self._on_main():
            return False
        return True

    def on_key(self, ev) -> None:  # noqa: N802 - textual hook
        """Tasks-pane keys only (Tab/Esc travel through the priority BINDINGS above)."""
        if not self._on_main():
            return
        if self.focus_mode == "tasks":
            key = getattr(ev, "key", "")
            if key == "up":
                self._selected = max(0, self._selected - 1)
                self._render_tasks()
                ev.prevent_default()
            elif key == "down":
                self._selected = min(max(0, len(self._task_ids) - 1), self._selected + 1)
                self._render_tasks()
                ev.prevent_default()
            elif key == "enter":
                if self._task_ids:
                    idx = min(self._selected, len(self._task_ids) - 1)
                    self.cmd_follow([f"T{self._task_ids[idx]}"])
                ev.prevent_default()


def main(all_projects: bool = False, project: str | None = None, control: bool = False) -> int:
    ConsoleApp(all_projects=all_projects, project=project, control=control).run()
    return 0
