"""Очередь H10: свой проект, слоты по мере освобождения, отдельный процесс, status."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import hub.commands.queue as q
from hub.config import ProjectConfig, Defaults
from hub.store import Store


def _proj(tmp_path, name="P", lock=""):
    wt = tmp_path / f"wt-{name}"
    wt.mkdir(parents=True, exist_ok=True)
    return ProjectConfig(
        name=name, root=str(tmp_path), worktrees=str(wt),
        rules="", python=sys.executable, test_lock=lock,
        work_branch="main", push="", allowed_paths=[],
        defaults=Defaults(executor="muse", reviewers=["muse"]),
    )


def _seed(tid, project="", created=1000, stage="queued", worktree=""):
    Store().upsert_task(id=tid, project=project, card_path="", card_hash="h",
                        level="hard", branch=f"agent/{tid}", worktree=worktree,
                        base_sha="b", stage=stage, round=0, executor="muse",
                        reviewers_json="[]", stage_reason="",
                        budget_go=0.5, budget_usd=0.0, created_at=created)


class _Done:
    pid = 9991

    def poll(self):
        return 0


class _BlockingProc:
    _pid_seq = 5000

    def __init__(self):
        type(self)._pid_seq += 1
        self.pid = type(self)._pid_seq
        self._ev = threading.Event()
        self.returncode = None

    def finish(self):
        self._ev.set()

    def poll(self):
        return 0 if self._ev.is_set() else None


def test_only_own_project(tmp_path):
    proj = _proj(tmp_path, "P")
    _seed("OWN", project="P", created=1000)
    _seed("ALIEN", project="OTHER", created=1001)
    started: list[str] = []

    def _spawn(tid):
        started.append(tid)
        Store().upsert_task(id=tid, stage="ready")
        return _Done()

    code = q._pump(Store(), proj, None, 2, spawn_fn=_spawn,
                   poll_secs=0.01, proc_root=str(tmp_path / "nproc"))
    assert code == 0
    assert started == ["OWN"]
    assert Store().get_task("ALIEN")["stage"] == "queued"


def test_empty_project_by_worktree(tmp_path):
    proj = _proj(tmp_path, "P")
    wt_root = proj.worktrees
    inside = str(Path(wt_root) / "T1")
    _seed("IN", project="", worktree=inside, created=1000)
    _seed("OUT", project="", worktree="/tmp/где-то-снаружи/T2", created=1001)
    started: list[str] = []

    def _spawn(tid):
        started.append(tid)
        Store().upsert_task(id=tid, stage="ready")
        return _Done()

    q._pump(Store(), proj, None, 2, spawn_fn=_spawn,
            poll_secs=0.01, proc_root=str(tmp_path / "nproc"))
    assert started == ["IN"]
    assert Store().get_task("OUT")["stage"] == "queued"


def test_slots_reused_as_freed(tmp_path):
    proj = _proj(tmp_path, "P")
    for i, tid in enumerate(("A", "B", "C")):
        _seed(tid, project="P", created=1000 + i)
    started: list[str] = []
    procs: dict[str, _BlockingProc] = {}
    lock = threading.Lock()

    def _spawn(tid):
        with lock:
            started.append(tid)
            Store().upsert_task(id=tid, stage="exec r1")
            p = _BlockingProc()
            procs[tid] = p
            return p

    res: dict = {}

    def _run():
        res["code"] = q._pump(Store(), proj, None, 2, spawn_fn=_spawn,
                              poll_secs=0.01, proc_root=str(tmp_path / "nproc"))

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        # Ждём два занятых места.
        t0 = time.time()
        while True:
            with lock:
                n = len(started)
            if n >= 2 or time.time() - t0 > 5:
                break
            time.sleep(0.01)
        with lock:
            assert sorted(started) == ["A", "B"], started
        # Третья не стартует, пока места заняты.
        time.sleep(0.05)
        with lock:
            assert started == ["A", "B"] or sorted(started) == ["A", "B"]
            assert "C" not in started
        # Освобождаем одно место — третья стартует, не дожидаясь всей пачки.
        Store().upsert_task(id="A", stage="ready")
        with lock:
            procs["A"].finish()
        t0 = time.time()
        while True:
            with lock:
                has_c = "C" in started
            if has_c or time.time() - t0 > 5:
                break
            time.sleep(0.01)
        assert has_c, started
        # В этот момент B ещё в работе (слотная модель, а не пачка).
        with lock:
            assert not procs["B"]._ev.is_set()
            Store().upsert_task(id="B", stage="ready")
            Store().upsert_task(id="C", stage="ready")
            procs["B"].finish()
            procs["C"].finish()
    finally:
        with lock:
            for tid in list(started):
                try:
                    Store().upsert_task(id=tid, stage="ready")
                except (OSError, ValueError):
                    pass
            for p in procs.values():
                p.finish()
        th.join(timeout=5)
    assert not th.is_alive()
    assert res.get("code") == 0


def test_playerok_single(tmp_path):
    proj = _proj(tmp_path, "P")
    c1 = tmp_path / "c1.md"
    c1.write_text("# T\n**Цель.** ц\n**Сеть.** playerok\n", encoding="utf-8")
    c2 = tmp_path / "c2.md"
    c2.write_text("# T\n**Цель.** ц\n**Сеть.** playerok\n", encoding="utf-8")
    Store().upsert_task(id="P1", project="P", card_path=str(c1), stage="queued",
                        created_at=1000)
    Store().upsert_task(id="P2", project="P", card_path=str(c2), stage="queued",
                        created_at=1001)
    started: list[str] = []
    procs: dict[str, _BlockingProc] = {}
    lock = threading.Lock()

    def _spawn(tid):
        with lock:
            started.append(tid)
            Store().upsert_task(id=tid, stage="exec r1")
            p = _BlockingProc()
            procs[tid] = p
            return p

    res: dict = {}

    def _run():
        res["code"] = q._pump(Store(), proj, None, 2, spawn_fn=_spawn,
                              poll_secs=0.01, proc_root=str(tmp_path / "nproc"))

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        t0 = time.time()
        while True:
            with lock:
                n = len(started)
            if n >= 1 or time.time() - t0 > 5:
                break
            time.sleep(0.01)
        time.sleep(0.05)
        with lock:
            assert len(started) == 1, started
            first = started[0]
            Store().upsert_task(id=first, stage="ready")
            procs[first].finish()
        t0 = time.time()
        while True:
            with lock:
                n = len(started)
            if n >= 2 or time.time() - t0 > 5:
                break
            time.sleep(0.01)
        with lock:
            assert len(started) == 2
            for tid in list(procs):
                Store().upsert_task(id=tid, stage="ready")
            for p in procs.values():
                p.finish()
    finally:
        with lock:
            for tid in list(procs):
                try:
                    Store().upsert_task(id=tid, stage="ready")
                except (OSError, ValueError):
                    pass
            for p in procs.values():
                p.finish()
        th.join(timeout=5)
    assert not th.is_alive()


def test_spawn_uses_own_group(tmp_path, monkeypatch):
    seen: dict = {}

    class _P:
        pid = 4242

        def poll(self):
            return 0

    def _fake(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return _P()

    monkeypatch.setattr(subprocess, "Popen", _fake)
    q._spawn_one("TX", "/proj")
    assert "--run-one" in seen["cmd"]
    assert "TX" in seen["cmd"]
    assert seen["kw"].get("start_new_session") is True


def test_run_one_calls_cycle(tmp_path, monkeypatch):
    _seed("T1", project="P", stage="queued")
    calls: dict = {}

    def _fake_run(store, project, tid, runners, rounds=2, blind=False):
        calls["tid"] = tid
        return "ready"

    monkeypatch.setattr("hub.pipeline.cycle.run_task", _fake_run)
    monkeypatch.setattr("hub.pipeline.runners.make_runner",
                        lambda name: object())
    monkeypatch.setattr(q, "_resolve_project",
                        lambda task, src: _proj(tmp_path, "P"))
    assert q._run_one("T1", None) == "ready"
    assert calls.get("tid") == "T1"


def test_queue_run_one_cli_calls_run_one(monkeypatch):
    calls: list = []
    monkeypatch.setattr(q, "_run_one", lambda tid, src: calls.append((tid, src)) or "ready")
    ns = type("A", (), {"run_one": "TZ", "project": "/p"})()
    assert q.cmd_queue_run(ns) == 0
    assert calls == [("TZ", "/p")]


def test_child_main_calls_run_one(monkeypatch):
    calls: list = []
    monkeypatch.setattr(q, "_run_one", lambda tid, src: calls.append(tid) or "ready")
    assert q._child_main(["--run-one", "TC"]) == 0
    assert calls == ["TC"]


def test_belongs_to_project_unit(tmp_path):
    proj = _proj(tmp_path, "P")
    assert q._belongs_to_project({"project": "P", "worktree": ""}, proj)
    assert not q._belongs_to_project({"project": "OTHER", "worktree": ""}, proj)
    inside = str(Path(proj.worktrees) / "T1")
    assert q._belongs_to_project({"project": "", "worktree": inside}, proj)
    assert not q._belongs_to_project({"project": "", "worktree": "/чужое/T2"}, proj)
    assert not q._belongs_to_project({"project": "", "worktree": ""}, proj)


def test_pump_filters_even_without_store_method(tmp_path, monkeypatch):
    """Фильтр — в воркере, а не только в store: чужой не берём, даже если выборка всё отдала."""
    proj = _proj(tmp_path, "P")
    _seed("OWN2", project="P", created=1000)
    _seed("ALIEN2", project="OTHER", created=1001)
    monkeypatch.setattr(Store, "list_queued_for_project",
                        lambda self, name, wt="": self.list_queued())
    started: list[str] = []

    def _spawn(tid):
        started.append(tid)
        Store().upsert_task(id=tid, stage="ready")
        return _Done()

    code = q._pump(Store(), proj, None, 2, spawn_fn=_spawn,
                   poll_secs=0.01, proc_root=str(tmp_path / "nproc"))
    assert code == 0
    assert started == ["OWN2"]
    assert Store().get_task("ALIEN2")["stage"] == "queued"


def test_restart_keeps_live_task(tmp_path, monkeypatch):
    proj = _proj(tmp_path, "P")
    _seed("LIVE", project="P", stage="exec r1", created=1000)
    _seed("Q", project="P", stage="queued", created=1001)
    # Фейковый /proc: живой --run-one для LIVE.
    proot = tmp_path / "proc"
    d = proot / "1234"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"python\x00-m\x00hub.commands.queue\x00--run-one\x00LIVE\x00")
    started: list[str] = []

    def _spawn(tid):
        started.append(tid)
        Store().upsert_task(id=tid, stage="ready")
        return _Done()

    code = q._pump(Store(), proj, None, 2, spawn_fn=_spawn,
                   poll_secs=0.01, proc_root=str(proot))
    assert Store().get_task("LIVE")["stage"] == "exec r1"
    assert started == ["Q"]
    assert code == 0


def test_live_scan_parses_run_one(tmp_path):
    proot = tmp_path / "proc"
    for pid, tid in (("11", "A"), ("22", "B")):
        d = proot / pid
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(
            f"python\x00-m\x00hub.commands.queue\x00--run-one\x00{tid}\x00".encode())
    got = q._live_run_one(str(proot))
    assert got == {"A": 11, "B": 22}


def test_status_json(tmp_path, capsys, monkeypatch):
    proj = _proj(tmp_path, "P")
    _seed("R1", project="P", stage="exec r1", created=1000)
    _seed("Q1", project="P", stage="queued", created=1001)
    _seed("ALIEN", project="OTHER", stage="queued", created=1002)
    monkeypatch.setattr(q, "_live_run_one", lambda root="/proc": {"R1": 777})
    ns = type("A", (), {"project": None, "max_parallel": 4, "json": True})()
    # Без фильтра проекта — все видны; pid/стадия/очередь/свободные места.
    assert q.cmd_queue_status(ns) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["free"] == 3
    assert any(r["task"] == "R1" and r["pid"] == 777 for r in data["running"])
    assert "Q1" in data["queued_ids"]
    # Фильтр своего проекта режет чужую очередь.
    import tempfile

    toml_dir = tmp_path / "proot"
    toml_dir.mkdir(exist_ok=True)
    (toml_dir / ".hub.toml").write_text(
        f'schema_version = 1\nname = "P"\nroot = "{tmp_path}"\n'
        f'worktrees = "{proj.worktrees}"\n', encoding="utf-8")
    ns2 = type("A", (), {"project": str(toml_dir), "max_parallel": 1, "json": True})()
    assert q.cmd_queue_status(ns2) == 0
    data2 = json.loads(capsys.readouterr().out)
    assert "ALIEN" not in data2["queued_ids"]
    assert "Q1" in data2["queued_ids"]


def test_store_queued_for_project(tmp_path):
    s = Store()
    wt = tmp_path / "wt"
    wt.mkdir()
    s.upsert_task(id="A", project="P", stage="queued", created_at=1)
    s.upsert_task(id="B", project="OTHER", stage="queued", created_at=2)
    s.upsert_task(id="C", project="", worktree=str(wt / "C"),
                  stage="queued", created_at=3)
    s.upsert_task(id="D", project="", worktree="/чужое/D",
                  stage="queued", created_at=4)
    got = [t["id"] for t in s.list_queued_for_project("P", str(wt))]
    assert got == ["A", "C"]


def test_once_runs_in_process_no_popen(tmp_path, monkeypatch):
    """--once — синхронно в этом процессе: Popen не вызывается, моки видны."""
    import subprocess as _sp

    proj = _proj(tmp_path, "P")
    _seed("W1", project="P", created=1000)
    seen: list[str] = []

    def _fake_run_one(tid, src):
        seen.append(tid)
        Store().upsert_task(id=tid, stage="ready")
        return "ready"

    monkeypatch.setattr(q, "_run_one", _fake_run_one)

    def _no_popen(*a, **k):
        raise AssertionError("Popen вызван в --once")

    monkeypatch.setattr(_sp, "Popen", _no_popen)
    code = q._pump(Store(), proj, None, 2, poll_secs=0.01,
                   proc_root=str(tmp_path / "nproc"))
    assert code == 0
    assert seen == ["W1"]


def test_daemon_spawns_run_one_process(tmp_path, monkeypatch):
    """Фон — через _spawn_one (--run-one в своей группе)."""
    from hub.pipeline.common import meta_set as _meta_set

    proj = _proj(tmp_path, "P")
    _seed("D1", project="P", created=1000)
    calls: list = []

    class _P:
        pid = 6001

        def poll(self):
            return 0

    def _fake_spawn(tid, src):
        calls.append(tid)
        Store().upsert_task(id=tid, stage="ready")
        _meta_set(Store(), "queue_stop", "1")
        return _P()

    monkeypatch.setattr(q, "_spawn_one", _fake_spawn)
    ns = type("A", (), {"project": None, "max_parallel": 2, "once": False,
                        "poll_secs": 0.01, "run_one": None})()
    # Без --project воркер берёт все queued; D1 — своя.
    assert q.cmd_queue_run(ns) == 0
    assert calls == ["D1"]
