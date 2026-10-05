"""The task process: a poll that keeps failing gives the task back, SIGTERM takes the provider with it."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ahub import paths, procs, tasks, worker, workspace
from ahub.engine import POLL_FAIL_MAX, Engine, PollFailed
from ahub.model import Kind, State
from ahub.store import Store
from tests.conftest import wait_until
from tests.enginekit import install_fake, make_project

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def scout(store, project, **kw):
    return tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="x", model="fake", **kw),
                        project, collect=False)


def agent_of(worktree: str) -> bool:
    """A live provider process of that work copy (the scenario file it runs lives in the copy)."""
    # Match the provider only: `git worktree add <copy>` also carries the path while preparing.
    for p in procs.pids():
        cmd = " ".join(procs.cmdline(p))
        if procs.alive(p) and "fake_agent" in cmd and worktree in cmd:
            return True
    return False


def kill_agents(worktree: str) -> None:
    """Cleanup: no provider process of that copy outlives the test (a failure may leave one behind)."""
    for pid in [p for p in procs.pids() if "fake_agent" in " ".join(procs.cmdline(p))
                and worktree in " ".join(procs.cmdline(p))]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_poll_tolerates_two_failures_then_gives_up(store, project, monkeypatch):
    """The stop poll cannot read the row: two failures are tolerated, the third one ends the process."""
    install_fake(store, [])
    t = scout(store, project)
    eng = Engine(store, project, t.id)
    eng._budget_at = time.monotonic()  # the budget is a separate poll — here the stop poll is under test
    boom = {"n": 0}

    def failing():
        boom["n"] += 1
        raise TypeError("Task.__init__() got an unexpected keyword argument 'request_text'")

    monkeypatch.setattr(eng, "task", failing)
    for _ in range(POLL_FAIL_MAX - 1):
        assert eng.stop_requested() is False
    with pytest.raises(PollFailed, match="request: TypeError"):
        eng.stop_requested()
    assert boom["n"] == POLL_FAIL_MAX


def test_worker_exit_3_is_a_flag_not_a_text_match(store, project, monkeypatch):
    """Exit code 3 (busy) is a flag on the settled result, not a comparison of rendered text.

    The engine settles under one language and the worker reads the result under another (a `--lang en`
    or an edited template): the code must not depend on the text.
    """
    from ahub import i18n, reasons, transitions
    install_fake(store, [])
    t = scout(store, project)
    transitions.move(store, t.id, State.PREPARING)
    assert transitions.acquire(store, t.id, "other_owner", pid=99999)  # the task is held
    settled = Engine(store, project, t.id).run()
    assert settled.busy is True
    i18n.set_lang("en")
    assert settled.reason != reasons.text(reasons.dump("busy"))  # the two languages differ here

    class _Settled:
        def __init__(self, *args, **kw):
            pass

        def run(self):
            return settled

    monkeypatch.setattr(worker, "Engine", _Settled)
    monkeypatch.setattr(worker, "find_project", lambda name: project)
    assert worker.main([f"T{t.id}"]) == 3


def test_poll_first_failure_logs_warning(store, project, monkeypatch):
    """First poll failure logs at warning level, not debug."""
    import sqlite3
    install_fake(store, [])
    t = scout(store, project)
    eng = Engine(store, project, t.id)
    eng._budget_at = time.monotonic()
    warnings = []
    monkeypatch.setattr(eng.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))
    monkeypatch.setattr(eng, "task", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database locked")))
    assert eng.stop_requested() is False
    assert len(warnings) == 1
    assert "request poll failed (1/" in warnings[0]


def test_poll_failure_stops_provider_and_exits_nonzero(store, project, monkeypatch, own_signals):
    """Old code on a newer schema: the worker stops the provider session, leaves the task and exits 4."""
    install_fake(store, [{"session": "ses_p", "steps": [{"sleep": 60}]}])
    t = scout(store, project)
    wt = str(workspace.worktree_path(project, t.id))
    real_task = Engine.task

    def flaky_task(self):
        if agent_of(wt):  # the turn is running — the row no longer builds the dataclass
            raise TypeError("Task.__init__() got an unexpected keyword argument 'request_text'")
        return real_task(self)

    monkeypatch.setattr(Engine, "task", flaky_task)
    monkeypatch.setattr(worker, "find_project", lambda name: project)
    try:
        assert worker.main([f"T{t.id}"]) == 4
        s = store.list_sessions(t.id)[0]
        assert s.status == "killed" and s.outcome == "killed" and s.ended_at
        assert wait_until(lambda: not agent_of(wt)), "процесс провайдера пережил отказ опроса"
        left = store.get_task(t.id)
        assert left.state is State.WORKING and not left.owner  # left to the service (orphan pickup)
    finally:
        kill_agents(wt)


def test_sigterm_takes_the_provider_process_group_with_it(store, tmp_path, monkeypatch):
    """SIGTERM to a task process: the provider group is stopped, then the process exits."""
    project = make_project(tmp_path)
    (Path(project.root) / ".hub.toml").write_text(
        f'schema_version = 2\nname = "P"\nworktrees = "{tmp_path / "wt"}"\n'
        '[timeouts]\nidle_s = 60\n', encoding="utf-8")
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text(f'projects = ["{project.root}"]\n', encoding="utf-8")
    q = tmp_path / "fakeq"
    q.mkdir()
    (q / "001.json").write_text(json.dumps({"session": "ses_t", "steps": [{"sleep": 60}]}), encoding="utf-8")
    monkeypatch.setenv("AHUB_FAKE_QUEUE", str(q))
    install_fake(store, [])
    t = scout(store, project)
    wt = str(workspace.worktree_path(project, t.id))
    p = subprocess.Popen([sys.executable, "-m", "ahub.worker", f"T{t.id}"], cwd=str(REPO),
                         env={**os.environ, "PYTHONPATH": str(REPO)},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        assert wait_until(lambda: agent_of(wt)), "процесс провайдера не стартовал"
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0
        assert wait_until(lambda: not agent_of(wt)), "процесс провайдера остался сиротой после SIGTERM"
        assert store.get_task(t.id).state is State.WORKING  # the task is left for the service
    finally:
        if p.poll() is None:
            p.kill()
        kill_agents(wt)
