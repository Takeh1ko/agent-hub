from __future__ import annotations

import fcntl
import os
import subprocess
import sys

import pytest

from ahub import procs, providers, pulse, transitions
from ahub.model import State
from ahub.providers.base import SessionState
from ahub.providers.fake import FakeProvider
from ahub.store import Store
from tests.conftest import wait_until


@pytest.fixture
def store() -> Store:
    return Store()


needs_locks = pytest.mark.skipif(sys.platform != "linux", reason="держатель замка читается только из /proc/locks")
NOPROC = "/nonexistent"  # no such dir — "no data" about the holder (as on macOS)


class StatefulFake(FakeProvider):
    def __init__(self, st):
        super().__init__()
        self.st = st

    def session_state(self, session_id):
        return self.st


def active(store, now=0, phase=""):
    tid = store.create_task(project="P", kind="code", title="x", now=now)
    transitions.move(store, tid, State.PREPARING, now=now)
    transitions.move(store, tid, State.WORKING, now=now)
    if phase:
        store.update_task(tid, phase=phase, now=now)
    return tid


def test_dead(store):
    tid = active(store)
    p = pulse.task_pulse(store, store.get_task(tid), live={}, now=10)
    assert p.state == "dead" and p.mark == "⚫"


def test_fresh_provider_activity(store):
    tid = active(store, now=0)
    store.add_session(task_id=tid, provider="fake", role="executor", external_id="s1", pid=None)
    providers.register("fake", StatefulFake(SessionState(last_activity_ms=10**7)))
    p = pulse.task_pulse(store, store.get_task(tid), live={tid: 1}, now=10**7 + 1000)
    assert p.state == "working"


def test_tool_explains(store):
    tid = active(store)
    store.add_session(task_id=tid, provider="fake", role="executor", external_id="s1")
    providers.register("fake", StatefulFake(SessionState(last_activity_ms=1, active_tool="bash", tool_started_ms=1)))
    p = pulse.task_pulse(store, store.get_task(tid), live={tid: 1}, now=40 * 60_000)
    assert p.state == "waiting" and "bash" in p.reason


def test_silent_after_threshold(store):
    tid = active(store)
    store.add_session(task_id=tid, provider="fake", role="executor", external_id="s1")
    providers.register("fake", StatefulFake(SessionState(last_activity_ms=1)))
    t = store.get_task(tid)
    assert pulse.task_pulse(store, t, live={tid: 1}, now=10 * 60_000).state == "working"
    p = pulse.task_pulse(store, t, live={tid: 1}, now=25 * 60_000)
    assert p.state == "silent" and "порог 20" in p.reason


def test_unknown_without_signal(store):
    tid = active(store)
    store.add_session(task_id=tid, provider="fake", role="executor")
    providers.register("fake", StatefulFake(None))
    p = pulse.task_pulse(store, store.get_task(tid), live={tid: 1}, now=60 * 60_000)
    assert p.state == "unknown" and p.mark == "⚪"


def test_children_explain(store):
    tid = active(store)
    child = subprocess.Popen([sys.executable, "-c", "import subprocess,sys,time;"
                              "subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)']);time.sleep(5)"])
    try:
        assert wait_until(lambda: procs.has_children(child.pid)), "у процесса не появился ребёнок"
        store.add_session(task_id=tid, provider="fake", role="executor", external_id="s1", pid=child.pid)
        providers.register("fake", StatefulFake(SessionState(last_activity_ms=1)))
        p = pulse.task_pulse(store, store.get_task(tid), live={tid: 1}, now=60 * 60_000)
        assert p.state == "waiting" and "дочерние" in p.reason
    finally:
        child.kill()


@needs_locks
def test_lock_holder(tmp_path):
    lk = tmp_path / "t.lock"
    lk.write_text("")
    holder = subprocess.Popen([sys.executable, "-c", f"import fcntl,time; f=open({str(lk)!r}); "
                               "fcntl.flock(f, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(10)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ok"
        assert pulse.lock_holder(str(lk)) == holder.pid
    finally:
        holder.kill()
    assert pulse.lock_holder(str(tmp_path / "nope")) is None


@needs_locks
def test_waiting_phase_names_lock_holder(store, tmp_path):
    from ahub import config
    lk = tmp_path / "db.lock"
    lk.write_text("")
    proj = config.parse_project({"schema_version": 2, "name": "P", "resources": {"db": {"lock": str(lk)}},
                                 "test_resource": "db"}, tmp_path)
    tid = active(store, phase="waiting")
    store.add_session(task_id=tid, provider="fake", role="executor", external_id="s1")
    providers.register("fake", StatefulFake(SessionState(last_activity_ms=1)))
    fd = os.open(lk, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        p = pulse.task_pulse(store, store.get_task(tid), live={tid: 1}, project=proj, now=60 * 60_000)
        assert p.state == "waiting" and "держит" in p.reason
    finally:
        os.close(fd)


def test_waiting_without_locks_names_no_holder(store, tmp_path):
    """Without /proc/locks (on macOS) the lock is busy but there is no holder data — the pulse does not lie (any OS)."""
    from ahub import config
    lk = tmp_path / "db.lock"
    lk.write_text("")
    proj = config.parse_project({"schema_version": 2, "name": "P", "resources": {"db": {"lock": str(lk)}},
                                 "test_resource": "db"}, tmp_path)
    tid = active(store, phase="waiting")
    store.add_session(task_id=tid, provider="fake", role="executor", external_id="s1")
    providers.register("fake", StatefulFake(SessionState(last_activity_ms=1)))
    fd = os.open(lk, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert pulse.lock_holder(str(lk), NOPROC) is None
        p = pulse.task_pulse(store, store.get_task(tid), live={tid: 1}, project=proj, now=60 * 60_000,
                             proc_root=NOPROC)
        assert p.state == "waiting" and p.reason == "ждёт"
    finally:
        os.close(fd)


def test_describe_short_cmdline(monkeypatch):
    """Pulse reasons are short: program basename and first argument (including module for -m)."""
    from ahub import procs

    monkeypatch.setattr(procs, "cmdline", lambda pid, root: ["/home/user/venv/bin/python", "-m", "pytest", "tests/"])
    assert pulse._describe(42, "/proc") == "python -m pytest"

    monkeypatch.setattr(procs, "cmdline", lambda pid, root: ["/usr/bin/git", "status", "-s"])
    assert pulse._describe(42, "/proc") == "git status"

    monkeypatch.setattr(procs, "cmdline", lambda pid, root: ["/bin/sleep", "10"])
    assert pulse._describe(42, "/proc") == "sleep 10"

    monkeypatch.setattr(procs, "cmdline", lambda pid, root: ["/bin/sh"])
    assert pulse._describe(42, "/proc") == "sh"

    monkeypatch.setattr(procs, "cmdline", lambda pid, root: [])
    assert pulse._describe(42, "/proc") == "pid 42"
