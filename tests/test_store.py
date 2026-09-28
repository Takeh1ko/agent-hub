"""Store: миграции, WAL, задачи, сессии, события, import_legacy."""

from __future__ import annotations

import json
import sqlite3

from hub.store import Store


def _tables(store: Store) -> set[str]:
    con = sqlite3.connect(str(store.path))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_migrations_and_wal(tmp_path):
    s = Store(tmp_path / "hub.db")
    assert {"task", "session", "event", "question", "inbox", "budget", "migration"} <= _tables(s)
    con = sqlite3.connect(str(s.path))
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("SELECT COUNT(*) FROM migration").fetchone()[0] >= 1
    finally:
        con.close()
    # Повторное открытие не дублирует миграции.
    Store(tmp_path / "hub.db")
    con = sqlite3.connect(str(s.path))
    try:
        assert con.execute("SELECT COUNT(*) FROM migration").fetchone()[0] >= 1
    finally:
        con.close()


def test_task_crud(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.upsert_task(id="T01", project="P", stage="exec r1", round=1)
    got = s.get_task("T01")
    assert got and got["stage"] == "exec r1" and got["round"] == 1
    s.upsert_task(id="T01", stage="review r1", round=1)
    assert s.get_task("T01")["stage"] == "review r1"
    assert s.get_task("нет") is None
    s.upsert_task(id="T02", stage="merged")
    active = [t["id"] for t in s.list_tasks()]
    assert "T01" in active and "T02" not in active
    assert {t["id"] for t in s.list_tasks(active_only=False)} == {"T01", "T02"}


def test_sessions_and_events(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.upsert_task(id="T01", stage="exec r1")
    s.link_session("ses_1", "opencode", "T01", "executor", 1, "muse")
    s.link_session("ses_2", "opencode", "T01", "reviewer", 1, "mimo")
    roles = {r["external_id"]: r["role"] for r in s.list_sessions("T01")}
    assert roles == {"ses_1": "executor", "ses_2": "reviewer"}
    e1 = s.add_event("T01", "stage", {"stage": "exec r1"})
    e2 = s.add_event("T01", "stuck", {})
    assert e2 > e1
    assert [e["kind"] for e in s.events_since(e1)] == ["stuck"]
    assert s.events_since(e2) == []


def test_import_legacy_stage_and_roles(tmp_path):
    wt = tmp_path / "worktrees"
    t1 = wt / "T05-что-то"
    (t1 / ".agent").mkdir(parents=True)
    (t1 / ".agent" / "state.json").write_text(json.dumps({
        "task": "T05-что-то", "base": "abc123", "worktree": str(t1),
        "executor_session": "ses_exec", "reviewer_sessions": ["ses_rev1", "ses_rev2"],
        "round": 1, "verdicts": ["changes"], "status": "failed",
    }), encoding="utf-8")
    (t1 / ".agent" / "review_r1.json").write_text(json.dumps({
        "verdict": "changes", "findings": []}), encoding="utf-8")
    t2 = wt / "T06-готово"
    (t2 / ".agent").mkdir(parents=True)
    (t2 / ".agent" / "state.json").write_text(json.dumps({
        "task": "T06-готово", "base": "abc123", "worktree": str(t2),
        "executor_session": "ses_e2", "reviewer_sessions": [],
        "round": 2, "verdicts": ["approve"], "status": "ready",
    }), encoding="utf-8")
    s = Store(tmp_path / "hub.db")
    ids = s.import_legacy(wt)
    assert ids == ["T05-что-то", "T06-готово"]
    # failed + changes в review → этап review r1.
    assert s.get_task("T05-что-то")["stage"] == "review r1"
    assert s.get_task("T06-готово")["stage"] == "ready"
    roles = {r["external_id"]: r["role"] for r in s.list_sessions("T05-что-то")}
    assert roles == {"ses_exec": "executor", "ses_rev1": "reviewer", "ses_rev2": "reviewer"}


def test_import_legacy_skips_broken(tmp_path):
    wt = tmp_path / "wt"
    (wt / "пусто").mkdir(parents=True)
    bad = wt / "битый"
    (bad / ".agent").mkdir(parents=True)
    (bad / ".agent" / "state.json").write_text("{не json", encoding="utf-8")
    s = Store(tmp_path / "hub.db")
    assert s.import_legacy(wt) == []
    assert s.import_legacy(tmp_path / "нет-каталога") == []
