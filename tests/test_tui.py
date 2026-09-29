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
from hub.tui.widgets import COL_KEYS, EventFeed, TaskTable

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
    app = HubApp(store=Store(), opencode_db=None, proc_root=str(proc))
    # Фоновый опрос выключен: тесты сами накладывают снимок, иначе тик
    # по пустому store стирает подставленные строки таблицы.
    app._schedule_refresh = lambda: None
    return app


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
        assert any("Проверка кода" in c for c in after)


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
        assert "T01" in text and "Сейчас:" in text and "Пишет код" in text


async def test_details_panel_scrolls_to_findings(tmp_path):
    """Хвост подробностей (замечания последнего круга) достижим прокруткой и виден."""
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    wt = tmp_path / "wt-T01"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "review_r1_muse.json").write_text(json.dumps({"findings": [
        {"file": "hub/x.py", "line": i, "issue": f"старое замечание {i}", "severity": "low"}
        for i in range(3)]}), encoding="utf-8")
    (wt / ".agent" / "review_r2_mimoflash.json").write_text(json.dumps({"findings": [
        {"file": "hub/y.py", "line": i, "issue": f"замечание круга 2 номер {i}",
         "severity": "medium"} for i in range(6)]}), encoding="utf-8")
    app = _app(tmp_path)
    app._store().upsert_task(id="T01", project="P", stage="review r2", round=2,
                             worktree=str(wt))
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
        text = str(inner.render())
        # Только последний круг: старые замечания r1 не показываются.
        assert "Замечания проверки (круг 2)" in text
        assert "замечание круга 2 номер 5" in text and "старое замечание" not in text
        assert "MiMo Flash" in text
        vs = app.query_one("#details", VerticalScroll)
        nlines = len(text.splitlines())
        assert vs.max_scroll_y > 0 or vs.size.height >= nlines
        vs.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()
        import html

        shot = html.unescape(app.export_screenshot()).replace("\xa0", " ")
        assert "номер 5" in shot  # хвост на экране


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


def test_details_content_plain_words():
    """Подробности словами: этап, путь, кто работает, деньги по моделям — без служебных id."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        proc = Path(td) / "proc"
        proc.mkdir()
        app = HubApp(store=Store(path=str(Path(td) / "hub.db")),
                     opencode_db=None, proc_root=str(proc))
        base = _snap(last="bash: pytest -q tests/")
        t = replace(base.tasks[0], title="Hub: повтор при сбое сети", goal="Чтобы не падало.",
                    executor="muse", reviewers=["muse", "mimoflash"], max_rounds=2,
                    stage_since_ms=NOW - 12 * 60_000)
        s1 = replace(t.sessions[0], pulse_ms=NOW - 30_000)
        app._snap = replace(base, tasks=[replace(t, sessions=[s1])])
        text = app._build_detail_text("T01")
        assert "T01 · Hub: повтор при сбое сети" in text
        assert "Зачем: Чтобы не падало." in text
        assert "Сейчас: Пишет код (круг 1 из 2) · 12 мин в этапе" in text
        assert "Путь: ✓ очередь → ● код → ○ тесты" in text
        assert "Команда: Spark Go → проверка: Spark Go, MiMo Flash" in text
        assert "Spark Go — пишет код: запускает тесты (только что)" in text
        assert "Потрачено: $0.10 — Spark Go $0.10 (1 запуск)" in text
        for junk in ("executor:", "exec r1", "нет источника", "ctx "):
            assert junk not in text
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


def test_header_counts_money_and_bot(tmp_path):
    app = _app(tmp_path)
    snap = Snapshot(tasks=[
        _snap(task_id="T1", stage="exec r1").tasks[0],
        _snap(task_id="T2", stage="queued").tasks[0],
        _snap(task_id="T3", stage="merged").tasks[0],
        _snap(task_id="T4", stage="arbiter").tasks[0],
    ], total_go=0.2, total_usd=0.0, now_ms=NOW, all_go=0.5, month_go=12.0)
    text = app._header_text(snap, bot_alive=False).plain
    # Слитая не считается; очередь и «ждут Claude» — отдельно от работающих.
    assert "работают 1 · в очереди 1 · ждут Claude 1" in text
    # Нет процесса бота — «не вижу», а не «упал».
    assert "бот TG: не вижу" in text
    assert "месяц Go $12.00 из $60" in text and "сегодня задачи $0.20" in text
    assert "бот TG: работает" in app._header_text(snap, bot_alive=True).plain


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


def test_bot_seen_when_started_by_interpreter(tmp_path):
    """«python3 .venv/bin/hub bot» (так бот запущен вживую) — тоже бот."""
    from hub.read import procs as hub_procs

    root = tmp_path / "proc-bot-py"
    _fake_proc(root, "515151", b"/x/.venv/bin/python3\x00.venv/bin/hub\x00bot\x00")
    _fake_proc(root, "515152", b"/x/.venv/bin/python3\x00.venv/bin/hub\x00status\x00")
    live = hub_procs.agent_procs(str(root))
    assert [(p.pid, p.kind) for p in live] == [(515151, "hub_bot")]


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


def test_first_fetch_shows_recent_history(tmp_path):
    """Первый тик: последние FEED_HISTORY событий (видно, что было до запуска), дальше — новые."""
    from hub.tui.app import FEED_HISTORY

    store = Store()
    ids = [store.add_event("T01", "stage", {"stage": "queued", "n": n})
           for n in range(FEED_HISTORY + 5)]
    proc = tmp_path / "proc-пусто"
    proc.mkdir(exist_ok=True)
    app = HubApp(store=store, opencode_db=None, proc_root=str(proc))
    first = app._fetch_events()
    assert [e["id"] for e in first] == ids[-FEED_HISTORY:]
    assert app._last_event_id == ids[-1]
    new = store.add_event("T01", "stage", {"stage": "exec r1"})
    assert [e["id"] for e in app._fetch_events()] == [new]


async def test_feed_lines_are_plain_words(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        t = replace(_snap().tasks[0], executor="muse", reviewers=["muse", "mimoflash"])
        feed = app.query_one("#events", EventFeed)
        feed.push([
            {"id": 1, "ts": NOW, "task_id": "T01", "kind": "stage",
             "payload_json": json.dumps({"stage": "exec r1", "round": 1})},
            {"id": 2, "ts": NOW, "task_id": "T01", "kind": "stage",
             "payload_json": json.dumps({"stage": "review r1", "round": 1})},
            {"id": 3, "ts": NOW, "task_id": "T01", "kind": "ready",
             "payload_json": json.dumps({"stage": "ready"})},
        ], {"T01": t})
        await pilot.pause()
        text = "\n".join(line.text for line in feed.lines)
        assert "Spark Go пишет код" in text
        assert "проверка кода: Spark Go, MiMo Flash (круг 1)" in text
        # Дубль смены этапа для TG-бота в ленте не повторяется.
        assert len(feed.lines) == 2


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


# --- замечания ревью r2: каждый пункт закрыт тестом ---

async def test_enter_spawns_single_details_worker(tmp_path, monkeypatch):
    """Enter даёт ровно один воркер деталей — и с фокусом таблицы, и без."""
    app = _app(tmp_path)
    spawns: list[str] = []
    orig = HubApp.run_worker

    def spy(self, *args, **kwargs):
        if kwargs.get("group") == "cmd":
            spawns.append("cmd")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(HubApp, "run_worker", spy)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._apply_snapshot(_snap(), bot_alive=False, events=[])
        await pilot.pause()
        app.query_one("#tasks", TaskTable).focus()
        await pilot.pause()
        spawns.clear()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app._details_task:
                break
        assert len(spawns) == 1  # два пути (RowSelected + BINDINGS) не срабатывают вдвойне
        assert app._details_task == "T01"
        # Таблица не в фокусе: Enter идёт через BINDINGS приложения — тоже один воркер.
        app.set_focus(None)
        app._details_task = None
        await pilot.pause()
        spawns.clear()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            if app._details_task:
                break
        assert len(spawns) == 1
        assert app._details_task == "T01"


def test_no_dead_blocking_details_helpers():
    """Мёртвых _render_details/cell_selected нет: блокирующий I/O на лупе — ловушка."""
    assert not hasattr(HubApp, "_render_details")
    assert not hasattr(HubApp, "on_data_table_cell_selected")
    # Прод-путь деталей — только через воркер (to_thread), не из UI-потока.
    import inspect
    from hub.tui import app as app_mod

    src = inspect.getsource(app_mod.HubApp._do_show_details)
    assert "to_thread" in src


def test_top_resolves_opencode_db_at_call_time(monkeypatch, tmp_path):
    """Константа импорта DEFAULT_OPENCODB убрана: путь берётся в cmd_top при вызове."""
    import hub.commands.top as top_mod

    assert not hasattr(top_mod, "DEFAULT_OPENCODB")
    home = tmp_path / "home-после-импорта"
    db = home / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    monkeypatch.setenv("HOME", str(home))
    captured: dict = {}

    class FakeApp:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.project_filter = None

        def run(self):
            return None

    monkeypatch.setattr("hub.tui.app.HubApp", FakeApp)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    from hub.cli import main

    assert main(["top"]) == 0
    assert captured.get("opencode_db") == str(db)


def test_rich_is_only_transitive_dependency():
    """rich не объявлен в pyproject — транзитивная зависимость textual (новых нет)."""
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        encoding="utf-8")
    deps = [ln for ln in text.splitlines() if ln.strip().startswith("dependencies")]
    assert deps and "rich" not in deps[0]
    from rich.text import Text  # работает, пока textual тянет rich

    assert Text("x").plain == "x"


async def test_keys_stop_merge_and_findings(tmp_path, monkeypatch):
    """Клавиши: s → подтверждение → hub stop, m → подтверждение → hub merge, f → findings."""
    import hub.tui.app as app_mod

    from hub.tui.app import ConfirmAction, ConfirmMerge

    app = _app(tmp_path)
    cmds: list[list[str]] = []

    async def fake_cmd(self, argv):
        cmds.append(list(argv))

    monkeypatch.setattr(HubApp, "_run_hub_cmd", fake_cmd)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._apply_snapshot(_snap(), bot_alive=False, events=[])
        await pilot.pause()
        app.query_one("#tasks", TaskTable).focus()
        await pilot.pause()

        # s — тоже с подтверждением: случайное нажатие задачу не останавливает.
        await pilot.press("s")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, ConfirmAction):
                break
        assert isinstance(app.screen, ConfirmAction) and cmds == []
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause()
            if cmds:
                break
        assert cmds == [["stop", "T01"]]

        # m — модальное подтверждение, без «y» merge не уходит.
        await pilot.press("m")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, ConfirmMerge):
                break
        assert isinstance(app.screen, ConfirmMerge)
        assert cmds == [["stop", "T01"]]
        await pilot.press("n")
        for _ in range(50):
            await pilot.pause()
            if not isinstance(app.screen, ConfirmMerge):
                break
        assert cmds == [["stop", "T01"]]

        await pilot.press("m")
        for _ in range(50):
            await pilot.pause()
            if isinstance(app.screen, ConfirmMerge):
                break
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause()
            if cmds[-1] == ["merge", "T01"]:
                break
        assert cmds[-1] == ["merge", "T01"]

        # f: H04 (load_findings) не применён — путь через подпроцесс hub findings.
        monkeypatch.setattr(app_mod, "load_findings", None)
        cmds.clear()
        await pilot.press("f")
        for _ in range(50):
            await pilot.pause()
            if cmds:
                break
        assert cmds == [["findings", "T01"]]

        # f при наличии H04 — детали импортом load_findings, без подпроцесса.
        monkeypatch.setattr(app_mod, "load_findings", lambda wt, round=None: [])
        cmds.clear()
        app._details_task = None
        await pilot.press("f")
        for _ in range(50):
            await pilot.pause()
            if app._details_task:
                break
        assert cmds == []
        assert app._details_task == "T01"


async def test_run_hub_cmd_error_shows_stderr(tmp_path, monkeypatch):
    """При ошибке показывается stderr (причина), а не stdout."""
    app = _app(tmp_path)
    notes: list = []

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=2, stdout="полускачанный вывод",
                               stderr="ошибка: нет такой задачи")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(app, "notify", lambda msg, *a, **k: notes.append((msg, k)))
    monkeypatch.setattr(app, "_schedule_refresh", lambda: None)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await app._run_hub_cmd(["stop", "T01"])
        await pilot.pause()
    assert notes and notes[-1][0] == "ошибка: нет такой задачи"
    assert notes[-1][1].get("severity") == "error"
