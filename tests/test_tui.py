"""TUI hub top: таблица, спарклайн, лента, узкий режим, не-TTY."""

from __future__ import annotations

import sys
from pathlib import Path

from hub.read.snapshot import SessionSnap, Snapshot, TaskSnap
from hub.store import Store
from hub.tui.app import HubApp
from hub.tui.widgets import BLOCKS, EventFeed, TaskTable, sparkline

NOW = 1_789_000_000_000


def _snap(task_id="T01", pulse="🔴", stage="exec r1", last="bash: pytest -q",
          project="P", ctx=1000) -> Snapshot:
    sess = SessionSnap(
        external_id="s1", role="executor", model="muse", provider="opencode-go",
        pulse_ms=NOW - 21 * 60_000, pulse=pulse, cost=0.1, go=True,
        context_tokens=ctx, last_activity=last,
    )
    task = TaskSnap(
        id=task_id, project=project, stage=stage, round=1, pulse=pulse,
        cost_go=0.1, cost_usd=0.0, context=ctx, last_activity=last,
        sessions=[sess],
    )
    return Snapshot(tasks=[task], total_go=0.1, total_usd=0.0, now_ms=NOW)


def _app(tmp_path) -> HubApp:
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    return HubApp(store=Store(), opencode_db=None, proc_root=str(proc))


# --- спарклайн ---

def test_sparkline_ladder_and_empty():
    assert sparkline([]) == ""
    got = sparkline([0, 1, 2, 3])
    assert len(got) == 4
    idx = [BLOCKS.index(c) for c in got]
    assert idx == sorted(idx) and len(set(idx)) > 1  # лесенка вверх
    assert got[0] == BLOCKS[0]  # с нижнего блока
    assert sparkline([5, 5, 5]) == "▅▅▅"  # плоский ряд
    assert len(sparkline(list(range(20)), width=10)) == 10  # обрезка по ширине


# --- таблица показывает 🔴 ---

async def test_table_shows_red_pulse(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        table.update(_snap(pulse="🔴"))
        await pilot.pause()
        assert table.row_count == 1
        row = table.get_row("T01")
        assert any("🔴" in str(c) for c in row)


# --- q завершает ---

async def test_press_q_quits(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running


# --- узкий режим ---

async def test_narrow_hides_details_and_feed(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.query_one("#details").display is False
        assert app.query_one("#events").display is False


async def test_wide_shows_details_and_feed(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#details").display is not False
        assert app.query_one("#events").display is not False


# --- обновление без роста строк ---

async def test_update_changes_row_without_growth(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        table.update(_snap(stage="exec r1", last="думает"))
        await pilot.pause()
        assert table.row_count == 1
        before = [str(c) for c in table.get_row("T01")]
        table.update(_snap(stage="review r1", last="bash: pytest -q tests/"))
        await pilot.pause()
        assert table.row_count == 1
        after = [str(c) for c in table.get_row("T01")]
        assert before != after
        assert any("review" in c for c in after)


# --- лента ---

async def test_eventfeed_push_adds_lines(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        feed = app.query_one("#events", EventFeed)
        before = len(feed.lines)
        feed.push([
            {"id": 1, "task_id": "T01", "kind": "stage", "payload_json": "{}"},
            {"id": 2, "task_id": "T01", "kind": "stuck", "payload_json": "{}"},
        ])
        await pilot.pause()
        assert len(feed.lines) > before


# --- hub top в не-TTY ---

def test_top_nontty_exit3(monkeypatch, capsys):
    from hub.cli import main

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert main(["top"]) == 3
    out = capsys.readouterr()
    assert "нужен терминал" in out.out + out.err


# --- нет прямого чтения чужих БД ---

def test_no_direct_db_read():
    for name in ("__init__.py", "app.py", "widgets.py"):
        text = (Path("hub/tui") / name).read_text(encoding="utf-8")
        assert "sqlite3" not in text
