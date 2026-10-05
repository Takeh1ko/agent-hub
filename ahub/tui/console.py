"""Interactive console: shell, commands, confirmations, the draft flow (K1+K2).

Layout top to bottom: welcome box, alert strip, task blocks pane (diffed refresh),
event transcript (capped), live status line, input box (always focused), footer.
Every command calls the same functions the CLI calls (accept.*, transitions.*,
drafts.*, comms.*, cost.*) — no copied logic. Mutating commands confirm through
the Confirm/Ask modals. Plain text is a draft.

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
from textual.suggester import Suggester
from textual.widgets import Input, Static

import ahub
from ahub import (
    accept,
    comms,
    config,
    cost,
    doctor,
    drafts,
    events,
    pulse,
    reasons,
    registry,
    scope,
    transitions,
    ui,
    views,
)
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
COMMANDS = ("accept", "reject", "rework", "stop", "nudge", "model", "budget", "follow", "status",
            "history", "inbox", "questions", "alarms", "models", "providers", "projects", "cost",
            "doctor", "project", "all", "draft", "start", "help", "quit")
TASK_COMMANDS = frozenset({"accept", "reject", "rework", "stop", "nudge", "model", "budget",
                           "follow", "status"})


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
        money = topdata.header(store, {}, now).split("\n")[-1]
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


def complete_input(value: str, task_labels: list[str]) -> str | None:
    """Command-name and task-id completion for the input (pure, testable).

    '/ac' → '/accept '; '/accept T1' → '/accept T12' (first label with that prefix).
    A numeric rest ('1') matches the digits of the label ('T12'). Plain text → None.
    """
    raw = value or ""
    stripped = raw.lstrip()
    if not stripped.startswith("/"):
        return None
    gap = raw[: len(raw) - len(stripped)]
    if " " not in stripped:
        want = stripped.lower()
        if want == "/":
            return None
        for cmd in COMMANDS:
            cand = "/" + cmd
            if cand.startswith(want) and cand != want:
                return gap + cand + " "
        # exact command without the trailing space — offer it
        for cmd in COMMANDS:
            if ("/" + cmd) == want:
                return gap + "/" + cmd + " "
        return None
    parts = stripped.split(None, 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    cmd = head[1:].lower()
    if cmd not in TASK_COMMANDS:
        return None
    tail = rest.lstrip()
    if not tail:
        return (gap + head + " " + task_labels[0]) if task_labels else None
    upper = tail.upper()
    for label in task_labels:
        if label.upper().startswith(upper):
            return gap + head + " " + label
    if tail.isdigit():
        for label in task_labels:
            if label[1:].startswith(tail):
                return gap + head + " " + label
    return None


class ConsoleSuggester(Suggester):
    """Completion for the console input: command names, then task ids of the scope."""

    def __init__(self, app: ConsoleApp) -> None:
        super().__init__(case_sensitive=False)
        self._app = app

    async def get_suggestion(self, value: str) -> str | None:
        try:
            labels = [f"T{tid}" for tid in (self._app._task_ids or [])]
        except Exception:
            labels = []
        return complete_input(value, labels)


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
        self._drafting = False  # a draft model run is in flight — the live line says so
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
        yield ConsoleInput(placeholder=_t("console.input_placeholder"), id="input",
                           suggester=ConsoleSuggester(self))
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

    def _drafting_line(self) -> str:
        """The live line while a draft model run is in flight: ✻ Drafting… ."""
        return f"{ui.styled('✻', 'accent')} {ui.styled(_t('console.drafting'), 'dim')}"

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
        if self._drafting:
            self._safe_update("#live", "live", lambda: self._drafting_line(), display=True)
        else:
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

    def _say_ok(self, msg: str, next_key: str = "", label: str = "") -> None:
        """A command result into the transcript: ⏺ statement + ⎿ Next."""
        try:
            nxt = _t("views.hint_next", cmd=_t(next_key, label=label)) if next_key else ""
        except Exception:
            nxt = ""
        try:
            text = ui.item(msg, [nxt] if nxt else [])
        except Exception:
            text = f"⏺ {msg}" + (f"\n  ⎿ {nxt}" if nxt else "")
        self._say(text.splitlines() or [""])

    def _say_err(self, msg: str, hint: str = "") -> None:
        """A refusal into the transcript: ✗ what + ⎿ way out."""
        try:
            text = ui.failed(msg, hint) if hint else ui.failed(msg)
        except Exception:
            text = f"✗ {msg}" + (f"\n  ⎿ {hint}" if hint else "")
        self._say(text.splitlines() or [""])

    def _service_down(self) -> bool:
        """True when the queue will not move: no fresh service heartbeat."""
        try:
            hb = self.store.meta_get(HEARTBEAT_KEY)
            return not (hb and now_ms() - int(hb) < 30_000)
        except Exception:  # a store failure is shown by the widgets; do not block the confirm
            return False

    def _confirm_text(self, base: str) -> str:
        """A confirmation plus the queue note when the service is down."""
        if self._service_down():
            return base + _t("console.confirm_stalled")
        return base

    def _do(self, fn, next_key: str = "", label: str = "") -> None:
        """Run a mutating function; the result (or the refusal) goes to the transcript."""
        try:
            msg = fn()
        except (OSError, ValueError, RuntimeError) as e:
            hint = getattr(e, "hint", "")
            self._say_err(str(e)[:300], hint)
            return
        self._say_ok(str(msg)[:500], next_key, label)
        self.refresh_data()

    def _confirm_then(self, text: str, fn, next_key: str = "", label: str = "") -> None:
        """Mutating commands confirm through the Confirm modal, never inline y/n."""
        from ahub.tui.app import Confirm

        def done(ok: bool | None) -> None:
            if ok:
                self._do(fn, next_key, label)
            else:
                self._say([ui.styled(_t("console.cancelled"), "dim")])

        try:
            self.push_screen(Confirm(text), done)
        except Exception as e:  # a modal failure returns to the console
            self._say([_t("console.widget_error", widget="confirm", hint=str(e)[:100])])

    def _ask_then(self, prompt: str, fn, next_key: str = "", label: str = "",
                  placeholder: str = "") -> None:
        """A missing text (rework notes, nudge text, model, budget) is asked through Ask."""
        from ahub.tui.app import Ask

        def done(val: str | None) -> None:
            if val:
                self._do(lambda: fn(val), next_key, label)
            else:
                self._say([ui.styled(_t("console.cancelled"), "dim")])

        try:
            self.push_screen(Ask(prompt, placeholder), done)
        except Exception as e:
            self._say([_t("console.widget_error", widget="ask", hint=str(e)[:100])])

    def _current_project(self):
        """The project of the console scope; None in /all mode or without a config."""
        if self.scope.all:
            return None
        try:
            projects, _ = config.load_projects()
        except Exception:
            return None
        return next((p for p in projects if p.name == self.scope.name), None)

    def _project_of_task(self, t: Task):
        """The project config of a task; None — the hub does not know it."""
        try:
            projects, _ = config.load_projects()
        except Exception:
            return None
        return next((p for p in projects if p.name == t.project), None)

    def _project_by_name(self, name: str):
        try:
            projects, _ = config.load_projects()
        except Exception:
            return None
        return next((p for p in projects if p.name == name), None)

    def _selected_task_id(self) -> int | None:
        if not self._task_ids:
            return None
        idx = min(max(0, self._selected), len(self._task_ids) - 1)
        return self._task_ids[idx]

    def run_command(self, text: str) -> None:
        cmd, args = parse_command(text)
        if cmd == "":
            return
        if cmd == "draft":
            self.cmd_draft(args)
            return
        if cmd in ("quit", "exit", "q"):
            self._say([_t("console.quit")])
            self.exit()
            return
        if cmd == "help":
            self._show_shortcuts = True
            self._apply_shortcuts()
            self._say(_t("console.help").splitlines() or [""])
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
        if cmd == "accept":
            self.cmd_accept(args)
            return
        if cmd == "reject":
            self.cmd_reject(args)
            return
        if cmd == "rework":
            self.cmd_rework(args)
            return
        if cmd == "stop":
            self.cmd_stop(args)
            return
        if cmd == "nudge":
            self.cmd_nudge(args)
            return
        if cmd == "model":
            self.cmd_model(args)
            return
        if cmd == "budget":
            self.cmd_budget(args)
            return
        if cmd == "inbox":
            self.cmd_inbox(args)
            return
        if cmd == "questions":
            self.cmd_questions(args)
            return
        if cmd == "alarms":
            self.cmd_alarms(args)
            return
        if cmd == "models":
            self.cmd_models(args)
            return
        if cmd == "providers":
            self.cmd_providers(args)
            return
        if cmd == "projects":
            self.cmd_projects(args)
            return
        if cmd == "cost":
            self.cmd_cost(args)
            return
        if cmd == "doctor":
            self.cmd_doctor(args)
            return
        if cmd == "start":
            self.cmd_start(args)
            return
        self._say_err(_t("console.unknown", cmd="/" + cmd))

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
            self._say_err(_t("console.follow_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
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
                    self.call_from_thread(self._say_err, _t("console.no_task", ref=args[0]))
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
        if cmd == "all" or (args and args[0] == "all"):
            self.scope = scope.Scope()
        elif args:
            self.scope = scope.Scope((scope.name_of(args[0]),))
        else:
            self._say_err(_t("console.project_usage"))
            return
        self._blocks, self._order = {}, []
        self.refresh_data()

    # --- mutating commands (Confirm/Ask, same functions as the CLI) ---

    def cmd_accept(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.accept_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        p = self._project_of_task(t)
        if p is None:
            self._say_err(_t("tui.no_project", name=t.project))
            return
        tid = t.id
        self._confirm_then(self._confirm_text(_t("tui.confirm_accept", tid=tid)),
                           lambda: accept.accept(self.store, p, tid, by="human"),
                           "views.next_accept", t.label)

    def cmd_reject(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.reject_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        p = self._project_of_task(t)
        if p is None:
            self._say_err(_t("tui.no_project", name=t.project))
            return
        keep = "--keep" in args[1:]
        reason = " ".join(a for a in args[1:] if a != "--keep").strip()
        tid = t.id
        self._confirm_then(self._confirm_text(_t("tui.confirm_reject", tid=tid)),
                           lambda: accept.reject(self.store, p, tid, reason=reason,
                                                 by="human", keep=keep),
                           "views.next_reject", t.label)

    def cmd_rework(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.rework_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        notes = " ".join(args[1:]).strip()
        if not notes:
            self._ask_then(_t("tui.ask_rework", tid=t.id),
                           lambda v: accept.rework(self.store, t.id, v, by="human"),
                           "views.next_rework", t.label)
            return
        tid = t.id
        self._confirm_then(self._confirm_text(f"{_t('tui.ask_rework', tid=tid)} {notes}"),
                           lambda: accept.rework(self.store, tid, notes, by="human"),
                           "views.next_rework", t.label)

    def cmd_stop(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.stop_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        tid, label = t.id, t.label

        def _fn() -> str:
            how = transitions.request_stop(self.store, tid, by="human")
            return _t("task.stopped", label=label) if how == "stopped" else _t(
                "task.stop_requested", label=label)

        self._confirm_then(self._confirm_text(_t("tui.confirm_stop", tid=tid)),
                           _fn, "views.next_task", label)

    def cmd_nudge(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.nudge_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        text = " ".join(args[1:]).strip()
        if not text:
            self._ask_then(_t("tui.ask_nudge", tid=t.id),
                           lambda v: self._nudge(t.id, t.label, v))
            return
        tid, label = t.id, t.label
        self._confirm_then(self._confirm_text(f"{_t('tui.ask_nudge', tid=tid)} {text}"),
                           lambda: self._nudge(tid, label, text))

    def _nudge(self, tid: int, label: str, text: str) -> str:
        transitions.request_nudge(self.store, tid, text=text, by="human")
        return _t("task.nudge_requested", label=label)

    def cmd_model(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.model_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        alias = args[1].strip() if len(args) > 1 else ""
        if not alias:
            self._ask_then(_t("tui.ask_model", tid=t.id),
                           lambda v: self._change_model(t, v),
                           "views.next_task", t.label, placeholder="spark / mimo-flash")
            return
        self._confirm_then(self._confirm_text(f"{_t('tui.ask_model', tid=t.id)} {alias}"),
                           lambda: self._change_model(t, alias),
                           "views.next_task", t.label)

    def _change_model(self, t: Task, alias: str) -> str:
        p = self._project_of_task(t)
        if p is None:
            raise accept.DecisionError(_t("tui.no_project", name=t.project))
        return accept.change_model(self.store, p, t.id, alias.strip(), by="human")

    def cmd_budget(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.budget_usage"))
            return
        t = self._resolve_task(args[0])
        if t is None:
            self._say_err(_t("console.no_task", ref=args[0]))
            return
        raw = args[1].strip() if len(args) > 1 else ""
        if not raw:
            self._ask_then(_t("tui.ask_budget", tid=t.id),
                           lambda v: self._extend_budget(t.id, v),
                           "views.next_task", t.label, placeholder="0.5")
            return
        self._confirm_then(self._confirm_text(f"{_t('tui.ask_budget', tid=t.id)} {raw}"),
                           lambda: self._extend_budget(t.id, raw),
                           "views.next_task", t.label)

    def _extend_budget(self, tid: int, raw: str) -> str:
        try:
            add = float(raw.lstrip("+").replace(",", "."))
        except ValueError as e:
            raise ValueError(_t("console.budget_usage")) from e
        return accept.extend_budget(self.store, tid, add=add, by="human")

    # --- read commands (same functions as the CLI, console scope) ---

    def _row_num(self, ref: str) -> int | None:
        try:
            return int(ref.lstrip("#"))
        except ValueError:
            self._say_err(_t("err.bad_ref", ref=ref))
            return None

    def cmd_inbox(self, args: list[str]) -> None:
        w = self._width()
        if args:
            num = self._row_num(args[0])
            if num is None:
                return
            try:
                row = comms.message(self.store, num)
            except (OSError, ValueError, RuntimeError) as e:
                self._say_err(str(e)[:200])
                return
            if row is None or scope.foreign(self.scope, str(row.get("project") or "")):
                self._say_err(_t("err.no_message", ref=args[0]))
                return
            try:
                self._say(views.message_text(row, w=w).splitlines() or [""])
            except (OSError, ValueError, RuntimeError) as e:
                self._say([_t("console.widget_error", widget="inbox", hint=str(e)[:100])])
            return
        try:
            rows = comms.inbox(self.store, scope=self.scope)
            self._say(views.inbox_text(rows, w=w).splitlines() or [""])
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="inbox", hint=str(e)[:100])])

    def cmd_questions(self, args: list[str]) -> None:
        w = self._width()
        if args:
            num = self._row_num(args[0])
            if num is None:
                return
            try:
                row = comms.question(self.store, num)
            except (OSError, ValueError, RuntimeError) as e:
                self._say_err(str(e)[:200])
                return
            if row is None or scope.foreign(self.scope, str(row.get("project") or "")):
                self._say_err(_t("err.no_question", ref=args[0]))
                return
            try:
                self._say(views.question_text(row, w=w).splitlines() or [""])
            except (OSError, ValueError, RuntimeError) as e:
                self._say([_t("console.widget_error", widget="questions", hint=str(e)[:100])])
            return
        try:
            rows = comms.open_questions(self.store, scope=self.scope)
            self._say(views.questions_text(rows, w=w).splitlines() or [""])
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="questions", hint=str(e)[:100])])

    def cmd_alarms(self, args: list[str]) -> None:
        try:
            w = self._width()
            unacked_only = "--acked" not in args
            do_ack = "--ack" in args
            al = comms.alarms(self.store, unacked_only=unacked_only, scope=self.scope)
            if not al:
                self._say([_t("comms.alarms_empty")])
                return
            now = now_ms()
            head = [_t("alarms.col_id"), _t("alarms.col_age"), _t("alarms.col_what")]
            try:
                rendered = events.lines(self.store, al)
            except (OSError, ValueError, RuntimeError):
                rendered = ["" for _ in al]
            body = [[f"#{e.id}", views.age(e.ts, now), line]
                    for e, line in zip(al, rendered, strict=False)]
            out = ui.table(head, body, max_width=[6, 8, None], indent=2, w=w)
            lines = out.splitlines()
            if do_ack:
                try:
                    events.ack(self.store, [e.id for e in al], scope=self.scope)
                except (OSError, ValueError, RuntimeError) as e:
                    self._say_err(str(e)[:200])
                    return
            self._say(lines or [""])
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="alarms", hint=str(e)[:100])])

    def cmd_models(self, args: list[str]) -> None:
        del args
        try:
            from ahub.model import Role as _Role

            w = self._width()
            project = self._current_project()
            head = [_t("models.col_role"), _t("models.col_default"), _t("models.col_other")]
            rows = []
            for role in _Role:
                try:
                    items = registry.menu(self.store, role)
                except (OSError, ValueError, RuntimeError):
                    continue
                default = next((e for e, d in items if d), None)
                others = [e.alias + self._model_tags(e, project) for e, d in items if not d]
                dflt = ((default.alias + self._model_tags(default, project))
                        if default is not None else _t("models.no_default"))
                rows.append([role.value, dflt, ", ".join(others) or _t("models.no_other")])
            self._say(ui.table(head, rows, max_width=[10, 18, None], indent=2, w=w).splitlines())
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="models", hint=str(e)[:100])])

    def _model_tags(self, entry, project) -> str:
        try:
            out = ""
            if not entry.enabled:
                out += _t("models.tag_off")
            if project is not None and registry.denied_by(entry, project):
                out += _t("models.tag_denied")
            return out
        except (OSError, ValueError, RuntimeError):
            return ""

    def cmd_providers(self, args: list[str]) -> None:
        del args
        try:
            w = self._width()
            off = registry.disabled_providers()
            head = [_t(f"providers.col_{c}") for c in ("name", "found", "login", "enabled")]
            marks = {True: "✓", False: "✗", None: "–"}
            rows = []
            for st in doctor.provider_states():
                en = st.name not in off
                rows.append([st.name, marks[st.found],
                             marks[st.logged_in] if st.found else marks[None],
                             _t("providers.enabled_on") if en else _t("providers.enabled_off")])
            self._say(ui.table(head, rows, max_width=None, indent=2, w=w).splitlines() or [""])
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="providers", hint=str(e)[:100])])

    def cmd_projects(self, args: list[str]) -> None:
        del args
        try:
            from ahub.commands import projects as _proj

            w = self._width()
            hub = config.load_hub()
            plist, errors = config.load_projects(hub)
            known = {p.name: p for p in plist}
            month0 = cost.month_start()
            every = _proj.stats(self.store, month0)
            problems = {p.name: config.check_project(p) for p in plist}
            for name in self.store.task_projects():
                problems.setdefault(name, [_t("projects.not_in_hub", project=name)])
            rows = [(name, _proj._row_cells(name, known[name].root if name in known else "—",
                                            every.get(name, _proj.Stat()),
                                            "!" if problems[name] else " "))
                    for name in problems]
            if not rows:
                self._say([_t("projects.empty", source=hub.source or "—")])
                return
            keys = _proj._keys([cells for _name, cells in rows], w - 2)
            caps = {k: c for k, c, _f in _proj.COLUMNS}
            text = ui.table([_t(f"projects.col_{k}") for k in keys],
                            [[cells[k] for k in keys] for _name, cells in rows],
                            max_width=[caps[k] for k in keys], indent=2, w=w)
            lines = text.splitlines()
            for name, _cells in rows:
                lines.extend(f"    {e}" for e in problems[name])
            lines.extend(f"! {e}" for e in errors)
            self._say(lines or [""])
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="projects", hint=str(e)[:100])])

    def cmd_cost(self, args: list[str]) -> None:
        del args
        try:
            w = self._width()
            since = cost.month_start()
            models = cost.by_model(self.store, scope=self.scope, since=since)
            total = cost.total(self.store, scope=self.scope, since=since)
            sessions = sum(m.sessions for m in models)
            head = [_t("cost.col_project"), _t("cost.col_model"), _t("cost.col_sessions"),
                    _t("cost.col_go"), _t("cost.col_usd")]
            body = [[m.project or _t("cost.hub"), m.model or "—", str(m.sessions),
                     f"{m.go:.3f}", f"{m.usd:.3f}"] for m in models]
            lines = []
            if body:
                lines.append(ui.table(head, body, max_width=[16, None, None, 9, 9], indent=2, w=w))
            else:
                lines.append(_t("cost.empty"))
            lines.append(ui.styled(_t("cost.totals", go=f"{total.go:.3f}",
                                       usd=f"{total.usd:.3f}", n=sessions), "dim"))
            self._say("\n".join(lines).splitlines() or [""])
        except (OSError, ValueError, RuntimeError) as e:
            self._say([_t("console.widget_error", widget="cost", hint=str(e)[:100])])

    @work(thread=True)
    def cmd_doctor(self, args: list[str]) -> None:
        del args
        try:
            from ahub.commands import doctor as _doc

            checks = doctor.run_all()
            w = self._width()
            text = _doc.text(checks, w)
        except (OSError, ValueError, RuntimeError) as e:
            text = _t("console.widget_error", widget="doctor", hint=str(e)[:100])
        self.call_from_thread(self._say, text.splitlines() or [""])

    # --- drafts: plain text is a draft ---

    def cmd_draft(self, args: list[str]) -> None:
        text = " ".join(args).strip()
        if not text:
            self._say_err(_t("console.draft_usage"))
            return
        self._start_draft(text)

    def _start_draft(self, text: str) -> None:
        project = self._current_project()
        if project is None:
            self._say_err(_t("console.need_project"))
            return
        self._drafting = True
        line = self._drafting_line()
        self._say([line])
        try:
            self.query_one("#live", Static).update(_plain(line))
            self.query_one("#live").display = True
        except Exception:
            pass
        self._run_draft(project, text)

    @work(thread=True)
    def _run_draft(self, project, text: str) -> None:
        try:
            did = drafts.create(self.store, project, text, source="top")
            preview = drafts.preview(self.store, did)
            status = drafts.status(self.store, did)
        except (OSError, ValueError, RuntimeError) as e:
            self.call_from_thread(self._say_err, str(e)[:300])
            self.call_from_thread(self._end_drafting)
            return
        self.call_from_thread(self._offer_draft, project, did, preview, status)

    def _end_drafting(self) -> None:
        self._drafting = False
        self.refresh_data()

    def _offer_draft(self, project, did: int, preview: str, status: str) -> None:
        self._drafting = False
        # readiness by the status code of the row, never by the text of the preview
        # (the preview is localized and may quote the words themselves)
        if status != drafts.READY:
            self._say_err(preview[:300])
            self.refresh_data()
            return
        lines = preview.splitlines() or [preview]
        out = [f"{ui.status_mark('⏺', 'success')} {lines[0]}"]
        out += [f"  {ui.styled('⎿', 'dim')} {ui.styled(ln, 'dim')}" for ln in lines[1:]]
        out.append(f"  {ui.styled('⎿', 'dim')} {ui.styled(_t('console.start_hint', id=did), 'dim')}")
        self._say(out)
        self.refresh_data()

    def _draft_project(self, did: int):
        try:
            with self.store.read() as c:
                row = c.execute("SELECT project FROM draft WHERE id=?", (did,)).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        return self._project_by_name(str(row["project"])) or self._current_project()

    def cmd_start(self, args: list[str]) -> None:
        if not args:
            self._say_err(_t("console.start_usage"))
            return
        try:
            did = int(args[0].lstrip("#"))
        except ValueError:
            self._say_err(_t("console.start_usage"))
            return
        try:
            status = drafts.status(self.store, did)
        except (OSError, ValueError, RuntimeError) as e:
            self._say_err(str(e)[:200])
            return
        if not status:
            self._say_err(_t("draft.no_draft", id=did))
            return
        if status != drafts.READY:
            try:
                preview = drafts.preview(self.store, did)
            except (OSError, ValueError, RuntimeError) as e:
                preview = str(e)[:200]
            self._say_err(preview[:300])
            return
        project = self._draft_project(did)
        if project is None:
            self._say_err(_t("console.need_project"))
            return
        try:
            preview = drafts.preview(self.store, did)
        except (OSError, ValueError, RuntimeError) as e:
            self._say_err(str(e)[:200])
            return
        from ahub.i18n import t as _tt

        def _fn() -> str:
            tid = drafts.start(self.store, project, did)
            return _t("draft.queued", tid=tid)

        # the label is known only after the start: the Next line is added by _do_done
        def _done(ok: bool | None) -> None:
            if not ok:
                try:
                    drafts.cancel(self.store, did)
                except (OSError, ValueError, RuntimeError):
                    pass
                self._say([ui.styled(_t("console.cancelled"), "dim")])
                return
            try:
                msg = _fn()
            except (OSError, ValueError, RuntimeError) as e:
                self._say_err(str(e)[:300], getattr(e, "hint", ""))
                return
            try:
                tid = int(str(msg).split("T")[1].split()[0])
                label = f"T{tid}"
            except (IndexError, ValueError):
                label = ""
            self._say_ok(msg, "views.next_task" if label else "", label)
            self.refresh_data()

        from ahub.tui.app import Confirm

        try:
            self.push_screen(Confirm(self._confirm_text(preview + "\n\n" + _tt("tui.confirm_run"))),
                             _done)
        except Exception as e:
            self._say([_t("console.widget_error", widget="confirm", hint=str(e)[:100])])

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
        """Tasks-pane keys: Up/Down select, Enter follow, a/x/r/m act on the selected task."""
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
                tid = self._selected_task_id()
                if tid is not None:
                    self.cmd_follow([f"T{tid}"])
                ev.prevent_default()
            elif isinstance(key, str) and key.lower() in ("a", "x", "r", "m"):
                tid = self._selected_task_id()
                if tid is not None:
                    ref = f"T{tid}"
                    if key.lower() == "a":
                        self.cmd_accept([ref])
                    elif key.lower() == "x":
                        self.cmd_reject([ref])
                    elif key.lower() == "r":
                        self.cmd_rework([ref])
                    else:
                        self.cmd_nudge([ref])
                ev.prevent_default()


def main(all_projects: bool = False, project: str | None = None, control: bool = False) -> int:
    ConsoleApp(all_projects=all_projects, project=project, control=control).run()
    return 0
