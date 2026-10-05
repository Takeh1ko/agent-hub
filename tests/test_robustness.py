"""Hub robustness: lock wait is a wait, invalid result.json, old-code worker survives update."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ahub import accept, gates, reasons, tasks
from ahub.engine import Engine, _stale_code_error
from ahub.model import Kind, State
from ahub.store import Store
from tests.enginekit import install_fake, make_project
from tests.test_engine_code import work


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def _code_task(store: Store, project, **kw):
    kw.setdefault("paths", ["core/**", "tests/**"])
    kw.setdefault("accept", ["tests/test_a.py::test_x"])
    kw.setdefault("review_level", 0)
    return tasks.create(
        store,
        tasks.TaskSpec(project="P", kind=Kind.CODE, title="fix", model="fake", **kw),
        project,
        collect=False,
    )


def test_lock_timeout_raises_not_red(tmp_path, monkeypatch):
    """A busy lock raises LockTimeout — run_acceptance never reports it as red."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_marker.py").write_text("def test_marker():\n    assert True\n")
    project = __import__("ahub.config", fromlist=["parse_project"]).parse_project(
        {
            "schema_version": 2,
            "name": "P",
            "python": sys.executable,
            "allowed_paths": ["tests/**"],
            "resources": {"db": {"lock": str(tmp_path / "db.lock"), "capacity": 1}},
            "test_resource": "db",
        },
        root,
    )
    import fcntl
    import os

    fd = os.open(str(tmp_path / "db.lock"), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(gates.LockTimeout):
            gates.with_lock(str(tmp_path / "db.lock"), lambda: None, wait_s=0.2)
        # run_acceptance propagates the wait instead of returning red
        def _busy(*a, **kw):
            raise gates.LockTimeout("lock busy")

        monkeypatch.setattr(gates, "with_lock", _busy)
        with pytest.raises(gates.LockTimeout):
            gates.run_acceptance(project, str(root), ["tests/test_marker.py"], task_label="T1")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_engine_lock_busy_goes_to_queue(store: Store, tmp_path, monkeypatch):
    """Lock timeout in gates is a wait: QUEUED with wait_test_lock, no fix round."""
    project = make_project(tmp_path)
    fake = install_fake(store, [work()])
    t = _code_task(store, project)

    def _busy(*a, **kw):
        raise gates.LockTimeout("lock busy", stopped=False)

    monkeypatch.setattr(gates, "run_acceptance", _busy)
    # gates.check calls run_acceptance for acceptance; force it through the lock path
    res = Engine(store, project, t.id, sleep=lambda s: None).run()
    assert res.state is State.QUEUED, res.reason
    assert '"wait_test_lock"' in store.get_task(t.id).state_reason
    assert store.get_task(t.id).state is State.QUEUED
    assert len(fake.calls) == 1  # never reached the worker as a failing test


def test_lock_wait_resume_runs_gates_without_new_turn(store: Store, tmp_path, monkeypatch):
    """After a lock-wait requeue the resume goes straight to the gates: no new worker turn."""
    project = make_project(tmp_path)
    fake = install_fake(store, [work()])
    t = _code_task(store, project)
    real_run_acceptance = gates.run_acceptance
    calls = {"n": 0}

    def _busy_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gates.LockTimeout("lock busy", stopped=False)
        return real_run_acceptance(*a, **kw)

    monkeypatch.setattr(gates, "run_acceptance", _busy_once)
    first = Engine(store, project, t.id, sleep=lambda s: None).run()
    assert first.state is State.QUEUED, first.reason
    assert '"wait_test_lock"' in store.get_task(t.id).state_reason
    assert len(fake.calls) == 1
    sessions_after_first = len(store.list_sessions(t.id))

    second = Engine(store, project, t.id, sleep=lambda s: None).run()
    assert second.state is State.DONE, second.reason
    assert len(fake.calls) == 1  # the resume ran the gates, not another worker turn
    assert len(store.list_sessions(t.id)) == sessions_after_first
    assert "lock_wait" not in store.get_task(t.id).limits  # the flag is cleared when used


def test_lock_wait_resume_with_fresh_session_runs_a_turn(store: Store, tmp_path, monkeypatch):
    """A fresh session overrules the lock-wait skip: the worker still gets its turn."""
    project = make_project(tmp_path)
    fake = install_fake(store, [work(), work(session="ses_new", text="Y = 3\n")])
    t = _code_task(store, project)
    real_run_acceptance = gates.run_acceptance
    calls = {"n": 0}

    def _busy_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise gates.LockTimeout("lock busy", stopped=False)
        return real_run_acceptance(*a, **kw)

    monkeypatch.setattr(gates, "run_acceptance", _busy_once)
    first = Engine(store, project, t.id, sleep=lambda s: None).run()
    assert first.state is State.QUEUED, first.reason
    assert len(fake.calls) == 1
    lim = dict(store.get_task(t.id).limits)
    lim["fresh_session"] = True
    store.update_task(t.id, limits=lim)

    second = Engine(store, project, t.id, sleep=lambda s: None).run()
    assert second.state is State.DONE, second.reason
    assert len(fake.calls) == 2  # the fresh turn was not skipped
    assert "lock_wait" not in store.get_task(t.id).limits


def test_engine_lock_stopped_is_stopped(store: Store, tmp_path, monkeypatch):
    """A stop during the lock wait is STOPPED, not a wait and not red."""
    project = make_project(tmp_path)
    install_fake(store, [work()])
    t = _code_task(store, project)

    def _stopped(*a, **kw):
        raise gates.LockTimeout("stopped", stopped=True)

    monkeypatch.setattr(gates, "run_acceptance", _stopped)
    res = Engine(store, project, t.id, sleep=lambda s: None).run()
    assert res.state is State.STOPPED


def test_accept_lock_busy_is_one_line_refusal(store: Store, tmp_path, monkeypatch):
    """Accept on a busy test lock refuses without ever merging (verify-before-move)."""
    from tests.test_accept import done_code, git_out

    project = make_project(tmp_path)
    t, res, _ = done_code(store, project)
    assert res.state is State.DONE
    before = git_out(project.root, "rev-parse", "HEAD").strip()

    def _busy(*a, **kw):
        raise gates.LockTimeout("lock busy")

    from ahub import gates as g

    monkeypatch.setattr(g, "run_acceptance", _busy)
    with pytest.raises(accept.DecisionError) as exc:
        accept.accept(store, project, t.id)
    assert "\n" not in str(exc.value)  # one line, not a red-tests report with a tail
    after = git_out(project.root, "rev-parse", "HEAD").strip()
    assert after == before  # nothing was merged; retry the accept
    cur = store.get_task(t.id)
    assert cur.state is State.NEEDS_DECISION
    assert "wait_test_lock" in cur.state_reason


def test_invalid_result_json_is_own_problem(store: Store, tmp_path):
    """A result.json that does not parse reports result_json with the parser error."""
    project = make_project(tmp_path)
    install_fake(store, [])
    t = _code_task(store, project)
    from ahub import workspace

    ws = workspace.ensure(project, t.id)
    store.update_task(t.id, worktree=ws.path, branch=ws.branch, base_sha=ws.base_sha)
    bad = '{"summary": "x", "status": "done", "commit": "abc", "files": []} EXTRA'
    (Path(ws.path) / ".ahub" / "result.json").write_text(bad, encoding="utf-8")
    g = gates.check(project, store.get_task(t.id), run_tests=False)
    codes = [p.code for p in g.repairable if isinstance(p, gates.Problem)]
    assert "result_json" in codes
    assert "no_result" not in codes
    assert "result_json" in gates.RESULT_JSON_CODES
    text = next(str(p) for p in g.repairable if isinstance(p, gates.Problem) and p.code == "result_json")
    assert "result.json" in text and "Extra data" in text
    assert "Extra data: x" in reasons.text(reasons.dump("gate_result_json", err="Extra data: x"))


def test_scout_invalid_result_json_has_parser_error(store: Store, tmp_path):
    """Scout with a broken result.json reports result_json, not a bare 'not a JSON object'."""
    project = make_project(tmp_path)
    install_fake(store, [])
    t = tasks.create(
        store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="x", model="fake"), project, collect=False
    )
    from ahub import workspace

    ws = workspace.ensure(project, t.id)
    store.update_task(t.id, worktree=ws.path, branch=ws.branch, base_sha=ws.base_sha)
    base = Path(ws.path) / ".ahub"
    base.mkdir(parents=True, exist_ok=True)
    (base / "result.json").write_text('{"summary": "x", "status": "done" EXTRA', encoding="utf-8")
    (base / "report.md").write_text("## Summary\nx\n", encoding="utf-8")
    eng = Engine(store, project, t.id)
    codes = [p.code for p in eng._check_scout(store.get_task(t.id))]
    assert "result_json" in codes


def test_stale_import_is_poll_failed(store: Store, tmp_path, monkeypatch):
    """Changed hub code under the worker exits 4 like the poll failure path, not ERROR."""
    from ahub import selfupdate

    project = make_project(tmp_path)
    install_fake(store, [{"session": "ses_x", "steps": [{"sleep": 60}]}])
    t = tasks.create(
        store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="x", model="fake"), project, collect=False
    )
    assert _stale_code_error(ImportError("cannot import name 'QuotaBucket' from 'ahub.providers.base'"))
    assert _stale_code_error(AttributeError("module 'ahub.providers.base' has no attribute 'QuotaBucket'"))
    assert not _stale_code_error(ImportError("cannot import name 'foo' from 'bar'"))
    assert not _stale_code_error(AttributeError("'NoneType' object has no attribute 'foo'"))

    prints = iter(["v1", "v1", "v2"])
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: next(prints, "v2"))

    real_prepare = Engine._prepare

    def _boom(self, t):
        raise ImportError("cannot import name 'QuotaBucket' from 'ahub.providers.base'")

    monkeypatch.setattr(Engine, "_prepare", _boom)
    from ahub import worker

    monkeypatch.setattr(worker, "find_project", lambda name: project)
    try:
        assert worker.main([f"T{t.id}"]) == 4
    finally:
        monkeypatch.setattr(Engine, "_prepare", real_prepare)
    left = store.get_task(t.id)
    assert left.state is not State.ERROR

    # provider subprocess output mentioning ImportError stays a normal step failure, not exit 4
    assert not _stale_code_error(ValueError("ImportError: bad import in model output"))


def test_stale_import_same_code_goes_to_error(store: Store, tmp_path, monkeypatch):
    """Unchanged hub code is a genuine bug: no exit 4, the task goes to error as before."""
    from ahub import selfupdate

    project = make_project(tmp_path)
    install_fake(store, [{"session": "ses_x", "steps": [{"sleep": 60}]}])
    t = tasks.create(
        store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="x", model="fake"), project, collect=False
    )
    monkeypatch.setattr(selfupdate, "code_fingerprint", lambda: "v1")

    real_prepare = Engine._prepare

    def _boom(self, t):
        raise ImportError("cannot import name 'QuotaBucket' from 'ahub.providers.base'")

    monkeypatch.setattr(Engine, "_prepare", _boom)
    from ahub import worker

    monkeypatch.setattr(worker, "find_project", lambda name: project)
    try:
        assert worker.main([f"T{t.id}"]) != 4
    finally:
        monkeypatch.setattr(Engine, "_prepare", real_prepare)
    left = store.get_task(t.id)
    assert left.state is State.ERROR


def test_provider_output_importerror_is_not_stale():
    """Only ImportError/AttributeError count — model text mentioning them does not."""
    assert not _stale_code_error(RuntimeError("ImportError: foo"))
    assert not _stale_code_error(ValueError("ahub mentioned in text but wrong type"))
