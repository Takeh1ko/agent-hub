"""Accept decisions: merge, orchestrator edit, rollbacks, conflict, rework, allowed files, budget, model."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahub import accept, tasks
from ahub.engine import Engine
from ahub.model import Kind, State
from ahub.store import Store
from tests.enginekit import git, install_fake, make_project
from tests.test_engine_code import work


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def done_code(store, project, scenarios=None, **kw):
    fake = install_fake(store, scenarios or [work()])
    kw.setdefault("paths", ["core/**", "tests/**"])
    kw.setdefault("accept", ["tests/test_a.py::test_x"])
    kw.setdefault("review_level", 0)
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="сделать b", model="fake", **kw),
                     project, collect=False)
    res = Engine(store, project, t.id, sleep=lambda s: None).run()
    return store.get_task(t.id), res, fake


def root_log(project):
    return git_out(project.root, "log", "--oneline", "-3")


def git_out(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_merge_happy(store, project):
    t, res, _ = done_code(store, project)
    assert res.state is State.DONE
    msg = accept.accept(store, project, t.id)
    assert msg.startswith(f"T{t.id} слита в main")
    t2 = store.get_task(t.id)
    assert t2.state is State.ACCEPTED and t2.accepted_sha
    assert (Path(project.root) / "core" / "b.py").read_text() == "Y = 2\n"
    assert f"merge T{t.id}" in root_log(project)
    assert not Path(t.worktree).exists()
    assert t.branch not in git_out(project.root, "branch")
    assert (Path(project.root) / ".agent-hub" / "tasks" / f"T{t.id}" / "diff.patch").read_text().count("Y = 2")


def test_root_on_other_branch(store, project):
    t, _, _ = done_code(store, project)
    git(project.root, "checkout", "-q", "-b", "other")
    with pytest.raises(accept.DecisionError, match="на ветке other"):
        accept.accept(store, project, t.id)
    assert store.get_task(t.id).state is State.DONE  # refusal before the transition — task untouched


def test_conflict_aborts(store, project):
    t, _, _ = done_code(store, project)
    (Path(project.root) / "core" / "b.py").write_text("Y = 99\n")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "параллельная правка")
    with pytest.raises(accept.DecisionError, match="конфликт слияния: core/b.py"):
        accept.accept(store, project, t.id)
    assert store.get_task(t.id).state is State.NEEDS_DECISION
    assert git_out(project.root, "status", "--porcelain", "--untracked-files=no") == ""


def test_red_after_merge_rolls_back(store, project):
    t, _, _ = done_code(store, project)
    (Path(project.root) / "core" / "a.py").write_text("X = 5\n")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "сломали X")
    before = git_out(project.root, "rev-parse", "HEAD")
    with pytest.raises(accept.DecisionError, match="приёмка красная — слияние откачено"):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD") == before
    assert store.get_task(t.id).state is State.NEEDS_DECISION


def test_orchestrator_edit(store, project):
    t, _, _ = done_code(store, project)
    (Path(t.worktree) / "core" / "b.py").write_text("Y = 3\n")
    git(t.worktree, "commit", "-qam", "правка оркестратора")
    accept.accept(store, project, t.id)
    assert (Path(project.root) / "core" / "b.py").read_text() == "Y = 3\n"
    assert "orch_edit" in [e.kind for e in store.events(task_id=t.id)]


def test_uncommitted_orch_edit_refused(store, project):
    t, _, _ = done_code(store, project)
    (Path(t.worktree) / "core" / "b.py").write_text("Y = 4\n")
    with pytest.raises(accept.DecisionError, match="незакоммиченные"):
        accept.accept(store, project, t.id)


def test_extend_paths_then_accept(store, project):
    t, res, _ = done_code(store, project, scenarios=[work(path="docs/x.md", text="док\n")],
                          accept=["tests/test_a.py::test_x"])
    assert res.state is State.NEEDS_DECISION and "вне разрешённых" in res.reason
    with pytest.raises(accept.DecisionError, match="вне разрешённых проекту"):
        accept.extend_paths(store, project, t.id, ["/etc/**"])
    assert "docs/x.md" in accept.extend_paths(store, project, t.id, ["docs/x.md"])
    accept.accept(store, project, t.id)
    assert store.get_task(t.id).state is State.ACCEPTED


def test_budget_extend_continues(store, project):
    stop = {"session": "ses_x", "steps": [{"event": {"type": "text", "text": "сохранил"}}]}
    t, res, _ = done_code(store, project, scenarios=[work(cost=0.02), stop], budget_go=0.01, review_level=1)
    assert res.state is State.NEEDS_DECISION and res.reason.startswith("бюджет")
    msg = accept.extend_budget(store, t.id, add=1.0)
    assert "задача продолжена" in msg and store.get_task(t.id).state is State.QUEUED
    assert store.get_task(t.id).budget_go == pytest.approx(1.01)


def test_rework_same_session_with_notes(store, project):
    t, _, fake = done_code(store, project, scenarios=[work(), work(text="Y = 7\n")])
    accept.rework(store, t.id, "сделай Y = 7")
    t2 = store.get_task(t.id)
    assert t2.state is State.QUEUED and t2.round == 2
    assert Engine(store, project, t.id, sleep=lambda s: None).run().state is State.DONE
    assert "сделай Y = 7" in fake.calls[1]["prompt"] and fake.calls[1]["session_id"] == "ses_x"
    assert "rework_notes" not in store.get_task(t.id).limits


def test_model_change_fresh_session(store, project):
    t, _, fake = done_code(store, project, scenarios=[work(), work(session="ses_new", text="Y = 8\n")])
    accept.rework(store, t.id, "ещё раз")
    with pytest.raises(accept.DecisionError, match="нет модели"):
        accept.change_model(store, project, t.id, "nope")
    from ahub import registry
    registry.add_model(store, "fake2", "fake", "fake/model2")
    accept.change_model(store, project, t.id, "fake2")
    Engine(store, project, t.id, sleep=lambda s: None).run()
    # new model — new session with the full brief and the rework notes
    assert fake.calls[1]["session_id"] is None
    assert "ещё раз" in fake.calls[1]["prompt"] and "Allowed files" in fake.calls[1]["prompt"]


def test_edit_spec_new_session(store, project):
    from ahub import transitions
    t, _, fake = done_code(store, project, scenarios=[work(), work(session="ses_2", text="Y = 9\n")])
    transitions.move(store, t.id, State.QUEUED)  # "continue" on a finished task goes through rework/queue
    msg = accept.edit(store, project, t.id, spec="совсем другое")
    assert "новая сессия" in msg
    Engine(store, project, t.id, sleep=lambda s: None).run()
    assert fake.calls[1]["session_id"] is None and "совсем другое" in fake.calls[1]["prompt"]


def test_usd_budget_extend(store, project):
    t, _, _ = done_code(store, project)
    from ahub import transitions
    transitions.move(store, t.id, State.QUEUED)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, t.id, st)
    store.add_event("budget_hard", task_id=t.id, project=t.project, payload={})  # as the engine does on stop
    transitions.move(store, t.id, State.NEEDS_DECISION, reason="бюджет исчерпан ($0.000 из $1.5)")
    msg = accept.extend_budget(store, t.id, add_usd=0.5)
    assert "реальные $0 → $0.5" in msg and "продолжена" in msg
    assert store.get_task(t.id).budget_usd == 0.5
    with pytest.raises(accept.DecisionError):
        accept.extend_budget(store, t.id)


def test_red_after_merge_root_moved_not_reset(store, project, monkeypatch):
    t, _, _ = done_code(store, project)
    (Path(project.root) / "core" / "a.py").write_text("X = 5\n")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "сломали X")
    from ahub import gates as g

    def red_and_foreign_commit(project_, cwd, nodes, **kw):
        (Path(cwd) / "foreign.txt").write_text("чужое\n")
        git(cwd, "add", "foreign.txt")
        git(cwd, "commit", "-q", "-m", "чужой коммит во время приёмки")
        return False, "FAILED", "pytest"

    monkeypatch.setattr(g, "run_acceptance", red_and_foreign_commit)
    with pytest.raises(accept.DecisionError, match="корень уехал"):
        accept.accept(store, project, t.id)
    assert (Path(project.root) / "foreign.txt").exists()  # the foreign commit is left alone


def test_budget_extend_does_not_resume_other_decision(store, project):
    t, _, _ = done_code(store, project)
    from ahub import transitions
    transitions.move(store, t.id, State.QUEUED)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, t.id, st)
    transitions.move(store, t.id, State.NEEDS_DECISION, reason="бюджет и круги ревью кончились")  # text does not matter
    accept.extend_budget(store, t.id, add=1.0)
    assert store.get_task(t.id).state is State.NEEDS_DECISION
