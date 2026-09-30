"""Сервис: живые процессы из /proc, очередь, места, ресурсы, зависимости, пауза; сквозной запуск процесса задачи."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ahub import paths, service, tasks, transitions
from ahub.model import Kind, State
from ahub.store import Store
from tests.v2.enginekit import install_fake, make_project, scout_ok


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
    fake_proc(root, 101, ["python", "-m", "ahub.worker", "8"], state="Z")  # зомби — не жив
    fake_proc(root, 102, ["python", "-m", "hub.commands.queue", "--run-one", "T9"])  # v1 — не наш
    fake_proc(root, 103, ["bash"])
    (root / "self").mkdir()
    assert service.live_workers(root) == {7: 100}


def scout(store, project, **kw):
    return tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="x", model="fake", **kw),
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
    assert store.get_task(ids[2]).state_reason == "ждёт места (2/2)"
    s.tick()  # запущенные ещё не видны в /proc — не запускать повторно
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
    assert "ждёт места" in store.get_task(new.id).state_reason


def test_dependencies(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    a = scout(store, project)
    transitions.move(store, a.id, State.STOPPED)  # не принята
    b = scout(store, project, after=[a.id])
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert rec.spawned == [] and "ждёт принятия" in store.get_task(b.id).state_reason
    transitions.move(store, a.id, State.QUEUED)
    for st in (State.PREPARING, State.WORKING, State.DONE, State.ACCEPTED):
        transitions.move(store, a.id, st)
    s.tick()
    assert rec.spawned == [b.id] and store.get_task(b.id).state_reason == ""


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
    assert store.get_task(b.id).state_reason == "ждёт ресурс api"
    assert store.get_task(c.id).state_reason == "ждёт ресурс db (занят вне хаба)"
    busy["v"] = False
    s.tick()
    assert rec.spawned == [a.id, c.id]


def test_pause(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    t = scout(store, project)
    s, rec = svc(store, project, tmp_path)
    store.meta_set(service.PAUSE_KEY, "1")
    r = s.tick()
    assert r.paused and rec.spawned == [] and store.get_task(t.id).state_reason == "очередь на паузе"
    store.meta_del(service.PAUSE_KEY)
    s.tick()
    assert rec.spawned == [t.id]
    assert store.meta_get(service.HEARTBEAT_KEY)


def test_external_lock_busy(tmp_path):
    import fcntl
    import os
    p = tmp_path / "x.lock"
    assert not service.external_lock_busy(str(p))  # нет файла — свободен
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
    """Сервис запускает настоящий процесс задачи; тот ведёт разведку на фейковом поставщике до «Готово»."""
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
    install_fake(store, [])  # модель «fake» в реестре общей базы
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
    assert final.state is State.DONE, (final.state_reason, log.read_text() if log.exists() else "")
    assert store.list_sessions(t.id)[0].external_id == "ses_e2e"


def _orphan_task(store, project, state=State.WORKING, lease_age_ms=10 * 60_000):
    from ahub.time import now_ms
    t = scout(store, project)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, t.id, st)
    if state is State.ACCEPTING:
        transitions.move(store, t.id, State.DONE)
        transitions.move(store, t.id, State.ACCEPTING)
    old = now_ms() - lease_age_ms
    with store.tx() as c:
        c.execute("UPDATE task SET owner='dead', owner_pid=999999, lease_until=?, updated_at=? WHERE id=?",
                  (old, old, t.id))
    return t.id


def test_orphan_requeued_once_then_decision(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project)
    s, rec = svc(store, project, tmp_path)
    s.tick()
    t = store.get_task(tid)
    assert rec.spawned == [tid] and t.limits["orphans"] == 1  # вернули в очередь и сразу запустили
    assert "orphan" in [e.kind for e in store.events(task_id=tid)]
    # процесс снова пропал, задача опять активна без аренды
    s.recent.clear()
    with store.tx() as c:
        c.execute("UPDATE task SET state='working', owner='', lease_until=NULL, updated_at=0 WHERE id=?", (tid,))
    s.tick()
    assert store.get_task(tid).state is State.NEEDS_DECISION and "повторно" in store.get_task(tid).state_reason


def test_orphan_live_lease_untouched(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project, lease_age_ms=-60_000)  # аренда ещё жива (CLI-владелец)
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert store.get_task(tid).state is State.WORKING and rec.spawned == []


def test_orphan_accepting_is_decision(store, tmp_path):
    project = make_project(tmp_path)
    install_fake(store, [])
    tid = _orphan_task(store, project, state=State.ACCEPTING)
    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert store.get_task(tid).state is State.NEEDS_DECISION and "принятие прервано" in store.get_task(tid).state_reason
