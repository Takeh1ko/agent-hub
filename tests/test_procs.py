"""procs through psutil (no /proc): children, liveness, cmdline, start time on live processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from ahub import procs, pulse, service

NOPROC = "/nonexistent"  # no such dir — the psutil branch


def _sleep(args: list[str] | None = None) -> subprocess.Popen:
    code = "import time; time.sleep(30)"
    cmd = [sys.executable, "-c", code, *(args or [])]
    return subprocess.Popen(cmd)


def test_psutil_children_alive_cmdline_start_time():
    s = _sleep()
    try:
        time.sleep(0.5)
        assert procs.alive(s.pid, NOPROC)
        args = procs.cmdline(s.pid, NOPROC)
        assert any("time.sleep" in a for a in args)
        st = procs.start_time(s.pid, NOPROC)
        assert isinstance(st, int)
        assert procs.start_time(s.pid, NOPROC) == st  # equal to itself
        assert s.pid in procs.children(os.getpid(), NOPROC)
        assert s.pid in procs.descendants(os.getpid(), NOPROC)
        assert not procs.has_children(s.pid, NOPROC)  # the sleeper has no children
        assert s.pid in procs.pids(NOPROC)
        assert os.getpid() in procs.pids(NOPROC)
        assert not procs.alive(999999, NOPROC)
        assert procs.cmdline(999999, NOPROC) == []
        assert procs.start_time(999999, NOPROC) is None
    finally:
        s.kill()
        s.wait()


def test_psutil_zombie_not_alive():
    z = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        time.sleep(0.5)
        # not reaped yet — a zombie (the /proc entry is there, psutil has no state)
        assert not procs.alive(z.pid, NOPROC)
        assert not procs.alive(z.pid)  # and via /proc it is not alive either
    finally:
        z.wait()
    assert not procs.alive(z.pid, NOPROC)


def test_psutil_no_data_without_psutil(monkeypatch):
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "psutil", None)
    assert procs.pids(NOPROC) == []
    assert procs.children(os.getpid(), NOPROC) == []
    assert procs.descendants(os.getpid(), NOPROC) == []
    assert not procs.has_children(os.getpid(), NOPROC)
    assert not procs.alive(os.getpid(), NOPROC)
    assert procs.cmdline(os.getpid(), NOPROC) == []
    assert procs.start_time(os.getpid(), NOPROC) is None


def test_psutil_live_workers_finds_marked():
    tid = 987654
    s = _sleep(["ahub.worker", f"T{tid}"])
    try:
        time.sleep(0.5)
        live = service.live_workers(NOPROC)
        assert live.get(tid) == s.pid
    finally:
        s.kill()
        s.wait()
    assert service.live_workers(NOPROC).get(tid) is None


def test_psutil_lock_wait_without_holder(tmp_path):
    import fcntl

    from ahub import transitions
    from ahub.model import State
    from ahub.store import Store

    assert pulse.lock_holders(NOPROC) == {}
    lk = tmp_path / "db.lock"
    lk.write_text("")
    fd = os.open(lk, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert pulse.lock_holder(str(lk), NOPROC) is None  # the lock is busy, but without /proc there is no holder
        from ahub import config

        proj = config.parse_project({"schema_version": 2, "name": "P",
                                     "resources": {"db": {"lock": str(lk)}}, "test_resource": "db"}, tmp_path)
        store = Store()
        tid = store.create_task(project="P", kind="code", title="x", now=0)
        transitions.move(store, tid, State.PREPARING, now=0)
        transitions.move(store, tid, State.WORKING, now=0)
        store.update_task(tid, phase="waiting", state_reason="ждёт места", now=0)
        t = store.get_task(tid)
        p = pulse.task_pulse(store, t, live={tid: os.getpid()}, project=proj,
                             now=60 * 60_000, proc_root=NOPROC)
        assert p.state == "waiting" and "держит" not in p.reason  # honest — no holder name
    finally:
        os.close(fd)
