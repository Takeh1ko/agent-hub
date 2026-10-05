"""Console K4: transparent background, stage glyph+colour+word, ordering, hardening."""

from __future__ import annotations

import re

import pytest

from ahub import scope, ui
from ahub.i18n import _reset
from ahub.model import State
from ahub.store import Store
from ahub.time import now_ms
from ahub.tui import console as con
from ahub.tui.console import ConsoleApp
from tests.conftest import wait_for


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    yield
    _reset()


@pytest.fixture
def store() -> Store:
    return Store()


def _task(store: Store, project: str = "P", title: str = "t"):
    return store.create_task(project=project, kind="scout", title=title)


def _colour(monkeypatch):
    monkeypatch.setattr(ui, "colour_on", lambda: True)


def test_transparent_background_ansi_and_no_blue():
    app = ConsoleApp(store=Store(), all_projects=True)
    assert app.ansi_color is True
    css = ConsoleApp.CSS
    assert "background: ansi_default" in css
    assert "scrollbar-color: #ff8700" in css
    assert "scrollbar-background: ansi_default" in css
    assert "$primary" not in css
    assert "$surface" not in css
    assert "$boost" not in css
    assert "#0178D4" not in css


def test_no_widget_paints_a_background(store: Store):
    """Every widget background is transparent: Screen and all panes are ansi_default."""
    css = ConsoleApp.CSS
    for pane in ("Screen", "#welcome", "#tasks", "#transcript", "#input", "#footer",
                 "Static", "VerticalScroll", "Input"):
        assert pane in css
    # no non-default background left (theme surfaces, boosts, primary blues)
    for bad in ("$surface", "$boost", "$primary", "$background", "#0178D4"):
        assert bad not in css


def test_stage_words_colours_glyphs(store: Store, monkeypatch):
    from ahub import transitions
    from ahub.pulse import Pulse

    _colour(monkeypatch)
    now = now_ms()

    active = _task(store, "P", "code it")
    transitions.move(store, active, State.PREPARING)
    transitions.move(store, active, State.WORKING)
    store.update_task(active, phase="writing", now=now)
    t = store.get_task(active)
    lines = con.task_lines(t, None, now, 80, frame=0)
    assert "⏺" in lines[0] and ui.SPINNER[0] in lines[0]
    assert "\x1b[38;5;208m⏺\x1b[0m" not in lines[0]  # ⏺ never accent
    assert "writing code" in lines[1]
    assert "bash" not in "\n".join(lines)

    # raw tool never leaks: pulse carries bash, the line still says the stage
    pl = Pulse(active, "working", reason="", active_tool="bash")
    lines = con.task_lines(t, pl, now, 80, frame=1)
    assert "bash" not in "\n".join(lines)
    assert "writing code" in "\n".join(lines)

    studying = _task(store, "P", "read it")
    transitions.move(store, studying, State.PREPARING)
    transitions.move(store, studying, State.WORKING)
    store.update_task(studying, phase="studying", now=now)
    assert "studying" in "\n".join(con.task_lines(store.get_task(studying), None, now, 80))

    testing = _task(store, "P", "test it")
    transitions.move(store, testing, State.PREPARING)
    transitions.move(store, testing, State.WORKING)
    store.update_task(testing, phase="testing", now=now)
    assert "running tests" in "\n".join(con.task_lines(store.get_task(testing), None, now, 80))

    reviewing = _task(store, "P", "review it")
    transitions.move(store, reviewing, State.PREPARING)
    transitions.move(store, reviewing, State.WORKING)
    transitions.move(store, reviewing, State.REVIEWING)
    assert "in review" in "\n".join(con.task_lines(store.get_task(reviewing), None, now, 80))

    queued = _task(store, "P", "later")
    qlines = con.task_lines(store.get_task(queued), None, now, 80)
    assert "\x1b[2m◦\x1b[0m" in qlines[0]
    assert "queued" in qlines[1]

    done = _task(store, "P", "waiting")
    transitions.move(store, done, State.PREPARING)
    transitions.move(store, done, State.WORKING)
    transitions.move(store, done, State.DONE)
    wlines = con.task_lines(store.get_task(done), None, now, 80)
    assert "\x1b[33m⏺\x1b[0m" in wlines[0]
    assert "waiting for you" in "\n".join(wlines)

    err = _task(store, "P", "boom")
    transitions.move(store, err, State.PREPARING)
    transitions.move(store, err, State.WORKING)
    transitions.move(store, err, State.ERROR)
    elines = con.task_lines(store.get_task(err), None, now, 80)
    assert "\x1b[31m✗\x1b[0m" in elines[0]
    assert "error" in "\n".join(elines)

    stopped = _task(store, "P", "halt")
    transitions.move(store, stopped, State.STOPPED)
    slines = con.task_lines(store.get_task(stopped), None, now, 80)
    assert "\x1b[2m⏸\x1b[0m" in slines[0]
    assert "stopped" in "\n".join(slines)

    # dead process: active task with no live process reads as red ✗
    dead = _task(store, "P", "ghost")
    transitions.move(store, dead, State.PREPARING)
    transitions.move(store, dead, State.WORKING)
    dlines = con.task_lines(store.get_task(dead), Pulse(dead, "dead", "no task process"), now, 80)
    assert "\x1b[31m✗\x1b[0m" in dlines[0]
    assert "dead process" in "\n".join(dlines)


def test_snapshot_orders_active_waiting_queued(store: Store):
    from ahub import transitions

    q = _task(store, "P", "queued last")
    w = _task(store, "P", "waiting middle")
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, w, st)
    transitions.move(store, w, State.DONE)
    a = _task(store, "P", "active first")
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, a, st)
    snap = con.snapshot(store, scope.Scope(("P",)), 80, now_ms())
    assert snap.task_ids == [a, w, q]


def test_alert_keeps_latest_at_40_cols(store: Store):
    from ahub import comms

    comms.raise_alarm(store, "opencode down, a very long alarm text " + "word " * 20, critical=True)
    line = con.alert_text(store, scope.Scope(("P",)), 40)
    assert ui.plain_len(line) <= 38
    assert "—" in line
    assert "latest:" in line  # compact keeps the localized "— latest: …" label, not just the dash
    assert "opencode" in line or "…" in line


async def test_stale_marker_never_clipped(store: Store):
    from ahub.i18n import t

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._frame >= 1)
        app._last_w = 40
        app._last_footer = "x" * 60
        stale = t("console.stale", n=7)
        avail = 38
        # capture what _apply_stale delivers to the footer widget
        delivered: list[str] = []
        orig = app._safe_update

        def _capture(selector: str, widget_name: str, get_text, display: bool = True) -> None:
            try:
                delivered.append(get_text())
            except Exception as e:
                delivered.append(t("console.widget_error", widget=widget_name, hint=str(e)[:100]))
            return orig(selector, widget_name, get_text, display=display)

        app._safe_update = _capture  # type: ignore[method-assign]
        app._apply_stale(7)
        await wait_for(pilot, lambda: len(delivered) > 0)
        assert delivered, "stale footer was never delivered to the widget"
        text = delivered[-1]
        # the stale note survives verbatim: its room was reserved, the old footer was clipped
        assert stale in text
        assert ui.plain_len(text) <= avail


async def test_safe_update_class_patch_keeps_app_alive(store: Store, monkeypatch):
    from textual.widgets import Static

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._frame >= 1)
        calls: list[int] = []
        orig = Static.update

        def _flaky(self, *a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("boom")
            return orig(self, *a, **k)

        monkeypatch.setattr(Static, "update", _flaky)
        # first update raises, the single fallback delivers the error text instead
        app._safe_update("#welcome", "welcome", lambda: "hi")
        await wait_for(pilot, lambda: len(calls) == 2)
        assert len(calls) == 2
        assert app.is_running
        rendered = str(app.query_one("#welcome", Static).render())
        assert "✗ welcome:" in rendered


async def test_on_mount_reads_nothing_on_ui_thread(store: Store, monkeypatch):
    import threading

    from ahub.store import Store as _Store

    ui_thread = threading.current_thread().name
    calls: list[str] = []
    orig = _Store.last_event_id

    def _recording(self):
        calls.append(threading.current_thread().name)
        return orig(self)

    monkeypatch.setattr(_Store, "last_event_id", _recording)
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: bool(calls) and app._need_init is False)
        # the cursor is recorded off the UI thread by the first refresh, never on mount
        assert calls, "expected the worker to record the event cursor"
        assert all(name != ui_thread for name in calls)
        assert app._need_init is False
        assert app.is_running


def test_resolve_task_uses_cache_not_store(store: Store, monkeypatch):
    from ahub import transitions

    tid = _task(store, "P", "cached")
    transitions.move(store, tid, State.PREPARING)
    app = ConsoleApp(store=store, all_projects=True)
    snap = con.snapshot(store, app.scope, 80, now_ms())
    app._tasks = dict(snap.tasks)
    app._task_ids = list(snap.task_ids)

    def _boom(_tid):
        raise AssertionError("store read on the UI thread")

    monkeypatch.setattr(store, "get_task", _boom)
    t = app._resolve_task(f"T{tid}")
    assert t is not None and t.id == tid


async def test_refresh_does_not_steal_real_focus(store: Store):
    from textual.widgets import Input

    _task(store, "P", "keep me")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await wait_for(pilot, lambda: app._frame >= 1)
        inp = app.query_one("#input", Input)
        inp.value = "hello /status"
        inp.focus()
        await pilot.pause(0.1)
        assert app.focused is inp
        snap = con.snapshot(store, app.scope, 80, now_ms())
        app._apply(snap, [])
        await wait_for(pilot, lambda: app.query_one("#input", Input).value == "hello /status")
        assert app.query_one("#input", Input).value == "hello /status"
        assert app.focused is app.query_one("#input", Input)


def test_json_gate_on_tty(monkeypatch, tmp_path, capsys):
    from ahub import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    called = {}

    def _fake(*, all_projects=False, project=None, control=False):
        called["console"] = True
        return 0

    monkeypatch.setattr("ahub.tui.console.main", _fake)
    assert cli.main(["--json"]) == 0
    assert "console" not in called  # --json on a TTY stays one-shot, no console


def test_home_and_status_palette_at_80_cols(tmp_path, monkeypatch):
    from ahub import home, paths, transitions, views
    from ahub.model import Kind

    monkeypatch.setattr(ui, "colour_on", lambda: True)
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    (demo_dir / ".hub.toml").write_text('schema_version = 2\nname = "demo"\n', encoding="utf-8")
    monkeypatch.chdir(demo_dir)
    monkeypatch.setattr(paths, "global_config_path", lambda: tmp_path / "global.toml")
    (tmp_path / "global.toml").write_text("projects = []\n", encoding="utf-8")
    store = Store()
    a = store.create_task(project="demo", kind=Kind.CODE, title="active work", executor="spark")
    transitions.move(store, a, State.PREPARING)
    transitions.move(store, a, State.WORKING)
    w = store.create_task(project="demo", kind=Kind.SCOUT, title="wait for me")
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, w, st)
    transitions.move(store, w, State.DONE)
    text = home.text(w=80)
    assert "\x1b[38;5;208m⏺\x1b[0m" not in text  # ⏺ never accent
    assert "⏺" in text and "\x1b[33m⏺\x1b[0m" in text  # waiting is yellow
    assert "\x1b[38;5;208m✻ ahub\x1b[0m" in text  # accent only for ✻/borders/product
    st = views.status_text(store, w=80)
    assert "\x1b[38;5;208m⏺\x1b[0m" not in st
    for ln in (text + "\n" + st).splitlines():
        assert ui.plain_len(ln) <= 80


def test_pipe_output_has_no_ansi_and_no_console_marks(tmp_path, monkeypatch, capsys):
    from ahub import cli, home

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out == home.text() + "\n"
    assert "\x1b[" not in out
    assert "⏺" not in out and "◦" not in out  # pipe stays compact, no console marks


def test_welcome_box_reads_only_what_it_shows(store: Store, monkeypatch):
    from ahub.tui import data as topdata

    def _boom(*a, **k):
        raise AssertionError("welcome_box must not read the whole header")

    monkeypatch.setattr(topdata, "header", _boom)
    box = con.welcome_box(store, scope.Scope(("P",)), now_ms(), 80)
    assert "ahub" in box


def test_liveview_lines_capped(store: Store, tmp_path):
    from ahub.providers.fake import FakeProvider
    from ahub.tui.live import MAX_LINES, Feed, LiveView

    log = tmp_path / "x.log"
    log.write_text("", encoding="utf-8")
    feed = Feed(FakeProvider(), log)
    feed.lines = ["l%d" % i for i in range(MAX_LINES + 10)]
    feed.update()
    assert len(feed.lines) <= MAX_LINES
    tid = _task(store, "P", "live")
    view = LiveView(store, tid)
    view.feed = feed
    assert len(view.lines) <= MAX_LINES


def _bg_sgr_params(sgr_text: str) -> list[str]:
    """Background params of every SGR sequence: 40-47, 100-107, 48;5;n, 48;2;r;g;b."""
    found: list[str] = []
    for m in re.finditer(r"\x1b\[([0-9;]*)m", sgr_text):
        params = m.group(1).split(";")
        i = 0
        while i < len(params):
            p = params[i]
            if p.isdigit() and (40 <= int(p) <= 47 or 100 <= int(p) <= 107):
                found.append(p)
            elif p == "48" and i + 1 < len(params) and params[i + 1] in ("5", "2"):
                found.append("48;" + params[i + 1])
                i += 1
            i += 1
    return found


async def test_input_emits_no_background_sgr(store: Store):
    """The input line paints no background: placeholder dim on default, cursor underlined, no tint.

    Mirrors the real-terminal gate (pty_bg_check.py): no 40-47/100-107/48;5/48;2 SGR
    anywhere in the rows the Input emits under ansi_color.
    """
    from io import StringIO

    from rich.console import Console as RichConsole
    from textual.widgets import Input

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app._frame >= 1 and app.native_ansi_color)
        assert app.native_ansi_color
        inp = app.query_one("#input", Input)
        inp.focus()
        await wait_for(pilot, lambda: app.focused is inp)
        assert app.focused is inp
        assert inp.styles.background_tint.a == 0
        for comp in ("input--placeholder", "input--suggestion", "input--cursor"):
            bg = inp.get_component_rich_style(comp).bgcolor
            assert bg is None or "default" in str(bg), comp
        buf = StringIO()
        rc = RichConsole(file=buf, force_terminal=True, color_system="truecolor", width=120)
        for y in range(inp.size.height):
            for seg in inp.render_line(y):
                rc.print(seg, end="")
        assert _bg_sgr_params(buf.getvalue()) == []


def test_footer_names_hub_sessions_with_separator(store: Store):
    from ahub import transitions

    tid = _task(store, "P", "footer task")
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)
    wide = con.footer_text(store, scope.Scope(("P",)), 120)
    assert "1 working · P · hub sessions this month: Go $" in wide
    assert "USD $" in wide
    # at 80 cols with a long scope the scope segment goes first — count and money stay
    narrow = con.footer_text(store, scope.Scope(), 80)
    assert "1 working · hub sessions this month: Go $" in narrow
    assert "USD $" in narrow
    assert ui.plain_len(narrow) <= 78


def test_welcome_names_go_plan_machine(store: Store):
    box = con.welcome_box(store, scope.Scope(("P",)), now_ms(), 80)
    assert "Go plan (this machine): today $" in box
