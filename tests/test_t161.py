"""T161: accept fairness (test-lock priority) and safety (verify before main moves)."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path

import pytest

from ahub import accept, gates
from ahub.model import State
from ahub.store import Store
from tests.conftest import wait_until
from tests.enginekit import make_project
from tests.test_accept import done_code, git_out


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def test_gates_defer_to_accept_marker(tmp_path):
    """Gates keep waiting while an accept-waiting marker is live, even when the lock is free."""
    lock = str(tmp_path / "db.lock")
    Path(lock).touch()
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        pytest.skip("lock already held")
    acquired: list[float] = []

    def _run():
        acquired.append(time.monotonic())
        return True

    th = threading.Thread(target=lambda: gates.with_lock(lock, _run, wait_s=10))
    try:
        th.start()
        assert wait_until(lambda: True, timeout=0.2) is not None
        time.sleep(0.3)  # the gates waiter is polling on the busy lock
        # an accept starts waiting: live marker, lock still held
        gates._mark_accept_waiting(lock)
        assert gates.accept_waiting(lock)
        time.sleep(0.3)
        fcntl.flock(fd, fcntl.LOCK_UN)  # the holder releases, but the accept still waits
        time.sleep(1.2)  # gates must not take the free lock while the marker is live
        assert acquired == [], "gates took the lock while an accept waited"
        gates._clear_accept_waiting(lock)  # the accept went on (or died): marker gone
        th.join(timeout=10)
        assert acquired != [], "gates never took the lock after the marker cleared"
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        th.join(timeout=10)
        gates._clear_accept_waiting(lock)


def test_accept_gets_lock_first(tmp_path):
    """An accept that starts waiting while gates poll gets the lock first."""
    lock = str(tmp_path / "db.lock")
    Path(lock).touch()
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    order: list[str] = []
    try:

        def _gates():
            order.append("gates")
            time.sleep(0.3)

        def _accept():
            order.append("accept")
            time.sleep(0.3)

        th_g = threading.Thread(target=lambda: gates.with_lock(lock, _gates, wait_s=15))
        th_g.start()
        time.sleep(0.8)  # gates are polling; their phase is ahead
        th_a = threading.Thread(
            target=lambda: gates.with_lock(lock, _accept, wait_s=15, is_accept=True))
        th_a.start()
        assert wait_until(lambda: gates.accept_waiting(lock), timeout=10), "accept did not mark its wait"
        fcntl.flock(fd, fcntl.LOCK_UN)  # the current run ends: the accept goes next
        th_a.join(timeout=15)
        th_g.join(timeout=15)
        assert order == ["accept", "gates"], f"accept did not go first: {order}"
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        gates._clear_accept_waiting(lock)


def test_stale_marker_is_ignored(tmp_path):
    """A dead pid in the marker never blocks the gates."""
    lock = str(tmp_path / "db.lock")
    Path(lock).touch()
    gates.accept_waiting_path(lock).write_text("999999\n", encoding="utf-8")
    assert not gates.accept_waiting(lock)
    assert gates.with_lock(lock, lambda: 42, wait_s=5) == 42


def test_red_leaves_tip_unchanged_and_unseen(store, project, monkeypatch):
    """Red acceptance: the work-branch tip is unchanged, also during the run; task files never land."""
    t, _, _ = done_code(store, project)
    (Path(project.root) / "core" / "a.py").write_text("X = 5\n")
    from tests.enginekit import git

    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "broke X")
    before = git_out(project.root, "rev-parse", "HEAD").strip()
    seen: list[str] = []
    from ahub import gates as g

    real = g.run_acceptance

    def _rec(project_, cwd, nodes, **kw):
        seen.append(git_out(project.root, "rev-parse", "HEAD").strip())
        assert not (Path(project.root) / "core" / "b.py").exists()
        return real(project_, cwd, nodes, **kw)

    monkeypatch.setattr(g, "run_acceptance", _rec)
    with pytest.raises(accept.DecisionError, match="ничего не слито"):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD").strip() == before
    assert seen and all(s == before for s in seen), f"main moved during acceptance: {seen}"
    assert not (Path(project.root) / "core" / "b.py").exists()
    assert '"accept_red"' in store.get_task(t.id).state_reason
    from ahub.accept import _verify_branch, _verify_path

    assert not _verify_path(project, store.get_task(t.id)).exists()
    assert _verify_branch(project, store.get_task(t.id)) not in git_out(project.root, "branch")


def test_reader_never_sees_unverified_merge(store, project, monkeypatch):
    """A concurrent reader of main never sees the merge before acceptance is green."""
    t, _, _ = done_code(store, project)
    from ahub import gates as g

    real = g.run_acceptance
    finished = threading.Event()

    def _slow(project_, cwd, nodes, **kw):
        time.sleep(1.0)  # the merge is verified in temp meanwhile; main must not show it
        out = real(project_, cwd, nodes, **kw)
        finished.set()
        return out

    monkeypatch.setattr(g, "run_acceptance", _slow)
    seen: list[bool] = []

    def _reader():
        while not finished.is_set():
            seen.append((Path(project.root) / "core" / "b.py").exists())
            time.sleep(0.05)

    th = threading.Thread(target=_reader)
    th.start()
    msg = accept.accept(store, project, t.id)
    th.join(timeout=15)
    assert "merged into main" in msg or "слита в main" in msg
    assert seen, "the reader never polled"
    assert not any(seen), "a concurrent reader saw the unverified merge in main"
    assert (Path(project.root) / "core" / "b.py").read_text() == "Y = 2\n"
    from ahub.accept import _verify_branch, _verify_path

    assert not _verify_path(project, store.get_task(t.id)).exists()
    assert _verify_branch(project, store.get_task(t.id)) not in git_out(project.root, "branch")
    assert store.get_task(t.id).state is State.ACCEPTED
