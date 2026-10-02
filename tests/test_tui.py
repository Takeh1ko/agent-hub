"""`ahub top`: данные экрана и поведение приложения (пилот textual, без терминала)."""

from __future__ import annotations

import pytest

from ahub import comms, events, transitions
from ahub.model import State
from ahub.store import Store
from ahub.tui import data
from ahub.tui.app import TopApp


@pytest.fixture
def store() -> Store:
    return Store()


def fill(store):
    a = store.create_task(project="P", kind="scout", title="где утечка")
    b = store.create_task(project="P", kind="code", title="кнопка оплаты")
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, b, st, reason="ревью: все согласны")
    return a, b


def test_screen_data(store):
    a, b = fill(store)
    comms.raise_alarm(store, "opencode лёг", critical=True)
    screen, live, pulses = data.snapshot(store, projects=[])
    assert "сервис не отвечает" in screen.header and "тревог 1" in screen.header and "в очереди 1" in screen.header
    marks = {r.task_id: r.mark for r in screen.rows}
    assert marks[a] == "⏳" and marks[b] == "✅"
    assert any("ГОТОВО" in f for f in screen.feed) and any("→ готово" in f for f in screen.feed)
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
    from tests.conftest import write

    from ahub.tui import data as tuidata

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
        await pilot.press("s")  # в просмотре — ничего не делает
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


async def test_app_help(store):
    app = TopApp(store=store, projects=[])
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause(0.2)
        assert app.screen.__class__.__name__ == "Help"
        await pilot.press("escape")
