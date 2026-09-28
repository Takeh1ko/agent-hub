"""Snapshot: лимит 1500 байт, пульсы 🔴/🟡/🟢/⚫, итоги $, roster."""

from __future__ import annotations

import json
import sqlite3

from hub.read import snapshot as snap
from hub.store import Store

NOW = 1_789_000_000_000

SCHEMA = """
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


def _mkdb(path, rows: list[dict]) -> str:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for i, r in enumerate(rows):
        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], "p", r.get("dir", "/wt/" + r["id"]), r.get("title", "t"),
             json.dumps({"id": r.get("model", "muse"), "providerID": r.get("prov", "opencode-go")}),
             r.get("cost", 0.1), 0, 0, 0, 0,
             r.get("pulse", NOW), r.get("pulse", NOW)))
        if r.get("tool"):
            con.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"p{i}", f"m{i}", r["id"], NOW, r.get("pulse", NOW), json.dumps({
                    "type": "tool", "tool": r["tool"],
                    "state": {"status": "running", "input": r.get("input", {})}})))
    con.commit()
    con.close()
    return str(path)


def _proc(root, pid, argv, cwd, ppid=1):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes("\x00".join(argv).encode() + b"\x00")
    (d / "cwd").symlink_to(cwd)
    (d / "stat").write_text(f"{pid} (x) R {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
                            encoding="utf-8")


def test_status_limit_20_tasks(tmp_path):
    s = Store()
    for i in range(20):
        s.upsert_task(id=f"T{i:02d}-задача-длинное-название", stage="exec r1", round=1,
                      worktree=str(tmp_path / f"wt{i}"),
                      updated_at=NOW - i * 1000)
        s.link_session(f"ses{i}", "opencode", f"T{i:02d}-задача-длинное-название",
                       "executor", 1, "muse")
    db = _mkdb(tmp_path / "oc.db", [
        {"id": f"ses{i}", "pulse": NOW - 30_000, "cost": 0.05} for i in range(20)])
    snap_ = snap.build(s, NOW, opencode_db=db, proc_root=tmp_path / "пустой-proc")
    text = snap_.to_text()
    assert len(text.encode("utf-8")) <= 1500
    data = json.loads(snap_.to_json())
    assert len(data["tasks"]) == 20
    # Итоги $ за сегодня: 20 × 0.05 go.
    assert abs(data["total_go"] - 1.0) < 1e-9 and data["total_usd"] == 0.0


def test_red_stuck_exec_21min(tmp_path):
    s = Store()
    wt = tmp_path / "wt-red"
    wt.mkdir()
    s.upsert_task(id="T01", stage="exec r1", round=1, worktree=str(wt), updated_at=NOW - 21 * 60_000)
    s.link_session("ses1", "opencode", "T01", "executor", 1, "muse")
    db = _mkdb(tmp_path / "oc.db", [{"id": "ses1", "pulse": NOW - 21 * 60_000}])
    got = snap.build(s, NOW, opencode_db=db, proc_root=tmp_path / "пустой")
    assert got.tasks[0].pulse == "🔴"


def test_yellow_pytest_child(tmp_path):
    s = Store()
    wt = tmp_path / "wt-y"
    wt.mkdir()
    s.upsert_task(id="T02", stage="exec r1", round=1, worktree=str(wt), updated_at=NOW - 600_000)
    s.link_session("ses2", "opencode", "T02", "executor", 1, "muse")
    db = _mkdb(tmp_path / "oc.db", [{"id": "ses2", "pulse": NOW - 600_000}])
    root = tmp_path / "proc"
    _proc(root, 10, ["opencode", "run"], str(wt))
    _proc(root, 11, ["flock", "/tmp/x.lock", "pytest"], str(wt), ppid=10)
    got = snap.build(s, NOW, opencode_db=db, proc_root=root)
    assert got.tasks[0].pulse == "🟡"


def test_green_and_black(tmp_path):
    s = Store()
    s.upsert_task(id="TG", stage="exec r1", worktree=str(tmp_path / "wt-g"),
                  updated_at=NOW - 30_000)
    s.link_session("sesg", "opencode", "TG", "executor", 1, "muse")
    s.upsert_task(id="TB", stage="exec r1", worktree=str(tmp_path / "wt-b"),
                  updated_at=NOW - 30_000)
    (tmp_path / "wt-g").mkdir()
    (tmp_path / "wt-b").mkdir()
    db = _mkdb(tmp_path / "oc.db", [{"id": "sesg", "pulse": NOW - 30_000}])
    root = tmp_path / "proc"
    _proc(root, 10, ["opencode", "run"], str(tmp_path / "wt-g"))
    got = {t.id: t for t in snap.build(s, NOW, opencode_db=db, proc_root=root).tasks}
    assert got["TG"].pulse == "🟢"
    assert got["TB"].pulse == "⚫"  # процесса нет, этап не финальный


def test_roster_format(tmp_path):
    s = Store()
    s.upsert_task(id="T01", stage="exec r1", worktree=str(tmp_path / "w"), updated_at=NOW)
    s.link_session("s1", "opencode", "T01", "executor", 1, "muse")
    (tmp_path / "w").mkdir()
    db = _mkdb(tmp_path / "oc.db", [{"id": "s1", "pulse": NOW, "model": "muse"}])
    text = snap.build(s, NOW, opencode_db=db, proc_root=tmp_path / "пустой").roster_text()
    assert "muse" in text and "executor" in text and "T01" in text and "exec r1" in text
