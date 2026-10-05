"""Task branches sync with the work branch before the gates (each round)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ahub import gates, tasks, workspace
from ahub.engine import Engine
from ahub.model import Kind, State
from ahub.store import Store
from tests.enginekit import git, install_fake, make_project


def _code_task(store: Store, project, **kw):
    kw.setdefault("paths", ["core/**", "tests/**"])
    kw.setdefault("accept", ["tests/test_a.py::test_x"])
    kw.setdefault("review_level", 0)
    kind = kw.pop("kind", Kind.CODE)
    return tasks.create(store, tasks.TaskSpec(project="P", kind=kind, title="sync", model="fake", **kw),
                        project, collect=False)


def _work(path="core/b.py", text="Y = 2\n", session="ses_x"):
    return {"session": session, "steps": [
        {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
        {"write": {"path": path, "text": text}},
        {"git_commit": "feat: work"},
        {"result": {"summary": "did", "files": [path]}},
        {"event": {"type": "text", "text": "done"}}]}


def _main_move(root: Path, path: str, text: str, msg: str = "main moves") -> None:
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


def _run(store: Store, project, tid: int):
    return Engine(store, project, tid, sleep=lambda s: None).run()


def test_clean_merge_syncs_and_passes_gates(tmp_path):
    project = make_project(tmp_path)
    store = Store()
    fake = install_fake(store, [_work()])
    t = _code_task(store, project)
    workspace.ensure(project, t.id)  # worktree from the old base
    _main_move(Path(project.root), "core/c.py", "C = 1\n")
    res = _run(store, project, t.id)
    assert res.state is State.DONE, res.reason
    assert len(fake.calls) == 1  # no resolve turn needed
    wt = Path(store.get_task(t.id).worktree)
    assert (wt / "core" / "c.py").read_text() == "C = 1\n"  # main is in the task branch
    assert (wt / "core" / "b.py").read_text() == "Y = 2\n"
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "main", "HEAD"],
                       cwd=str(wt), capture_output=True)
    assert r.returncode == 0  # synced
    g = gates.check(project, store.get_task(t.id))
    assert not g.fatal and not g.repairable
    assert "core/c.py" not in g.diff_files  # main-only file is not the task's


def test_conflict_triggers_resolve_turn(tmp_path):
    project = make_project(tmp_path)
    store = Store()
    resolve = {"session": "ses_x", "steps": [
        {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
        {"merge": "main"},
        {"write": {"path": "core/b.py", "text": "MAIN\nTASK\n"}},
        {"git_commit": "resolve: keep both"},
        {"result": {"summary": "resolved", "files": ["core/b.py"]}},
        {"event": {"type": "text", "text": "done"}}]}
    fake = install_fake(store, [_work(path="core/b.py", text="TASK\n"), resolve])
    t = _code_task(store, project)
    workspace.ensure(project, t.id)
    _main_move(Path(project.root), "core/b.py", "MAIN\n")
    res = _run(store, project, t.id)
    assert res.state is State.DONE, res.reason
    assert len(fake.calls) == 2  # work + resolve
    assert "main" in fake.calls[1]["prompt"] and "Merge" in fake.calls[1]["prompt"]
    assert fake.calls[1]["session_id"] == "ses_x"  # same session
    wt = Path(store.get_task(t.id).worktree)
    body = (wt / "core" / "b.py").read_text()
    assert "MAIN" in body and "TASK" in body
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "main", "HEAD"],
                       cwd=str(wt), capture_output=True)
    assert r.returncode == 0


def test_allowed_paths_ignore_main_only_files(tmp_path):
    project = make_project(tmp_path)
    store = Store()
    fake = install_fake(store, [_work()])
    t = _code_task(store, project, paths=["core/**", "tests/**"])
    workspace.ensure(project, t.id)
    _main_move(Path(project.root), "docs/only_main.md", "# main\n")
    res = _run(store, project, t.id)
    assert res.state is State.DONE, res.reason
    assert len(fake.calls) == 1
    wt = Path(store.get_task(t.id).worktree)
    assert (wt / "docs" / "only_main.md").read_text() == "# main\n"
    g = gates.check(project, store.get_task(t.id))
    assert not g.fatal, g.fatal
    assert "docs/only_main.md" not in g.diff_files
