"""`ahub top`: screen data and app behaviour (textual pilot, no terminal)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import comms, events, transcript, transitions
from ahub.model import State
from ahub.service import PAUSE_KEY
from ahub.store import Store
from ahub.time import now_ms
from ahub.tui import data
from ahub.tui.app import TopApp
from ahub.tui.live import LiveView


@pytest.fixture
def store() -> Store:
    return Store()


def fill(store):
    a = store.create_task(project="P", kind="scout", title="где утечка")
    b = store.create_task(project="P", kind="code", title="кнопка оплаты")
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, b, st, reason="ревью: все согласны")
    return a, b


def accepted(store) -> int:
    """A finished (accepted) task — hidden in the current view."""
    tid = store.create_task(project="P", kind="code", title="уже принята")
    for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
        transitions.move(store, tid, st, reason="ок")
    return tid


def test_screen_data(store):
    a, b = fill(store)
    comms.raise_alarm(store, "opencode лёг", critical=True)
    screen, live, pulses = data.snapshot(store, projects=[])
    assert "сервис не отвечает" in screen.header and "тревог 1" in screen.header and "в очереди 1" in screen.header
    marks = {r.task_id: r.mark for r in screen.rows}
    assert marks[a] == "⏳" and marks[b] == "✅"
    assert any("DONE" in f for f in screen.feed) and any("→ готово" in f for f in screen.feed)
    events.touch(store)
    assert "Claude на связи" in data.header(store, {}, __import__("ahub.time", fromlist=["now_ms"]).now_ms())


def _fake_totals(day_go: float, month_go: float):
    from ahub.providers.base import Usage

    calls: list = []

    def _fake(since_ms: int, db_path=None, until_ms=None):
        calls.append(since_ms)
        if len(calls) == 1:
            return Usage(cost_go=day_go, cost_usd=0.0)
        return Usage(cost_go=month_go, cost_usd=0.0)

    return _fake


def test_money_with_limit(store, monkeypatch):
    from ahub.providers import opencode_db
    from ahub.time import now_ms

    monkeypatch.setattr(opencode_db, "totals", _fake_totals(5.0, 30.0))
    head = data.header(store, {}, now_ms(), go_limit=60.0)
    assert "месяц $30.00 из $60" in head and "50 %" in head
    assert "лимит превышен" not in head
    over = data.header(store, {}, now_ms(), go_limit=20.0)
    assert "лимит превышен" in over


def test_money_without_limit(store, monkeypatch):
    from ahub.providers import opencode_db
    from ahub.time import now_ms

    monkeypatch.setattr(opencode_db, "totals", _fake_totals(5.0, 30.0))
    head = data.header(store, {}, now_ms(), go_limit=None)
    assert "месяц $30.00" in head and "из $" not in head and "%" not in head


def test_money_limit_from_config(store, tmp_path, monkeypatch):
    from ahub import config, paths
    from ahub.providers import opencode_db
    from ahub.time import now_ms
    from ahub.tui import data as tuidata
    from tests.conftest import write

    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    monkeypatch.setattr(opencode_db, "totals", _fake_totals(5.0, 30.0))
    assert "из $" not in tuidata.header(store, {}, now_ms())
    write(paths.global_config_path(), "[usage]\ngo_month_limit = 60.0\n")
    assert config.load_hub().go_month_limit == 60.0
    assert "из $60" in tuidata.header(store, {}, now_ms())


async def test_app_view_mode_blocks_actions(store):
    a, b = fill(store)
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert "ПРОСМОТР" in str(app.query_one("#mode").render())
        assert len(app._ids) == 2
        await pilot.press("s")  # in view mode it does nothing
        await pilot.pause(0.2)
        assert store.get_task(app.selected()).state in (State.QUEUED, State.DONE)
        await pilot.press("c")
        assert "УПРАВЛЕНИЕ" in str(app.query_one("#mode").render())


async def test_app_stop_with_confirm(store):
    a, b = fill(store)
    app = TopApp(store=store, projects=[], control=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=app._ids.index(a))
        await pilot.press("s")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.3)
        assert store.get_task(a).state is State.STOPPED


async def test_app_nudge_message(store):
    """`m` in control mode asks for the text and stores the request; in view mode nothing happens."""
    a, b = fill(store)
    transitions.move(store, a, State.PREPARING)
    transitions.move(store, a, State.WORKING)
    transitions.acquire(store, a, "own", pid=5)
    store.add_session(task_id=a, provider="fake", role="executor", model="fake", external_id="ses_x")
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=app._ids.index(a))
        await pilot.press("m")  # view mode: nothing happens
        await pilot.pause(0.2)
        assert app.screen.__class__.__name__ != "Ask" and store.get_task(a).request == ""
        await pilot.press("c")  # control mode on
        await pilot.press("m")
        await pilot.pause(0.2)
        await pilot.press(*"продолжай, почини")
        await pilot.press("enter")
        await pilot.pause(0.3)
    t = store.get_task(a)
    assert t.request == "nudge" and t.request_text == "продолжай, почини"
    assert [e.payload["by"] for e in store.events(task_id=a) if e.kind == "nudge"] == ["human"]


async def test_app_help(store):
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        assert app.screen.__class__.__name__ == "Help"
        await pilot.press("escape")


# --- current work by default, history on h ---


def test_current_view_hides_finished(store):
    a, b = fill(store)
    c = accepted(store)
    screen, _, _ = data.snapshot(store, projects=[])
    assert {r.task_id for r in screen.rows} == {a, b} and c not in {r.task_id for r in screen.rows}
    assert "текущие" in screen.header and "история" not in screen.header
    old, _, _ = data.snapshot(store, projects=[], history=True)
    assert {r.task_id for r in old.rows} == {a, b, c}
    assert "история" in old.header


async def test_app_history_toggle(store):
    a, b = fill(store)
    c = accepted(store)
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert set(app._ids) == {a, b}
        await pilot.press("h")
        await pilot.pause(0.4)
        assert set(app._ids) == {a, b, c}
        assert "история" in str(app.query_one("#header").render())
        await pilot.press("h")
        await pilot.pause(0.4)
        assert set(app._ids) == {a, b} and "текущие" in str(app.query_one("#header").render())


# --- the live transcript screen ---


def _session(store: Store, tid: int, log: Path, *, role: str = "executor", round_no: int = 1,
             status: str = "ok") -> None:
    row = store.add_session(task_id=tid, provider="fake", role=role, model="fake", round=round_no,
                            external_id=f"ses_{log.stem}", log_path=str(log))
    store.update_session(row, status=status, ended_at=None if status == "running" else 1)


def _say(log: Path, texts: list[str]) -> Path:
    """What a worker said — one JSON line per phrase of the fake provider."""
    with log.open("a", encoding="utf-8") as f:
        for txt in texts:
            f.write(json.dumps({"sessionID": "ses_x", "type": "text", "text": txt}, ensure_ascii=False) + "\n")
    return log


def _fake_log(log: Path, texts: list[str], prompt: str = "Почини округление суммы.") -> Path:
    """A session log of the fake provider with the prompts sidecar next to it."""
    log.write_text("", encoding="utf-8")
    with transcript.prompts_path(log).open("w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_ms(), "turn": 1, "kind": "start", "text": prompt}) + "\n")
    return _say(log, texts)


def test_live_view_builds_lines_and_appends(tmp_path, store):
    tid = store.create_task(project="P", kind="code", title="починить")
    log = _fake_log(tmp_path / "executor.log", ["Смотрю код."])
    _session(store, tid, log)
    view = LiveView(store, tid)
    text = "\n".join(view.lines)
    assert "── Turn 1 · start · " in text and "Смотрю код." in text and "Почини округление суммы." in text
    head = view.header()
    assert f"T{tid}" in head and "executor" in head and "fake" in head and "эфир" in head
    assert view.prompt() == "Почини округление суммы."  # the `p` key
    assert view.update() is False  # the log did not grow
    _say(log, ["Правлю."])
    assert view.update() is True
    assert "\n".join(view.lines).endswith("Правлю.")
    assert text.count("Смотрю код.") == 1  # only the new bytes are read


def test_live_view_role_and_round(tmp_path, store):
    tid = store.create_task(project="P", kind="code", title="починить")
    _session(store, tid, _fake_log(tmp_path / "executor.log", ["круг 1"]), round_no=1)
    _session(store, tid, _fake_log(tmp_path / "reviewer_r1.log", ["ревью 1"]), round_no=1, role="reviewer")
    _session(store, tid, _fake_log(tmp_path / "executor_r2.log", ["круг 2"]), round_no=2)
    view = LiveView(store, tid)
    assert (view.session.round, view.session.role) == (2, "executor")  # the latest session
    assert view.step_round(-1) is True and view.session.round == 1 and view.session.role == "executor"
    assert view.step_round(-1) is False  # there is no earlier round
    assert view.switch_role() is True and view.session.role == "reviewer"
    assert "ревью 1" in "\n".join(view.lines)
    assert view.step_round(1) is True and (view.session.round, view.session.role) == (2, "executor")
    assert view.switch_role() is False  # one role in this round


def test_live_view_without_sessions(store):
    tid = store.create_task(project="P", kind="scout", title="разведка")
    view = LiveView(store, tid)
    assert view.session is None and "нет сессий" in view.lines[0]
    assert "сессии нет" in view.header() and view.update() is False


async def test_app_transcript_screen(tmp_path, store):
    tid = store.create_task(project="P", kind="code", title="починить")
    _session(store, tid, _fake_log(tmp_path / "executor.log", ["Смотрю код."]), status="running")
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=app._ids.index(tid))
        await pilot.press("t")
        await pilot.pause(0.3)
        screen = app.screen
        assert screen.__class__.__name__ == "Transcript"
        assert "Смотрю код." in str(screen.query_one("#live-log").render())
        assert "эфир" in str(screen.query_one("#live-head").render())
        await pilot.press("p")  # the full prompt of the screen, not the pause of the table
        await pilot.pause(0.2)
        assert app.screen.__class__.__name__ == "Prompt"
        await pilot.press("escape")
        await pilot.pause(0.2)
        await pilot.press("a")  # the screen only reads: no accept dialog behind it
        await pilot.pause(0.2)
        assert app.screen.__class__.__name__ == "Transcript"
        await pilot.press("escape")
        await pilot.pause(0.2)
        app.screen.query_one("#tasks")  # back at the table
        assert store.meta_get(PAUSE_KEY) is None
        await pilot.press("enter")  # enter on a row opens the transcript too
        await pilot.pause(0.3)
        assert app.screen.__class__.__name__ == "Transcript"
        await pilot.press("q")
        await pilot.pause(0.2)
        app.screen.query_one("#tasks")
        assert app.is_running  # q on the transcript screen is "back", not "quit"


async def test_transcript_screen_nudges_the_worker(tmp_path, store):
    """`m` inside the transcript screen: the same ask, with the task of the screen; the table's own `m`
    waits behind the screen like every other table key."""
    tid = store.create_task(project="P", kind="code", title="починить")
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)
    transitions.acquire(store, tid, "own", pid=5)
    _session(store, tid, _fake_log(tmp_path / "executor.log", ["Смотрю код."]), status="running")
    app = TopApp(store=store, projects=[], control=True)
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=app._ids.index(tid))
        await pilot.press("t")
        await pilot.pause(0.3)
        assert app.check_action("nudge", ()) is False  # the table key is not offered behind the screen
        assert await app.run_action("app.nudge") is False and store.get_task(tid).request == ""
        await pilot.press("m")  # the screen's own m
        await pilot.pause(0.2)
        assert app.screen.__class__.__name__ == "Ask"
        await pilot.press(*"хватит, почини", "enter")
        await pilot.pause(0.3)
        assert app.screen.__class__.__name__ == "Transcript"  # the screen is still under the dialog
    assert store.get_task(tid).request_text == "хватит, почини"


async def test_transcript_tail_holds_when_scrolled_up(tmp_path, store):
    """The tail follows the log while it is at the end; a scroll up holds it, `f` brings it back."""
    from textual.containers import VerticalScroll

    tid = store.create_task(project="P", kind="code", title="починить")
    log = _fake_log(tmp_path / "executor.log", [f"строка {i}" for i in range(300)])
    _session(store, tid, log, status="running")
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app.query_one("#tasks").move_cursor(row=0)
        await pilot.press("t")
        await pilot.pause(0.3)
        screen = app.screen
        box = screen.query_one("#live-box", VerticalScroll)
        assert box.is_vertical_scroll_end and screen.view.following
        box.scroll_home(animate=False)
        screen.refresh_live()
        await pilot.pause(0.3)
        assert not screen.view.following
        assert "на паузе" in str(screen.query_one("#live-head").render())
        _say(log, ["ещё строчка"])  # a new line does not drag the reader down
        screen.refresh_live()
        await pilot.pause(0.3)
        assert "ещё строчка" in str(screen.query_one("#live-log").render())
        await pilot.press("f")
        await pilot.pause(0.3)
        assert screen.view.following and "эфир" in str(screen.query_one("#live-head").render())


def test_screen_data_en(store, monkeypatch):
    """Step 4: the TUI header and task row in English (AHUB_LANG=en)."""
    import re as _re

    from ahub.i18n import _reset
    from ahub.time import now_ms

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    a, b = fill(store)
    comms.raise_alarm(store, "opencode down", critical=True)
    screen, live, pulses = data.snapshot(store, projects=[])
    assert "service is down" in screen.header and "alarms 1" in screen.header
    assert "queued 1" in screen.header and "working" in screen.header
    by_id = {r.task_id: r for r in screen.rows}
    assert by_id[a].state == "queued" and by_id[b].state == "done"
    assert not _re.search(r"[а-яА-ЯёЁ]", screen.header + by_id[a].state + by_id[b].state)
    events.touch(store)
    assert "Claude is here" in data.header(store, {}, now_ms())
    _reset()


async def test_app_detail_shows_brackets_and_colours_as_text(store, monkeypatch):
    """A '[' in a worker report is text, not markup; the colours of a TTY never crash the panel."""
    monkeypatch.setattr("ahub.ui.colour_on", lambda: True)
    tid = store.create_task(project="P", kind="scout", title="брекеты [--all? (оставить)]")
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, tid, st, reason="отчёт [x] готов")
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        app._show_detail()
        await pilot.pause(0.1)
        text = str(app.query_one("#detail").render())
        assert "[--all? (оставить)]" in text
        assert "\x1b[" not in text
