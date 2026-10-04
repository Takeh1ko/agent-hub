"""Console K1 shell: layout, focus model, follow, clipping, bare-TTY gate (textual pilot)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import cli, comms, scope, ui
from ahub.model import State
from ahub.store import Store
from ahub.time import now_ms
from ahub.tui import console as con
from ahub.tui.console import ConsoleApp


@pytest.fixture
def store() -> Store:
    return Store()


def _task(store: Store, project: str, title: str):
    return store.create_task(project=project, kind="scout", title=title)


def test_welcome_box_has_version_project_and_service(store: Store):
    import ahub

    sc = scope.Scope(("P",))
    box = con.welcome_box(store, sc, now_ms(), 80)
    assert f"ahub {ahub.__version__}" in box
    assert "P" in box
    assert "service" in box.lower() or "сервис" in box.lower()


def test_scope_tasks_shown(store: Store):
    from ahub import transitions

    a = _task(store, "P", "my task here")
    b = _task(store, "Q", "other task here")
    transitions.move(store, a, State.PREPARING)
    transitions.move(store, b, State.PREPARING)
    now = now_ms()
    only = con.snapshot(store, scope.Scope(("P",)), 80, now)
    assert only.task_ids == [a]
    every = con.snapshot(store, scope.Scope(), 80, now)
    assert set(every.task_ids) == {a, b}


def test_clip_width_one_line_at_40_80_120():
    long_title = "word " * 30 + "tail"
    for w in (40, 80, 120):
        clipped = ui.clip_width(long_title, w)
        assert "\n" not in clipped
        assert ui.plain_len(clipped) <= w
        # word boundaries: no cut inside a word except the ellipsis
        assert not clipped.endswith(" ")


def test_task_lines_are_single_visual_lines(store: Store):
    from ahub import transitions

    tid = _task(store, "P", "a very long title " + "word " * 20)
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)
    t = store.get_task(tid)
    for w in (40, 80, 120):
        lines = con.task_lines(t, None, now_ms(), w)
        assert 1 <= len(lines) <= 4
        for ln in lines:
            assert "\n" not in ln
            assert ui.plain_len(ln) <= w


def test_elapsed_helper_shape():
    assert ui.elapsed(5_000) == "(5s)"
    assert ui.elapsed(133_000) == "(2m 13s)"
    assert ui.elapsed(3 * 3600_000 + 4 * 60_000) == "(3h 4m)"


def test_alert_strip_only_when_non_empty(store: Store):
    assert con.alert_text(store, scope.Scope(("P",)), 80) == ""
    comms.raise_alarm(store, "opencode down", critical=True)
    line = con.alert_text(store, scope.Scope(("P",)), 80)
    assert "🚨" in line


def test_live_view_lines_capped(tmp_path: Path, store: Store):
    from ahub.providers.fake import FakeProvider
    from ahub.tui.live import MAX_LINES, Feed

    log = tmp_path / "x.log"
    log.write_text("", encoding="utf-8")
    feed = Feed(FakeProvider(), log)
    feed.lines = ["l%d" % i for i in range(MAX_LINES + 10)]
    feed.update()
    assert len(feed.lines) <= MAX_LINES


async def test_refresh_keeps_input_text_and_focus(store: Store):
    from textual.widgets import Input

    _task(store, "P", "keep me")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one("#input", Input)
        inp.value = "hello /status"
        inp.focus()
        await pilot.pause(0.1)
        now = now_ms()
        snap = con.snapshot(store, app.scope, 80, now)
        app._apply(snap, [])
        await pilot.pause(0.2)
        assert app.query_one("#input", Input).value == "hello /status"
        assert app.focus_mode == "input"


async def test_tab_and_esc_move_focus(store: Store):
    _task(store, "P", "one")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app.focus_mode == "input"
        await pilot.press("tab")
        await pilot.pause(0.2)
        assert app.focus_mode == "tasks"
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.focus_mode == "input"


async def test_follow_opens_and_esc_returns(tmp_path: Path, store: Store):
    import json as _json

    from ahub import transcript

    tid = _task(store, "P", "follow me")
    log = tmp_path / "executor.log"
    log.write_text("", encoding="utf-8")
    with transcript.prompts_path(log).open("w", encoding="utf-8") as f:
        f.write(_json.dumps({"ts": now_ms(), "turn": 1, "kind": "start", "text": "hi"}) + "\n")
    with log.open("a", encoding="utf-8") as f:
        f.write(_json.dumps({"sessionID": "s", "type": "text", "text": "hello"}) + "\n")
    store.add_session(task_id=tid, provider="fake", role="executor", model="fake",
                      external_id="s", log_path=str(log))
    from ahub.pulse import Pulse

    fake_pulse = Pulse(task_id=tid, state="working", reason="reading")
    app = ConsoleApp(store=store, all_projects=True)
    app.pulses = lambda: {tid: fake_pulse}
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command(f"/follow T{tid}")
        await pilot.pause(0.4)
        assert app.screen.__class__.__name__ == "Transcript"
        assert app.screen._pulses() == {tid: fake_pulse}
        await pilot.press("p")
        await pilot.pause(0.3)
        assert app.screen.__class__.__name__ == "Prompt"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.screen.__class__.__name__ == "Transcript"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.screen.query_one("#input")


async def test_tasks_focus_selects_and_enter_follows(store: Store):
    from textual.widgets import Static

    from ahub import transitions

    ids = []
    for i in range(3):
        tid = _task(store, "P", f"task {i}")
        transitions.move(store, tid, State.PREPARING)
        ids.append(tid)
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        # Initially in input mode: no task has ▌
        pane = str(app.query_one("#tasks-inner", Static).render())
        assert "▌" not in pane

        await pilot.press("tab")
        await pilot.pause(0.2)
        assert app.focus_mode == "tasks"
        assert app._selected == 0
        pane = str(app.query_one("#tasks-inner", Static).render())
        lines0 = [ln for ln in pane.splitlines() if f"T{ids[0]}" in ln]
        lines1 = [ln for ln in pane.splitlines() if f"T{ids[1]}" in ln]
        assert len(lines0) == 1 and "▌" in lines0[0]
        assert len(lines1) == 1 and "▌" not in lines1[0]

        await pilot.press("down")
        await pilot.pause(0.2)
        assert app._selected == 1
        pane = str(app.query_one("#tasks-inner", Static).render())
        lines0 = [ln for ln in pane.splitlines() if f"T{ids[0]}" in ln]
        lines1 = [ln for ln in pane.splitlines() if f"T{ids[1]}" in ln]
        assert "▌" not in lines0[0]
        assert "▌" in lines1[0]

        await pilot.press("enter")
        await pilot.pause(0.4)
        assert app.screen.__class__.__name__ == "Transcript"
        assert app.screen.view.task_id == ids[1]


async def test_status_history_help_read_only(store: Store):
    _task(store, "P", "read me")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command("/status")
        app.run_command("/history")
        app.run_command("/help")
        await pilot.pause(0.2)
        assert len(app._transcript) >= 3


async def test_quit_exits_the_console(store: Store):
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command("/quit")
        await pilot.pause(0.3)
        assert not app.is_running


async def test_input_history_up_and_down(store: Store):
    from textual.widgets import Input

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one("#input", Input)
        inp.focus()
        await pilot.press(*"/status", "enter")
        await pilot.pause(0.3)
        await pilot.press("up")
        await pilot.pause(0.2)
        assert app.query_one("#input", Input).value == "/status"
        await pilot.press("down")
        await pilot.pause(0.2)
        assert app.query_one("#input", Input).value == ""


async def test_transcript_capped_with_earlier_line(store: Store):
    from textual.widgets import Static

    from ahub.i18n import t

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._say([f"line {i}" for i in range(con.TRANSCRIPT_CAP + 20)])
        assert len(app._transcript) == con.TRANSCRIPT_CAP
        assert app._transcript[-1] == f"line {con.TRANSCRIPT_CAP + 19}"
        text = str(app.query_one("#transcript-inner", Static).render())
        assert t("console.transcript_earlier") in text


async def test_only_the_console_footer_is_rendered(store: Store):
    from textual.widgets import Footer

    from ahub import transitions

    tid = _task(store, "P", "task for follow footer")
    transitions.move(store, tid, State.PREPARING)

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert len(app.query("#footer")) == 1
        assert not app.query(Footer)

        app.run_command(f"/follow T{tid}")
        await pilot.pause(0.4)
        assert app.screen.__class__.__name__ == "Transcript"
        assert not app.screen.query(Footer)


def test_pipe_and_json_byte_identical(capsys, monkeypatch, tmp_path):
    import ahub
    from ahub import home

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out == home.text() + "\n"
    assert out.startswith(f"ahub {ahub.__version__}")
    assert cli.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == ahub.__version__


def test_bare_tty_opens_console_not_home(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    called = {}

    def _fake(*, all_projects=False, project=None, control=False):
        called["all"] = all_projects
        called["project"] = project
        called["control"] = control
        return 7

    monkeypatch.setattr("ahub.tui.console.main", _fake)
    assert cli.main([]) == 7
    assert called == {"all": True, "project": None, "control": False}

    assert cli.main(["--all"]) == 7
    assert called == {"all": True, "project": None, "control": False}

    assert cli.main(["--project", "myproj"]) == 7
    assert called == {"all": False, "project": "myproj", "control": False}


def test_top_opens_console(monkeypatch):
    called = {}

    def _fake(*, all_projects=False, project=None, control=False):
        called["ok"] = True
        called["all"] = all_projects
        called["project"] = project
        called["control"] = control
        return 0

    monkeypatch.setattr("ahub.tui.console.main", _fake)
    assert cli.main(["top"]) == 0
    assert called == {"ok": True, "all": False, "project": "agent-hub", "control": False}

    assert cli.main(["top", "--control"]) == 0
    assert called == {"ok": True, "all": False, "project": "agent-hub", "control": True}

    assert cli.main(["top", "--all"]) == 0
    assert called == {"ok": True, "all": True, "project": None, "control": False}

    assert cli.main(["top", "--project", "other"]) == 0
    assert called == {"ok": True, "all": False, "project": "other", "control": False}


def test_snapshot_task_ids_follow_project_order(store: Store):
    from ahub import transitions

    z = _task(store, "Z_proj", "task in Z")
    a = _task(store, "A_proj", "task in A")
    transitions.move(store, z, State.PREPARING)
    transitions.move(store, a, State.PREPARING)
    now = now_ms()
    snap = con.snapshot(store, scope.Scope(), 80, now)
    assert snap.task_ids == [a, z]
    block_keys = [k for k, _ in snap.blocks]
    assert block_keys == ["head:A_proj", f"T{a}", "head:Z_proj", f"T{z}"]


async def test_console_app_pushes_transcript_and_prompt_without_extra_footer(store: Store):
    from textual.widgets import Footer

    from ahub.tui.app import Prompt, Transcript

    tid = _task(store, "P", "test task")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        tr = Transcript(store, tid)
        app.push_screen(tr)
        await pilot.pause(0.3)
        assert app.screen == tr
        assert not app.screen.query(Footer)

        pr = Prompt("Question?")
        app.push_screen(pr)
        await pilot.pause(0.3)
        assert app.screen == pr
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.screen == tr
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.screen.query_one("#input")


async def test_snapshot_deadline_timeout_enforced(store: Store, monkeypatch):
    import time

    def _slow_snapshot(*args, **kwargs):
        time.sleep(0.3)
        return con.Snapshot(welcome="SLOW")

    monkeypatch.setattr(con, "SNAPSHOT_DEADLINE_S", 0.05)
    monkeypatch.setattr(con, "snapshot", _slow_snapshot)

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app._stale_s > 0
        welcome_text = str(app.query_one("#welcome").render())
        assert "SLOW" not in welcome_text


async def test_question_mark_toggles_shortcuts_without_inserting(store: Store):
    from textual.widgets import Static

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        inp = app.query_one("#input", con.ConsoleInput)
        inp.focus()
        await pilot.pause(0.1)

        # Empty input: '?' toggles shortcuts on
        assert not app._show_shortcuts
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        assert app._show_shortcuts is True
        assert app.query_one("#shortcuts", Static).display is True
        assert inp.value == ""

        # Press again: toggles off
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        assert app._show_shortcuts is False
        assert app.query_one("#shortcuts", Static).display is False
        assert inp.value == ""

        # Non-empty input: '?' is inserted into the input
        inp.value = "/help"
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        assert inp.value == "/help?"
        assert app._show_shortcuts is False


def test_clip_width_never_cuts_inside_ansi():
    colored = "\033[38;5;208mSupercalifragilistic\033[0m"
    for w in (2, 3, 5, 8):
        clipped = ui.clip_width(colored, w)
        assert "\033[38;5;208m" in clipped
        assert clipped.endswith(ui.ELLIPSIS)
        assert not clipped.endswith("\033")
        assert ui.plain_len(clipped) <= w


def test_event_lines_statement_and_evidence(store: Store):
    from ahub.model import Ev

    tid = _task(store, "P", "my scout task")
    t = store.get_task(tid)

    # DONE event
    eid = store.add_event(Ev.DONE, task_id=tid, project="P",
                          payload={"summary": "all tests pass", "report_bytes": 2048})
    ev_done = store.events(after_id=eid - 1)[0]
    lines = con.event_lines(ev_done, t, 80)
    assert len(lines) == 2
    assert "DONE" in lines[0] and "my scout task" in lines[0]
    assert "all tests pass" not in lines[0]
    assert "⎿" in lines[1] and "all tests pass" in lines[1]

    # NEEDS_DECISION event
    eid = store.add_event(Ev.NEEDS_DECISION, task_id=tid, project="P", payload={"reason": "budget"})
    ev_dec = store.events(after_id=eid - 1)[0]
    lines_dec = con.event_lines(ev_dec, t, 80)
    assert len(lines_dec) == 2
    assert "DECISION" in lines_dec[0] and "my scout task" in lines_dec[0]
    assert "budget" not in lines_dec[0]
    assert "⎿" in lines_dec[1] and "budget" in lines_dec[1]

    # ERROR event
    eid = store.add_event(Ev.ERROR, task_id=tid, project="P", payload={"reason": "unexpected error"})
    ev_err = store.events(after_id=eid - 1)[0]
    lines_err = con.event_lines(ev_err, t, 80)
    assert len(lines_err) == 2
    assert "ERROR" in lines_err[0]
    assert "unexpected error" not in lines_err[0]
    assert "⎿" in lines_err[1] and "unexpected error" in lines_err[1]


async def test_done_decision_event_reaches_transcript_inner(store: Store):
    from textual.widgets import Static

    from ahub import transitions
    from ahub.model import Ev

    tid = _task(store, "P", "live event task")
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        store.add_event(Ev.DONE, task_id=tid, project="P", payload={"summary": "completed successfully"})
        app.refresh_data()
        await pilot.pause(0.5)
        text = str(app.query_one("#transcript-inner", Static).render())
        assert "DONE" in text
        assert "live event task" in text
        assert "⎿" in text
        assert "completed successfully" in text


def test_live_text_behaviour(store: Store):
    now = now_ms()
    assert con.live_text(store, scope.Scope(), now, 80) == ""

    tid = _task(store, "P", "active task")
    from ahub import transitions
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)

    text = con.live_text(store, scope.Scope(), now, 80, frame=0)
    assert text != ""
    assert f"T{tid}" in text
    assert ui.SPINNER[0] in text

    text_f1 = con.live_text(store, scope.Scope(), now, 80, frame=1)
    assert ui.SPINNER[1] in text_f1


def test_footer_text_behaviour(store: Store):
    tid = _task(store, "P", "footer task")
    from ahub import transitions
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)

    text = con.footer_text(store, scope.Scope(), 80, stale_s=0)
    assert "1 working" in text or "1 в работе" in text
    assert "Go $" in text and "USD $" in text
    assert "stale" not in text

    stale_text = con.footer_text(store, scope.Scope(), 80, stale_s=5)
    assert "stale 5s" in stale_text or "устарело 5" in stale_text


def test_feed_lines_filters_foreign_project_events(store: Store):
    from ahub.model import Ev

    p_tid = _task(store, "P", "P task")
    q_tid = _task(store, "Q", "Q task")

    app = ConsoleApp(store=store, project="P")
    app._last_event = 0

    store.add_event(Ev.DONE, task_id=p_tid, project="P", payload={"summary": "p finished"})
    store.add_event(Ev.DONE, task_id=q_tid, project="Q", payload={"summary": "q finished"})

    lines = app._feed_lines(80)
    joined = "\n".join(lines)
    assert "P task" in joined
    assert "p finished" in joined
    assert "Q task" not in joined
    assert "q finished" not in joined


@pytest.mark.parametrize("cols", [40, 80, 120])
async def test_console_app_runs_at_narrow_and_wide_terminals(store: Store, cols: int):
    tid = _task(store, "P", "terminal width task test")
    from textual.widgets import Static

    from ahub import transitions
    transitions.move(store, tid, State.PREPARING)

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test(size=(cols, 24)) as pilot:
        await pilot.pause(0.5)
        assert app.is_running
        tasks_text = str(app.query_one("#tasks-inner", Static).render())
        for ln in tasks_text.splitlines():
            assert ui.plain_len(ln) <= cols


async def test_widget_update_error_renders_in_place(store: Store, monkeypatch):
    from textual.widgets import Static

    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.4)
        welcome_widget = app.query_one("#welcome", Static)

        def _failing_update(*args, **kwargs):
            raise ValueError("simulated widget error")

        monkeypatch.setattr(welcome_widget, "update", _failing_update)

        snap = con.snapshot(store, app.scope, 80, now_ms())
        app._apply(snap, [])
        await pilot.pause(0.2)
        assert app.is_running
        # Error is rendered in place in welcome widget
        rendered = str(app.query_one("#welcome", Static).render())
        assert "✗ welcome:" in rendered


async def test_top_control_focuses_tasks_pane(store: Store):
    from textual.widgets import Static

    from ahub import transitions

    tid = _task(store, "P", "control task")
    transitions.move(store, tid, State.PREPARING)

    app = ConsoleApp(store=store, all_projects=True, control=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert app.focus_mode == "tasks"
        pane = str(app.query_one("#tasks-inner", Static).render())
        assert "▌" in pane
