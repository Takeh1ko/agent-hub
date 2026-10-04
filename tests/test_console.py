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
    sc = scope.Scope(("P",))
    box = con.welcome_box(store, sc, now_ms(), 80)
    assert "ahub" in box and "3." in box
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
            assert ui.plain_len(ln) <= w + 20  # ANSI adds no width; mark + indent fit


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
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command(f"/follow T{tid}")
        await pilot.pause(0.4)
        assert app.screen.__class__.__name__ == "Transcript"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert app.screen.query_one("#input")


async def test_status_history_help_quit_read_only(store: Store):
    _task(store, "P", "read me")
    app = ConsoleApp(store=store, all_projects=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.run_command("/status")
        app.run_command("/history")
        app.run_command("/help")
        await pilot.pause(0.2)
        assert len(app._transcript) >= 3


def test_pipe_and_json_byte_identical(capsys, monkeypatch, tmp_path):
    from ahub import home

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out == home.text() + "\n"
    assert cli.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"].startswith("3.")


def test_bare_tty_opens_console_not_home(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    called = {}

    def _fake(*, all_projects=False, project=None):
        called["all"] = all_projects
        called["project"] = project
        return 7

    monkeypatch.setattr("ahub.tui.console.main", _fake)
    assert cli.main([]) == 7
    assert "all" in called


def test_top_opens_console(monkeypatch):
    called = {}

    def _fake(*, all_projects=False, project=None):
        called["ok"] = True
        return 0

    monkeypatch.setattr("ahub.tui.console.main", _fake)
    assert cli.main(["top"]) == 0
    assert called.get("ok") is True


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


def test_console_app_css_has_transcript_and_prompt_rules():
    css = ConsoleApp.CSS
    for selector in ("#live-head", "#live-box", "#live-log", "#prompt-box", "#prompt-text", "Prompt", "Transcript"):
        assert selector in css


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
