"""H14b: снимок пачкой (сессии ≤10 запросов, /proc один раз, merged без чтений) + ворота от merge-base."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

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


def _mkdb(path: Path, n: int) -> str:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for i in range(n):
        sid = f"s{i:03d}"
        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "p", f"/wt/{sid}", "t",
             json.dumps({"id": "muse", "providerID": "opencode-go"}),
             0.1, 0, 0, 0, 0, NOW, NOW))
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (f"m{i}", sid, NOW, NOW, json.dumps({
                "role": "assistant", "tokens": {"input": 100, "cache": {"read": 200}}})))
        con.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (f"p{i}", f"m{i}", sid, NOW, NOW, json.dumps({"type": "text", "text": "hi"})))
    con.commit()
    con.close()
    return str(path)


def test_sessions_fixed_queries(tmp_path, monkeypatch):
    """300 сессий — число запросов не растёт (≤10), данные те же."""
    from hub.read import opencode as oc

    db = _mkdb(tmp_path / "oc.db", 300)
    calls: list[str] = []
    orig_connect = sqlite3.connect

    def fake_connect(*a, **k):
        c = orig_connect(*a, **k)
        c.set_trace_callback(lambda sql: calls.append(sql))
        return c

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    got = oc.sessions(db, 0)
    assert len(got) == 300
    assert len(calls) <= 10
    one = {s.id: s for s in got}["s007"]
    assert one.context_tokens == 300
    assert one.provider == "opencode-go"


def _proc(root: Path, pid: int, argv: list[str], cwd: str) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes("\x00".join(argv).encode() + b"\x00")
    (d / "cwd").symlink_to(cwd)
    (d / "stat").write_text(
        f"{pid} (x) R 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n",
        encoding="utf-8")


def test_proc_single_scan(tmp_path, monkeypatch):
    """Фейковый /proc читается один раз за build (5 задач — один iterdir)."""
    s = Store()
    for i in range(5):
        wt = tmp_path / f"wt{i}"
        wt.mkdir()
        s.upsert_task(id=f"T{i}", stage="exec r1", worktree=str(wt), updated_at=NOW)
    root = tmp_path / "proc"
    for i in range(5):
        _proc(root, 100 + i, ["opencode", "run"], str(tmp_path / f"wt{i}"))
    iters: list[str] = []
    orig_iter = Path.iterdir

    def fake_iter(self):
        if str(self) == str(root):
            iters.append(str(self))
        return orig_iter(self)

    monkeypatch.setattr(Path, "iterdir", fake_iter)
    snap.build(s, NOW, opencode_db=None, proc_root=root, agy_root=str(tmp_path / "noagy"))
    assert len(iters) == 1


def test_merged_no_session_reads(tmp_path, monkeypatch):
    """Merged-задача без --all не вызывает чтение сессий."""
    from hub.read import opencode as oc

    s = Store()
    s.upsert_task(id="TM", stage="merged", worktree=str(tmp_path / "wt"), updated_at=NOW)
    db = _mkdb(tmp_path / "oc.db", 3)
    hits: list = []
    orig = oc.sessions

    def fake(*a, **k):
        hits.append(1)
        return orig(*a, **k)

    monkeypatch.setattr("hub.read.snapshot.oc.sessions", fake)
    got = snap.build(s, NOW, opencode_db=db, proc_root=tmp_path / "пустой",
                     agy_root=str(tmp_path / "noagy"))
    assert hits == []
    assert [t.id for t in got.tasks] == ["TM"]
    assert got.tasks[0].pulse == "✅"
    # С флагом --all (include_done) и линканутой сессией чтения есть.
    s.link_session("s000", "opencode", "TM", "executor", 1, "muse")
    hits.clear()
    got2 = snap.build(s, NOW, opencode_db=db, proc_root=tmp_path / "пустой",
                      agy_root=str(tmp_path / "noagy"), include_done=True)
    assert hits != []
    assert [t.id for t in got2.tasks] == ["TM"]
    assert len(got2.tasks[0].sessions) == 1


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_gate_merge_base(tmp_path):
    """Ручное слияние рабочей ветки: чужие файлы не forbidden, свой — forbidden."""
    from hub.gate.gate import check_gate

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "market")
    (repo / "work.txt").write_text("w\n", encoding="utf-8")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-m", "work")
    _git(repo, "checkout", "-b", "agent/T1", base)
    (repo / "own.txt").write_text("o\n", encoding="utf-8")
    _git(repo, "add", "own.txt")
    _git(repo, "commit", "-m", "own")
    _git(repo, "merge", "market", "-m", "merge market")
    (repo / "bad.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "bad.txt")
    _git(repo, "commit", "-m", "bad")
    head = _git(repo, "rev-parse", "HEAD")
    passing = [sys.executable, "-c", "pass"]
    # Без рабочей ветки — старый счёт: work.txt forbidden.
    r1 = check_gate(repo, base, head, ["own.txt", "bad.txt"], passing)
    assert not r1.ok and any("work.txt" in e for e in r1.errors)
    # С рабочей веткой: work.txt не forbidden, а свой bad.txt — forbidden.
    r2 = check_gate(repo, base, head, ["own.txt"], passing, work_branch="market")
    assert not r2.ok
    assert not any("work.txt" in e for e in r2.errors)
    assert any("bad.txt" in e for e in r2.errors)
    # Оба своих разрешены — ворота зелёные.
    r3 = check_gate(repo, base, head, ["own.txt", "bad.txt"], passing,
                    work_branch="market")
    assert r3.ok, r3.errors
