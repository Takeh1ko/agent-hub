"""TUI hub top: таблица, спарклайн, лента, узкий режим, не-TTY."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hub.read.snapshot import SessionSnap, Snapshot, TaskSnap
from hub.store import Store
from hub.tui.app import HubApp
from hub.tui.widgets import BLOCKS, COL_KEYS, EventFeed, TaskTable, sparkline

NOW = 1_789_000_000_000

SCHEMA_OC = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
  title TEXT NOT NULL, model TEXT, cost REAL DEFAULT 0 NOT NULL,
  tokens_input INTEGER DEFAULT 0 NOT NULL, tokens_output INTEGER DEFAULT 0 NOT NULL,
  tokens_cache_read INTEGER DEFAULT 0 NOT NULL, tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
  priority TEXT NOT NULL, position INTEGER NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
"""


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


def _snap_two_sessions(task_id="T01", pulse="🔴") -> Snapshot:
    """Детали длиннее панели: две сессии и две активности (~12 строк)."""
    base = _snap(task_id=task_id, pulse=pulse)
    t = base.tasks[0]
    s2 = replace(t.sessions[0], last_activity="второй запуск: gate r1")
    t2 = replace(t, sessions=[t.sessions[0], s2])
    return Snapshot(tasks=[t2], total_go=base.total_go,
                    total_usd=base.total_usd, now_ms=NOW)


def _app(tmp_path) -> HubApp:
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    return HubApp(store=Store(), opencode_db=None, proc_root=str(proc))


def _make_oc_db(path: Path, now: int) -> Path:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA_OC)
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses1", "p", "/wt/T01", "задача",
         json.dumps({"id": "muse", "providerID": "opencode-go"}),
         0.5, 0, 0, 0, 0, now - 60_000, now - 30_000))
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m1", "ses1", now - 60_000, now - 50_000, json.dumps({
            "role": "assistant",
            "tokens": {"input": 1000, "cache": {"read": 4000}}})))
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p1", "m1", "ses1", now - 40_000, now - 30_000, json.dumps({
            "type": "tool", "tool": "bash",
            "state": {"status": "running",
                      "input": {"command": "pytest -q tests/"},
                      "time": {"start": now - 25_000}}})))
    con.commit()
    con.close()
    return path


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
        assert str(row[COL_KEYS.index("pulse")]) == "🔴"


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
        assert app.query_one("#details").display is True
        assert app.query_one("#events").display is True


async def test_resize_updates_narrow_mode(tmp_path):
    """MEDIUM: узкий режим — по новому размеру из события, без отставания."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#details").display is True
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.query_one("#details").display is False
        assert app.query_one("#events").display is False
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        assert app.query_one("#details").display is True
        assert app.query_one("#events").display is True


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


async def test_update_keeps_row_identity(tmp_path):
    """Контракт: обновление по ключу, без полного пересоздания строк."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        table.update(_snap(stage="exec r1", last="думает"))
        await pilot.pause()
        key_before = table.ordered_rows[0].key
        table.update(_snap(stage="review r1", last="bash: pytest -q"))
        await pilot.pause()
        assert table.row_count == 1
        assert table.ordered_rows[0].key is key_before


async def test_update_reorders_rows_like_snapshot(tmp_path):
    """Порядок строк — как в снимке: свежая задача поднимается вверх."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)

        def snap_of(*ids: str) -> Snapshot:
            return Snapshot(tasks=[_snap(task_id=i).tasks[0] for i in ids],
                            total_go=0.2, total_usd=0.0, now_ms=NOW)

        table.update(snap_of("TA", "TB"))
        await pilot.pause()
        assert [r.key.value for r in table.ordered_rows] == ["TA", "TB"]
        table.update(snap_of("TB", "TA"))
        await pilot.pause()
        assert table.row_count == 2
        assert [r.key.value for r in table.ordered_rows] == ["TB", "TA"]


async def test_spark_cell_follows_context(tmp_path):
    """Источник спарклайна таблицы: растущий context даёт лесенку вверх."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        spark_col = COL_KEYS.index("tokens")
        for ctx in (100, 300, 900, 2000, 5000):
            table.update(_snap(ctx=ctx))
            await pilot.pause()
        spark = str(table.get_row("T01")[spark_col])
        assert len(spark) == 5
        idx = [BLOCKS.index(c) for c in spark]
        # Лесенка от нижнего блока до верхнего, а не плоский ряд/неравенство.
        assert idx == sorted(idx)
        assert idx[0] == 0 and idx[-1] == len(BLOCKS) - 1


# --- Enter открывает детали ---

async def test_enter_opens_details(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._apply_snapshot(_snap(), bot_alive=False, events=[])
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        assert table.row_count == 1
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app._details_task == "T01":
                break
        assert app._details_task == "T01"
        from textual.widgets import Static

        text = str(app.query_one("#details-text", Static).render())
        assert "Задача T01" in text


async def test_details_panel_scrolls_to_findings(tmp_path):
    """HIGH: хвост деталей (todo, замечания) достижим прокруткой и виден."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._apply_snapshot(_snap_two_sessions(), bot_alive=False, events=[])
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause()
            if app._details_task == "T01":
                break
        assert app._details_task == "T01"
        await pilot.pause()
        inner = app.query_one("#details-text", Static)
        nlines = len(str(inner.render()).splitlines())
        assert nlines > 7  # в старом Static(7 строк контента) хвост терялся
        assert "Замечания:" in str(inner.render())
        vs = app.query_one("#details", VerticalScroll)
        assert vs.max_scroll_y > 0 or vs.size.height >= nlines
        assert "Замечания" not in app.export_screenshot()  # хвост обрезан
        vs.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        assert "Замечания" in app.export_screenshot()  # хвост на экране


async def test_enter_keeps_details_hidden_when_narrow(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._apply_snapshot(_snap(), bot_alive=False, events=[])
        await pilot.pause()
        table = app.query_one("#tasks", TaskTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app._details_task == "T01":
                break
        assert app._details_task == "T01"
        assert app.query_one("#details").display is False


def test_details_content_honest():
    """Детали: id/этап/строка сессии есть, выдуманных '20 вызовов' нет."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        proc = Path(td) / "proc"
        proc.mkdir()
        app = HubApp(store=Store(path=str(Path(td) / "hub.db")),
                     opencode_db=None, proc_root=str(proc))
        app._snap = _snap()
        text = app._build_detail_text("T01")
        assert "Задача T01" in text
        assert "exec r1" in text
        assert "executor:muse" in text
        assert "нет источника в H01" in text
        assert "последние 20" not in text
        app._snap = Snapshot(tasks=[], total_go=0.0, total_usd=0.0, now_ms=NOW)
        assert "(нет в снимке)" in app._build_detail_text("T01")


# --- фильтр проекта и финальных этапов ---

def test_filtered_drops_merged_and_other_project(tmp_path):
    app = _app(tmp_path)
    app.project_filter = "PlayerUP"
    other = _snap(task_id="TB", project="Other").tasks[0]
    merged = _snap(task_id="TM", stage="merged", project="PlayerUP/x").tasks[0]
    mine = _snap(task_id="TA", project="PlayerUP/x").tasks[0]
    snap = Snapshot(tasks=[mine, other, merged],
                    total_go=0.3, total_usd=0.0, now_ms=NOW)
    got = app._filtered(snap)
    assert [t.id for t in got.tasks] == ["TA"]
    # Итоги шапки фильтр не трогает.
    assert got.total_go == 0.3 and got.now_ms == NOW


def test_header_counts_and_bot_unknown(tmp_path):
    app = _app(tmp_path)
    snap = Snapshot(tasks=[
        _snap(task_id="T1", stage="exec r1").tasks[0],
        _snap(task_id="T2", stage="queued").tasks[0],
        _snap(task_id="T3", stage="merged").tasks[0],
    ], total_go=0.2, total_usd=0.0, now_ms=NOW)
    text = app._header_text(snap, bot_alive=False)
    # Очередь не входит в 'активно'; бота без данных не хороним.
    assert "активно 1 · в очереди 1" in text
    assert "бот ?" in text and "бот нет" not in text
    # total_go — расход за сегодня, лимит 60 — месячный котёл: подпись честная.
    assert "мес" in text
    assert "бот жив" in app._header_text(snap, bot_alive=True)


def _fake_proc(root: Path, pid: str, cmdline: bytes) -> None:
    """Один процесс в фейковом /proc: cmdline + stat + cwd."""
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(cmdline)
    (d / "stat").write_text(
        f"{pid} (hub) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0\n",
        encoding="utf-8")
    try:
        os.symlink(str(root), d / "cwd")
    except OSError:
        pass


def test_bot_seen_via_real_agent_procs(tmp_path):
    """MEDIUM: настоящий agent_procs видит hub bot на фейковом /proc."""
    from hub.read import procs as hub_procs

    root = tmp_path / "proc-bot"
    _fake_proc(root, "424242", b"hub\x00bot\x00")
    live = hub_procs.agent_procs(str(root))
    assert [(p.pid, p.kind) for p in live] == [(424242, "hub_bot")]
    app = HubApp(store=Store(), opencode_db=None, proc_root=str(root))
    assert app._check_bot() is True


def test_bot_absent_and_no_false_positive(tmp_path):
    """Другой hub-вызов — не бот; pytest с hub/bot в пути — не бот."""
    from hub.read import procs as hub_procs

    root = tmp_path / "proc-nobot"
    _fake_proc(root, "111", b"hub\x00status\x00")
    _fake_proc(root, "222", b"pytest\x00tests/test_hub_bot.py\x00")
    live = hub_procs.agent_procs(str(root))
    assert [p.kind for p in live] == ["pytest"]
    app = HubApp(store=Store(), opencode_db=None, proc_root=str(root))
    assert app._check_bot() is False


# --- живой путь _fetch_snapshot: БД по умолчанию ---

def test_fetch_snapshot_reads_default_db(tmp_path):
    # HOME уже подменён conftest на tmp_path — кладём БД как в hub status.
    db = tmp_path / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    now_real = int(time.time() * 1000)
    _make_oc_db(db, now_real)
    store = Store()
    store.upsert_task(id="T01", project="P", stage="exec r1", round=1,
                      worktree=str(tmp_path / "wt"))
    store.link_session("ses1", "opencode", "T01", "executor", 1, "muse")
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    app = HubApp(store=store, opencode_db=None, proc_root=str(proc))
    assert app._resolve_opencode_db() == str(db)
    snap = app._fetch_snapshot()
    assert snap is not None
    task = {t.id: t for t in snap.tasks}["T01"]
    assert task.context == 5000
    assert abs(task.cost_go - 0.5) < 1e-9
    assert "pytest" in task.last_activity
    assert abs(snap.total_go - 0.5) < 1e-9


def test_top_passes_existing_db(monkeypatch, tmp_path, capsys):
    db = tmp_path / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    captured: dict = {}
    made: list = []

    class FakeApp:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.project_filter = None
            self.ran = False
            made.append(self)

        def run(self):
            self.ran = True
            return 0

    monkeypatch.setattr("hub.tui.app.HubApp", FakeApp)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    from hub.cli import main

    assert main(["top"]) == 0
    assert captured.get("opencode_db") == str(db)
    assert made and made[-1].ran is True
    assert made[-1].project_filter is None
    db.unlink()
    captured.clear()
    made.clear()
    assert main(["top", "--project", "/x"]) == 0
    assert captured.get("opencode_db") is None
    assert made and made[-1].ran is True
    assert made[-1].project_filter == "/x"


# --- живой опрос заполняет таблицу ---

async def test_refresh_pipeline_fills_table(tmp_path):
    store = Store()
    store.upsert_task(id="T99", project="P", stage="exec r1", round=1,
                      worktree=str(tmp_path / "wt"))
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    app = HubApp(store=store, opencode_db=None, proc_root=str(proc))
    async with app.run_test(size=(120, 30)) as pilot:
        table = app.query_one("#tasks", TaskTable)
        for _ in range(100):
            await pilot.pause()
            if table.row_count >= 1:
                break
        assert table.row_count == 1
        assert table.ordered_rows[0].key.value == "T99"


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


def test_eventfeed_bounded():
    from hub.tui.widgets import EventFeed as EF

    assert EF().max_lines == 200


async def test_snapshot_events_reach_feed_and_interval(tmp_path, monkeypatch):
    """Связка 'события снапшота → лента' и опрос каждые 2 с."""
    calls: list = []
    orig = HubApp.set_interval

    def fake_interval(self, interval, *args, **kwargs):
        calls.append(interval)
        return orig(self, interval, *args, **kwargs)

    monkeypatch.setattr(HubApp, "set_interval", fake_interval)
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        feed = app.query_one("#events", EventFeed)
        before = len(feed.lines)
        app._apply_snapshot(_snap(), bot_alive=False, events=[
            {"id": 1, "task_id": "T01", "kind": "stage", "payload_json": "{}"},
        ])
        await pilot.pause()
        assert len(feed.lines) > before
        assert app._last_event_id == 1
    assert calls and calls[0] == 2.0


def test_first_fetch_primes_feed_without_history(tmp_path):
    """Первый тик ленту историей не заливает, только запоминает id."""
    store = Store()
    old = store.add_event("T01", "stage", {"s": 1})
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    app = HubApp(store=store, opencode_db=None, proc_root=str(proc))
    assert app._fetch_events() == []
    assert app._last_event_id == old
    new = store.add_event("T01", "stage", {"s": 2})
    got = app._fetch_events()
    assert [e["id"] for e in got] == [new]


async def test_run_hub_cmd_reports_error(tmp_path, monkeypatch):
    """Ненулевой код — notify с severity='error', запуск через свой python."""
    app = _app(tmp_path)
    seen: dict = {}
    notes: list = []

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=2, stdout="",
                               stderr="usage: нет такой команды")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(app, "notify",
                        lambda msg, *a, **k: notes.append((msg, k)))
    monkeypatch.setattr(app, "_schedule_refresh",
                        lambda: seen.setdefault("refresh", True))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app._run_hub_cmd(["stop", "T01"])
        await pilot.pause()
    assert seen["cmd"][:3] == [sys.executable, "-m", "hub.cli"]
    assert seen["cmd"][3:] == ["stop", "T01"]
    assert seen["timeout"] == 60
    assert seen["refresh"] is True
    assert notes and notes[-1][1].get("severity") == "error"


async def test_run_hub_cmd_merge_timeout_and_ok(tmp_path, monkeypatch):
    """merge ждёт дольше минуты; успех — без severity='error'."""
    app = _app(tmp_path)
    seen: dict = {}
    notes: list = []

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="смержено T01", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(app, "notify",
                        lambda msg, *a, **k: notes.append((msg, k)))
    monkeypatch.setattr(app, "_schedule_refresh", lambda: None)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app._run_hub_cmd(["merge", "T01"])
        await pilot.pause()
    assert seen["cmd"][3:] == ["merge", "T01"]
    assert seen["timeout"] == 600
    assert notes and notes[-1][1].get("severity") is None


# --- hub top в не-TTY ---

def test_top_nontty_exit3(monkeypatch, capsys):
    from hub.cli import main

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert main(["top"]) == 3
    out = capsys.readouterr()
    assert "нужен терминал" in out.out + out.err


# --- нет прямого чтения чужих БД ---

def test_no_direct_db_read():
    base = Path(__file__).resolve().parent.parent / "hub" / "tui"
    for name in ("__init__.py", "app.py", "widgets.py"):
        text = (base / name).read_text(encoding="utf-8")
        assert "sqlite3" not in text
        assert "mode=ro" not in text
