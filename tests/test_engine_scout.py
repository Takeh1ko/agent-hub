"""Движок на разведке: этапы, итог по форме, repair, повторы, тишина, остановка, аренда, подхват сироты."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ahub import tasks, transitions
from ahub.engine import Engine
from ahub.model import Kind, State
from ahub.store import Store
from tests.enginekit import install_fake, make_project, scout_ok


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def new_scout(store, project, **kw):
    return tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="где утечка", model="fake", **kw),
                        project, collect=False)


def run(store, project, tid):
    return Engine(store, project, tid, sleep=lambda s: None).run()


def events(store, tid):
    return [e.kind for e in store.events(task_id=tid)]


def test_happy_path(store, project, tmp_path):
    fake = install_fake(store, [scout_ok()])
    t = new_scout(store, project, spec="посмотри core/")
    res = run(store, project, t.id)
    assert res.state is State.DONE
    t = store.get_task(t.id)
    assert t.state is State.DONE and t.owner == "" and t.round == 1
    assert Path(t.worktree) == tmp_path / "wt" / f"T{t.id}" and t.branch == f"ahub/T{t.id}" and t.base_sha
    done = store.events(task_id=t.id, needs_reaction=True)[-1]
    assert done.kind == "done" and done.payload["summary"] == "нашёл" and done.payload["report_bytes"] > 0
    assert done.payload["cost_go"] == 0.01
    s = store.list_sessions(t.id)
    assert len(s) == 1 and s[0].external_id == "ses_s" and s[0].status == "ok" and s[0].role == "scout"
    assert "где утечка" in fake.calls[0]["prompt"] and "## Суть" in fake.calls[0]["prompt"]
    assert fake.calls[0]["cwd"] == t.worktree
    assert (Path(t.worktree) / ".ahub" / "logs" / "scout.log").exists()


def test_repair_once(store, project):
    fake = install_fake(store, [
        {"session": "ses_r", "steps": [{"event": {"type": "text", "text": "всё"}}]},  # не сдал итог
        scout_ok("ses_r"),
    ])
    t = new_scout(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert "Result is not in the required form" in fake.calls[1]["prompt"] and fake.calls[1]["session_id"] == "ses_r"
    assert len(store.list_sessions(t.id)) == 1  # продолжение — та же сессия


def test_repair_fails(store, project):
    install_fake(store, [{"session": "s", "steps": []}, {"session": "s", "steps": []}])
    t = new_scout(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "нет .ahub/result.json" in res.reason


def test_scout_changed_files_no_repair(store, project):
    bad = scout_ok(extra_steps=[{"write": {"path": "core/a.py", "text": "X = 2\n"}}])
    fake = install_fake(store, [bad])
    t = new_scout(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "изменила файлы" in res.reason and "core/a.py" in res.reason
    assert len(fake.calls) == 1


def test_blocked(store, project):
    blocked = {"session": "s", "steps": [
        {"write": {"path": ".ahub/result.json", "text": json.dumps({"summary": "нет доступа к БД", "status": "blocked"})}}]}
    install_fake(store, [blocked])
    t = new_scout(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "нет доступа к БД" in res.reason


def test_transient_retry_same_session(store, project):
    fake = install_fake(store, [
        {"session": "ses_t", "steps": [{"event": {"type": "error", "message": "status 503"}}], "exit": 1},
        scout_ok("ses_t"),
    ])
    t = new_scout(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert fake.calls[1]["session_id"] == "ses_t"
    assert "retry" in events(store, t.id)


def test_transient_exhausted(store, project):
    err = {"session": "s", "steps": [{"event": {"type": "error", "message": "ECONNREFUSED"}}], "exit": 1}
    install_fake(store, [err, err, err])
    t = new_scout(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "повторы кончились" in res.reason


def test_silence_then_continue(store, project):
    fake = install_fake(store, [{"session": "ses_q", "steps": [{"sleep": 10}]}, scout_ok("ses_q")])
    t = new_scout(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert fake.calls[1]["session_id"] == "ses_q" and "continue the task" in fake.calls[1]["prompt"].lower()
    assert "silence" in events(store, t.id)


def test_silence_twice(store, project):
    install_fake(store, [{"session": "s", "steps": [{"sleep": 10}]}, {"session": "s", "steps": [{"sleep": 10}]}])
    t = new_scout(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "молчал дважды" in res.reason


@pytest.mark.parametrize("msg,state", [("invalid tool call", State.ERROR), ("quota exceeded", State.NEEDS_DECISION),
                                       ("401 Unauthorized", State.ERROR)])
def test_error_outcomes(store, project, msg, state):
    install_fake(store, [{"session": "s", "steps": [{"event": {"type": "error", "message": msg}}], "exit": 1}])
    t = new_scout(store, project)
    assert run(store, project, t.id).state is state


def test_stop_request_during_run(store, project):
    install_fake(store, [{"session": "s", "steps": [{"child": 30}]}])
    t = new_scout(store, project)
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("r", run(store, project, t.id)))
    th.start()
    for _ in range(100):
        if store.get_task(t.id).state is State.WORKING and store.list_sessions(t.id):
            break
        time.sleep(0.1)
    assert transitions.request_stop(store, t.id) == "requested"
    th.join(30)
    assert box["r"].state is State.STOPPED
    assert store.get_task(t.id).state is State.STOPPED


def test_busy_task(store, project):
    install_fake(store, [])
    t = new_scout(store, project)
    transitions.acquire(store, t.id, "другой", pid=1)
    res = run(store, project, t.id)
    assert res.reason == "занята" and store.get_task(t.id).state is State.QUEUED


def test_orphan_resumes_same_session(store, project):
    fake = install_fake(store, [scout_ok("ses_old")])
    t = new_scout(store, project)
    transitions.move(store, t.id, State.PREPARING)
    from ahub import workspace
    ws = workspace.ensure(project, t.id)
    transitions.move(store, t.id, State.WORKING, fields={"worktree": ws.path, "branch": ws.branch,
                                                         "base_sha": ws.base_sha, "round": 1})
    store.add_session(task_id=t.id, provider="fake", role="scout", model="fake", external_id="ses_old")
    assert run(store, project, t.id).state is State.DONE
    assert fake.calls[0]["session_id"] == "ses_old" and "continue the task" in fake.calls[0]["prompt"].lower()


def test_unsupported_kind_is_decision(store, project):
    install_fake(store, [])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.ROUTINE, title="x", paths=["core/**"],
                                           review_level=0, model="fake"), project, collect=False)
    assert run(store, project, t.id).state is State.NEEDS_DECISION
