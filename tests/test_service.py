"""Service: live processes from /proc, queue, slots, resources, dependencies, pause; an end-to-end task process run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ahub import paths, reasons, service, tasks, transitions
from ahub.model import Kind, State
from ahub.store import Store
from tests.enginekit import install_fake, make_project, scout_ok


@pytest.fixture
def store() -> Store:
    return Store()


def fake_proc(root: Path, pid: int, args: list[str], state: str = "S") -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    (d / "stat").write_text(f"{pid} (python) {state} 1 1 1")


def test_live_workers(tmp_path):
    root = tmp_path / "proc"
    fake_proc(root, 100, ["/usr/bin/python3", "-m", "ahub.worker", "T7"])
    fake_proc(root, 101, ["python", "-m", "ahub.worker", "8"], state="Z")  # zombie — not alive
    fake_proc(root, 102, ["python", "-m", "hub.commands.queue", "--run-one", "T9"])  # v1 — not ours
    fake_proc(root, 103, ["bash"])
    (root / "self").mkdir()
    assert service.live_workers(root) == {7: 100}


def test_live_workers_ignores_a_command_that_mentions_the_mark(tmp_path):
    """A shell command, a grep or an agent prompt with the mark in it is not a task process."""
    root = tmp_path / "proc"
    fake_proc(root, 200, ["/bin/bash", "-c", "python -m ahub.worker T7 &  # a note"])
    fake_proc(root, 201, ["/usr/bin/grep", "-rn", "ahub.worker", "T7", "/proc"])
    fake_proc(root, 202, ["node", "opencode", "run", "task T70: how do I run python -m ahub.worker T7?"])
    (root / "self").mkdir()
    assert service.live_workers(root) == {}


def test_a_worker_process_runs_the_code_that_started_it():
    """PYTHONPATH of a spawned process points at this hub: an editable install of another checkout
    (with another schema) must not win in the child."""
    root = str(Path(service.__file__).resolve().parent.parent)
    assert service.hub_env()["PYTHONPATH"].split(os.pathsep)[0] == root
    here = subprocess.run([sys.executable, "-c", "import ahub; print(ahub.__file__)"],
                          capture_output=True, text=True, env=service.hub_env(), cwd="/").stdout.strip()
    assert here == str(Path(service.__file__).parent / "__init__.py")


def scout(store, project, **kw):
    return tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="x", model="fake", **kw),
                        project, collect=False)


def code(store, project, **kw):
    kw.setdefault("paths", ["core/**", "tests/**"])
    kw.setdefault("accept", ["tests/test_a.py::test_x"])
    return tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="починить", model="fake", **kw),
                        project, collect=False)


class Recorder:
    def __init__(self):
        self.spawned = []

    def __call__(self, tid):
        self.spawned.append(tid)
        return 10_000 + tid


def svc(store, project, tmp_path, lock_busy=lambda p: False):
    rec = Recorder()
    (tmp_path / "proc").mkdir(exist_ok=True)
    return service.Service(store, [project], spawn=rec, proc_root=tmp_path / "proc", lock_busy=lock_busy), rec


def test_slots_and_grace(store, tmp_path):
    project = make_project(tmp_path, max_parallel=2)
    install_fake(store, [])
    ids = [scout(store, project).id for _ in range(3)]
    s, rec = svc(store, project, tmp_path)
    r = s.tick()
    assert rec.spawned == ids[:2] and r.load["P"].waiting == {ids[2]: "ждёт места (2/2)"}
    assert reasons.text(store.get_task(ids[2]).state_reason) == "ждёт места (2/2)"
    s.tick()  # the spawned ones are not in /proc yet — do not start them again
    assert rec.spawned == ids[:2]


def test_live_workers_of_previous_instance_take_slots(store, tmp_path):
    project = make_project(tmp_path, max_parallel=1)
    install_fake(store, [])
    old = scout(store, project)
    transitions.move(store, old.id, State.PREPARING)
    new = scout(store, project)
    s, rec = svc(store, project, tmp_path)
    fake_proc(tmp_path / "proc", 555, ["python", "-m", "ahub.worker", f"T{old.id}"])
    r = s.tick()
    assert rec.spawned == [] and r.load["P"].running == [old.id]
    assert "ждёт места" in reasons.text(store.get_task(new.id).state_reason)


def test_dependencies(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    a = scout(store, project)
    transitions.move(store, a.id, State.STOPPED)  # not accepted
    b = scout(store, project, after=[a.id])
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert rec.spawned == [] and "ждёт принятия" in reasons.text(store.get_task(b.id).state_reason)
    transitions.move(store, a.id, State.QUEUED)
    for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
        transitions.move(store, a.id, st)
    s.tick()
    assert rec.spawned == [b.id] and reasons.text(store.get_task(b.id).state_reason) == ""


def test_resources(store, tmp_path):
    project = make_project(tmp_path, resources={"db": {"lock": str(tmp_path / "db.lock")}, "api": {"capacity": 1}},
                           max_parallel=5)
    install_fake(store, [])
    a = scout(store, project, resources=["api"])
    b = scout(store, project, resources=["api"])
    c = scout(store, project, resources=["db"])
    busy = {"v": True}
    s, rec = svc(store, project, tmp_path, lock_busy=lambda p: busy["v"])
    s.tick()
    assert rec.spawned == [a.id]
    assert reasons.text(store.get_task(b.id).state_reason) == "ждёт ресурс api"
    assert reasons.text(store.get_task(c.id).state_reason) == "ждёт ресурс db (занят вне хаба)"
    busy["v"] = False
    s.tick()
    assert rec.spawned == [a.id, c.id]


def test_test_resource_not_held_by_the_queue(store, tmp_path):
    """The project test resource is not held by the queue: tasks run in parallel, acceptance takes the lock."""
    project = make_project(tmp_path, resources={"db": {"lock": str(tmp_path / "db.lock"), "capacity": 1}},
                           test_resource="db", max_parallel=2)
    install_fake(store, [])
    a = code(store, project)
    b = code(store, project)
    assert a.limits["resources"] == [] and b.limits["resources"] == []
    s, rec = svc(store, project, tmp_path)
    report = s.tick()
    assert rec.spawned == [a.id, b.id] and report.load["P"].waiting == {}


def test_explicit_test_resource_blocks_the_queue(store, tmp_path):
    """Named in --resources by hand — the queue holds it for the whole task, as before."""
    project = make_project(tmp_path, resources={"db": {"capacity": 1}}, test_resource="db", max_parallel=5)
    install_fake(store, [])
    a = code(store, project, resources=["db"])
    b = code(store, project, resources=["db"])
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert rec.spawned == [a.id]
    assert reasons.text(store.get_task(b.id).state_reason) == "ждёт ресурс db"


def test_pause(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    t = scout(store, project)
    s, rec = svc(store, project, tmp_path)
    store.meta_set(service.PAUSE_KEY, "1")
    r = s.tick()
    assert r.paused and rec.spawned == []
    assert reasons.text(store.get_task(t.id).state_reason) == "очередь на паузе"
    store.meta_del(service.PAUSE_KEY)
    s.tick()
    assert rec.spawned == [t.id]
    assert store.meta_get(service.HEARTBEAT_KEY)


def test_external_lock_busy(tmp_path):
    import fcntl
    import os
    p = tmp_path / "x.lock"
    assert not service.external_lock_busy(str(p))  # no file — free
    p.write_text("")
    assert not service.external_lock_busy(str(p))
    fd = os.open(p, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        import subprocess
        import sys
        out = subprocess.run([sys.executable, "-c",
                              f"from ahub.service import external_lock_busy as b; print(b({str(p)!r}))"],
                             capture_output=True, text=True).stdout.strip()
        assert out == "True"
    finally:
        os.close(fd)


def test_end_to_end_real_worker(store, tmp_path, monkeypatch):
    """The service spawns a real task process; it runs a scout on the fake provider until «Готово»."""
    project = make_project(tmp_path)
    (Path(project.root) / ".hub.toml").write_text(
        f'schema_version = 2\nname = "P"\nworktrees = "{tmp_path / "wt"}"\n'
        '[timeouts]\nidle_s = 5\nretry_pause_s = 0\n', encoding="utf-8")
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text(f'projects = ["{project.root}"]\n', encoding="utf-8")
    q = tmp_path / "fakeq"
    q.mkdir()
    (q / "001.json").write_text(json.dumps(scout_ok("ses_e2e")), encoding="utf-8")
    monkeypatch.setenv("AHUB_FAKE_QUEUE", str(q))
    # the worker process must run the code of this checkout, not whatever `ahub` the environment has installed
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    install_fake(store, [])  # the "fake" model in the shared registry
    t = scout(store, project)
    s = service.Service(store)
    r = s.tick()
    assert r.spawned == [t.id]
    for _ in range(200):
        if store.get_task(t.id).state in (State.DONE, State.ERROR, State.NEEDS_DECISION):
            break
        time.sleep(0.1)
    final = store.get_task(t.id)
    log = (paths.state_dir() / "workers" / f"T{t.id}.log")
    assert final.state is State.DONE, (reasons.text(final.state_reason), log.read_text() if log.exists() else "")
    assert store.list_sessions(t.id)[0].external_id == "ses_e2e"


def _orphan_task(store, project, state=State.WORKING, lease_age_ms=10 * 60_000, owner_pid=999999):
    from ahub.time import now_ms
    t = scout(store, project)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, t.id, st)
    if state is State.ACCEPTING:
        transitions.move(store, t.id, State.DONE)
        transitions.move(store, t.id, State.ACCEPTING)
    old = now_ms() - lease_age_ms
    with store.tx() as c:
        c.execute("UPDATE task SET owner='dead', owner_pid=?, lease_until=?, updated_at=? WHERE id=?",
                  (owner_pid, old, old, t.id))
    return t.id


def test_orphan_requeued_once_then_decision(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project)
    s, rec = svc(store, project, tmp_path)
    s.tick()
    t = store.get_task(tid)
    assert rec.spawned == [tid] and t.limits["orphans"] == 1  # requeued and started right away
    assert "orphan" in [e.kind for e in store.events(task_id=tid)]
    # the process vanished again, the task is active without a lease once more
    s.recent.clear()
    with store.tx() as c:
        c.execute("UPDATE task SET state='working', owner='', lease_until=NULL, updated_at=0 WHERE id=?", (tid,))
    s.tick()
    assert store.get_task(tid).state is State.NEEDS_DECISION
    assert "повторно" in reasons.text(store.get_task(tid).state_reason)


def test_orphan_live_lease_untouched(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project, lease_age_ms=-60_000)  # the lease is still alive (a CLI owner)
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert store.get_task(tid).state is State.WORKING and rec.spawned == []


def test_orphan_accepting_is_decision(store, tmp_path):
    """The accept process is gone (owner_pid 999999) and the lease expired — the way out is `ahub accept`."""
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project, state=State.ACCEPTING)
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert store.get_task(tid).state is State.NEEDS_DECISION
    reason = reasons.text(store.get_task(tid).state_reason)  # a code, rendered in the reader's language
    assert "прервана" in reason
    assert f"ahub accept T{tid}" in reason  # the reason says how to finish it


def test_accepting_with_a_live_owner_process_is_not_an_orphan(store, tmp_path):
    """Acceptance is long: the lease is stale, but the owner process lives — the service must not touch the task."""
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project, state=State.ACCEPTING, owner_pid=os.getpid())
    s, rec = svc(store, project, tmp_path)
    fake_proc(tmp_path / "proc", os.getpid(), ["python", "-m", "ahub", "accept", f"T{tid}"])
    s.tick()
    assert store.get_task(tid).state is State.ACCEPTING and rec.spawned == []
    assert "orphan" not in [e.kind for e in store.events(task_id=tid)]


def test_code_fingerprint_and_health(tmp_path):
    a = service.code_fingerprint()
    assert a == service.code_fingerprint()
    ok, why = service.new_code_healthy()
    assert ok, why


def test_restart_self_execs_with_the_hub_on_pythonpath(monkeypatch):
    """A process of `ahub service install` starts as `python -m ahub bot run`, so argv[0] is
    `.../ahub/__main__.py`: the new process runs that file and must still find the package (without an install
    it dies with ModuleNotFoundError — the same reason hub_env exists for the processes the hub starts)."""
    from ahub import selfupdate

    calls = []
    monkeypatch.setattr(os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(os, "execve", lambda *a: calls.append(a))
    service.restart_self()
    assert len(calls) == 1 and len(calls[0]) == 3, "execve with our environment, not execv"
    exe, argv, env = calls[0]
    assert exe == sys.executable and argv[1] == sys.argv[0]
    root = Path(selfupdate.__file__).resolve().parents[1]
    assert Path(env["PYTHONPATH"].split(os.pathsep)[0]) == root


def test_self_update_triggers_restart(store, tmp_path, monkeypatch):
    project = make_project(tmp_path)
    s, rec = svc(store, project, tmp_path)
    prints = iter(["v1", "v2", "v2"])
    monkeypatch.setattr(service, "code_fingerprint", lambda: next(prints))
    monkeypatch.setattr(service, "new_code_healthy", lambda: (True, ""))
    monkeypatch.setattr(service, "CODE_CHECK_S", 0.0)
    called = []
    monkeypatch.setattr(service, "restart_self", lambda: (called.append(1), s.stop()))
    s.run_forever(poll_s=0.01)
    assert called == [1]


def test_self_update_skips_broken_code(store, tmp_path, monkeypatch):
    project = make_project(tmp_path)
    s, rec = svc(store, project, tmp_path)
    prints = iter(["v1"] + ["v2"] * 50)
    monkeypatch.setattr(service, "code_fingerprint", lambda: next(prints, "v2"))
    monkeypatch.setattr(service, "new_code_healthy", lambda: (False, "SyntaxError"))
    monkeypatch.setattr(service, "CODE_CHECK_S", 0.0)
    monkeypatch.setattr(service, "restart_self", lambda: pytest.fail("перезапуск на сломанный код"))
    import threading
    threading.Timer(0.3, s.stop).start()
    s.run_forever(poll_s=0.01)


def test_install_unit_print(capsys, monkeypatch):
    from ahub import cli
    monkeypatch.setattr(sys, "platform", "linux")  # this test is about systemd; the plist is in test_service_cmd.py
    assert cli.main(["service", "install", "--print"]) == 0
    out = capsys.readouterr().out
    assert "Restart=always" in out and "StartLimitBurst" in out and "KillMode=process" in out
    assert "-m ahub service run" in out and "-m ahub bot run" in out and 'Environment="PATH=' in out


def test_orphan_counter_resets_after_episode(store, tmp_path):
    from ahub.engine import Engine
    from tests.enginekit import scout_ok
    project = make_project(tmp_path)
    install_fake(store, [scout_ok()])
    tid = _orphan_task(store, project)
    s, rec = svc(store, project, tmp_path)
    s.tick()  # orphan → queue (orphans=1)
    assert store.get_task(tid).limits["orphans"] == 1
    assert Engine(store, project, tid, sleep=lambda x: None).run().state is State.DONE
    assert "orphans" not in store.get_task(tid).limits
