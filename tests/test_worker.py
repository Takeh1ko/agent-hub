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
    return any(procs.alive(p) and worktree in " ".join(procs.cmdline(p)) for p in procs.pids())


def wait_agent(worktree: str, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if agent_of(worktree):
            return True
        time.sleep(0.1)
    return False


def test_poll_tolerates_two_failures_then_gives_up(store, project):
    """The stop poll cannot read the row: two failures are tolerated, the third one ends the process."""
    install_fake(store, [])
    t = scout(store, project)
    eng = Engine(store, project, t.id)
    eng._budget_at = time.monotonic()  # the budget is a separate poll — here the stop poll is under test
    boom = {"n": 0}

    def failing():
        boom["n"] += 1
        raise TypeError("Task.__init__() got an unexpected keyword argument 'request_text'")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(eng, "task", failing)
    try:
        for _ in range(POLL_FAIL_MAX - 1):
            assert eng.stop_requested() is False
        with pytest.raises(PollFailed, match="request: TypeError"):
            eng.stop_requested()
        assert boom["n"] == POLL_FAIL_MAX
    finally:
        monkey.undo()


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
    assert worker.main([f"T{t.id}"]) == 4
    s = store.list_sessions(t.id)[0]
    assert s.status == "killed" and s.outcome == "killed" and s.ended_at
    time.sleep(0.5)
    assert not agent_of(wt), "процесс провайдера пережил отказ опроса"
    left = store.get_task(t.id)
    assert left.state is State.WORKING and not left.owner  # left to the service (orphan pickup)


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
        assert wait_agent(wt), "процесс провайдера не стартовал"
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0
    finally:
        if p.poll() is None:
            p.kill()
    time.sleep(0.5)
    assert not agent_of(wt), "процесс провайдера остался сиротой после SIGTERM"
    assert store.get_task(t.id).state is State.WORKING  # the task is left for the service
