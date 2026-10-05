"""Accept decisions: merge, orchestrator edit, rollbacks, conflict, rework, allowed files, budget, model."""

from __future__ import annotations

from pathlib import Path

import pytest

from ahub import accept, reasons, tasks, views
from ahub.engine import Engine
from ahub.model import Kind, State
from ahub.store import Store
from tests.conftest import wait_until
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


def interrupted_accept(store, project, tmp_path, tid):
    """The accept process dies right after the merge commit; the service sees the task as an orphan."""
    from ahub import service, transitions
    from ahub.engine import owner_token
    from ahub.time import now_ms

    t = store.get_task(tid)
    transitions.move(store, tid, State.ACCEPTING, reason="приёмка")
    assert transitions.acquire(store, tid, owner_token(), pid=999999)
    git(project.root, "merge", "--no-ff", "-m", f"merge {t.label}: сделать b", t.branch)
    old = now_ms() - 10 * 60_000  # the dead process took the lease with it
    with store.tx() as c:
        c.execute("UPDATE task SET owner='dead', owner_pid=999999, lease_until=?, updated_at=? WHERE id=?",
                  (old, old, tid))
    (tmp_path / "proc").mkdir(exist_ok=True)
    service.Service(store, [project], spawn=lambda i: 1, proc_root=tmp_path / "proc",
                    lock_busy=lambda p: False).tick()
    after = store.get_task(tid)
    # the reason is a code (ahub.reasons); the sentence with the way out is rendered at read time
    assert after.state is State.NEEDS_DECISION
    assert after.state_reason == '{"code":"orphan_accepting","label":"T1"}'
    assert f"ahub accept {t.label}" in reasons.text(after.state_reason)
    return after


def test_merge_happy(store, project):
    t, res, _ = done_code(store, project)
    assert res.state is State.DONE
    msg = accept.accept(store, project, t.id)
    assert msg.startswith(f"T{t.id} слита в main")
    t2 = store.get_task(t.id)
    assert t2.state is State.ACCEPTED and t2.accepted_sha
    # the reason is a code + params; no push configured here, so the note is empty — and it still reads
    assert t2.state_reason == '{"code":"merged","branch":"main","note":""}'
    assert reasons.text(t2.state_reason) == "слита в main"
    assert views.task_text(store, t2, now=0, w=100).count("слита в main") == 1
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
    with pytest.raises(accept.DecisionError, match="приёмка красная — ничего не слито"):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD") == before
    assert store.get_task(t.id).state is State.NEEDS_DECISION
    assert '"accept_red"' in store.get_task(t.id).state_reason
    assert not (Path(project.root) / "core" / "b.py").exists()  # never merged
    from ahub.accept import _verify_branch, _verify_path

    assert not _verify_path(project, store.get_task(t.id)).exists()  # temp cleaned
    assert _verify_branch(project, store.get_task(t.id)) not in git_out(project.root, "branch")


def test_orchestrator_edit(store, project):
    t, _, _ = done_code(store, project)
    (Path(t.worktree) / "core" / "b.py").write_text("Y = 3\n")
    git(t.worktree, "commit", "-qam", "правка оркестратора")
    accept.accept(store, project, t.id)
    assert (Path(project.root) / "core" / "b.py").read_text() == "Y = 3\n"
    assert "orch_edit" in [e.kind for e in store.events(task_id=t.id)]


@pytest.mark.parametrize("code", ["ru", "en"])
def test_orchestrator_edit_is_decided_by_the_problem_code(store, project, monkeypatch, code):
    """The result.json problem is recognised by its code — the wording of the language must not matter."""
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", code)
    _reset()
    t, _, _ = done_code(store, project)
    (Path(t.worktree) / "core" / "b.py").write_text("Y = 5\n")
    git(t.worktree, "commit", "-qam", "правка оркестратора")
    accept.accept(store, project, t.id)  # HEAD is not the commit in result.json — allowed after an edit
    assert store.get_task(t.id).state is State.ACCEPTED


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


def test_edit_refusals(store, project):
    """Refuse bad edit parameters: rounds out of bounds, an empty panel, an unknown model alias."""
    install_fake(store, [])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="x", model="fake",
                                           paths=["core/**"], accept=["tests/test_a.py::test_x"],
                                           review_models=["fake"], review_rounds=1), project, collect=False)
    with pytest.raises(accept.DecisionError, match="круги 99: допустимо 1-"):
        accept.edit(store, project, t.id, rounds=99)
    with pytest.raises(accept.DecisionError, match="круги 0: допустимо 1-"):
        accept.edit(store, project, t.id, rounds=0)
    with pytest.raises(accept.DecisionError, match="--review хотя бы с одной моделью"):
        accept.edit(store, project, t.id, review=[])
    with pytest.raises(accept.DecisionError, match="нет модели 'nonexistent_model'"):
        accept.edit(store, project, t.id, review=["nonexistent_model"])
    with pytest.raises(accept.DecisionError, match="нет модели 'nonexistent_model'"):
        accept.edit(store, project, t.id, model="nonexistent_model")


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


def test_usd_budget_lowered_but_not_below_the_spend(store, project):
    sc = work()
    sc["steps"][0]["event"]["usd"] = 0.04  # real money of the run
    t, _, _ = done_code(store, project, scenarios=[sc], budget_usd=0.5)
    msg = accept.extend_budget(store, t.id, set_usd=0.05)  # lowering is allowed
    assert "реальные $0.5 → $0.05" in msg and store.get_task(t.id).budget_usd == 0.05
    with pytest.raises(accept.DecisionError, match=r"ниже потраченных \$0\.040"):
        accept.extend_budget(store, t.id, set_usd=0.01)
    assert store.get_task(t.id).budget_usd == 0.05  # the refusal writes nothing


def test_red_after_merge_root_moved_not_reset(store, project, monkeypatch):
    """The work branch moved during a green acceptance — refuse, leave the foreign commit alone."""
    t, _, _ = done_code(store, project)
    from ahub import gates as g

    def green_and_foreign_commit(project_, cwd, nodes, **kw):
        (Path(project_.root) / "foreign.txt").write_text("чужое\n")
        git(project_.root, "add", "foreign.txt")
        git(project_.root, "commit", "-q", "-m", "чужой коммит во время приёмки")
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", green_and_foreign_commit)
    with pytest.raises(accept.DecisionError, match="уехала во время приёмки"):
        accept.accept(store, project, t.id)
    assert (Path(project.root) / "foreign.txt").exists()  # the foreign commit is left alone
    assert not (Path(project.root) / "core" / "b.py").exists()  # the task files never landed
    assert '"work_moved"' in store.get_task(t.id).state_reason


def test_budget_extend_does_not_resume_other_decision(store, project):
    t, _, _ = done_code(store, project)
    from ahub import transitions
    transitions.move(store, t.id, State.QUEUED)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, t.id, st)
    transitions.move(store, t.id, State.NEEDS_DECISION, reason="бюджет и круги ревью кончились")  # text does not matter
    accept.extend_budget(store, t.id, add=1.0)
    assert store.get_task(t.id).state is State.NEEDS_DECISION


def test_accept_finishes_an_interrupted_merge(store, project, tmp_path):
    """The merge is in the work branch, the accept process died: accept skips the gates and the merge."""
    t, _, _ = done_code(store, project)
    interrupted_accept(store, project, tmp_path, t.id)
    merged = git_out(project.root, "rev-parse", "HEAD").strip()
    msg = accept.accept(store, project, t.id)
    assert msg.startswith(f"T{t.id} слита в main")
    after = store.get_task(t.id)
    assert after.state is State.ACCEPTED and after.accepted_sha == merged
    assert git_out(project.root, "rev-parse", "HEAD").strip() == merged  # no second merge
    assert (Path(project.root) / "core" / "b.py").read_text() == "Y = 2\n"
    assert not Path(after.worktree).exists() and t.branch not in git_out(project.root, "branch")
    assert (Path(project.root) / ".agent-hub" / "tasks" / f"T{t.id}" / "diff.patch").exists()


def test_resumed_accept_rolls_back_a_red_merge(store, project, tmp_path):
    """Red on HEAD after an interrupted merge — the merge commit is rolled back, as in a normal accept."""
    t, _, _ = done_code(store, project)
    (Path(project.root) / "core" / "a.py").write_text("X = 5\n")  # a foreign commit in the work branch
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "сломали X")
    foreign = git_out(project.root, "rev-parse", "HEAD").strip()
    interrupted_accept(store, project, tmp_path, t.id)
    with pytest.raises(accept.DecisionError, match="приёмка красная — слияние откачено"):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD").strip() == foreign  # the merge is gone
    assert store.get_task(t.id).state is State.NEEDS_DECISION


def test_resumed_accept_refuses_foreign_commit_on_top(store, project, tmp_path):
    """Foreign commit on top of the merge — accept refuses and does not rollback foreign commit."""
    t, _, _ = done_code(store, project)
    interrupted_accept(store, project, tmp_path, t.id)
    (Path(project.root) / "core" / "c.py").write_text("Z = 1\n")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "чужой коммит поверх слияния")
    foreign_head = git_out(project.root, "rev-parse", "HEAD").strip()
    with pytest.raises(accept.DecisionError):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD").strip() == foreign_head


def test_resumed_accept_refuses_a_copy_that_moved(store, project, tmp_path):
    """The copy is not on the task branch tip any more — accept refuses instead of skipping the gates.

    The work branch still carries accept's merge commit, but the copy has its own commit on top: what the
    gates would have checked is not what is in the branch.
    """
    t, _, _ = done_code(store, project)
    interrupted_accept(store, project, tmp_path, t.id)
    merged = git_out(project.root, "rev-parse", "HEAD").strip()
    copy = Path(t.worktree)
    git(copy, "commit", "--allow-empty", "-q", "-m", "правка в копии после слияния")
    moved = git_out(copy, "rev-parse", "HEAD").strip()
    git(copy, "checkout", "-q", "--detach")  # the copy is left on its own commit
    git(project.root, "update-ref", f"refs/heads/{t.branch}", f"{moved}~1")  # the branch stays where it was
    with pytest.raises(accept.DecisionError, match="уже слита в main без приёмки"):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD").strip() == merged  # nothing moved in the work branch
    after = store.get_task(t.id)
    assert after.state is State.NEEDS_DECISION and '"already_merged"' in after.state_reason


def test_accept_refuses_branch_already_merged_without_acceptance(store, project, monkeypatch):
    """Orchestrator hand-merges the task branch into main: accept refuses already_merged."""
    from ahub import gates as g
    t, _, _ = done_code(store, project)
    git(project.root, "merge", "--ff-only", t.branch)  # fast-forward merge (not accept's merge commit)
    monkeypatch.setattr(g, "check", lambda *a, **kw: g.GateResult(base="b", head="h"))
    with pytest.raises(accept.DecisionError, match="уже слита в main без приёмки"):
        accept.accept(store, project, t.id)


def test_accept_refuses_a_hand_merge_with_the_own_parents(store, project, monkeypatch):
    """A hand `--no-ff` merge of the same branch — the parents of accept's merge, another subject.

    Only accept's own merge commit (`merge T<n>: …`) is the state of an interrupted accept: a foreign one is
    refused, so the orchestrator finishes it by hand.
    """
    from ahub import gates as g
    t, _, _ = done_code(store, project)
    git(project.root, "merge", "--no-ff", "-m", "слил руками", t.branch)  # the same parents accept would write
    monkeypatch.setattr(g, "check", lambda *a, **kw: g.GateResult(base="b", head="h"))
    with pytest.raises(accept.DecisionError, match="уже слита в main без приёмки"):
        accept.accept(store, project, t.id)
    assert '"already_merged"' in store.get_task(t.id).state_reason


def test_rollback_keeps_a_local_edit(store, project, monkeypatch):
    """Verify-before-move: a local edit in the root survives a red acceptance (nothing was ever merged).

    An untracked file survives both, so it cannot tell the two apart — this edit can.
    """
    from ahub import gates as g
    t, _, _ = done_code(store, project)
    before = git_out(project.root, "rev-parse", "HEAD").strip()
    edited = Path(project.root) / "core" / "a.py"  # the task touches core/b.py, this file the merge does not

    def red_with_a_local_edit(project_, cwd, nodes, **kw):
        edited.write_text(edited.read_text() + "# правка человека во время приёмки\n")
        return False, "FAILED", "pytest"

    monkeypatch.setattr(g, "run_acceptance", red_with_a_local_edit)
    with pytest.raises(accept.DecisionError, match="приёмка красная — ничего не слито"):
        accept.accept(store, project, t.id)
    assert git_out(project.root, "rev-parse", "HEAD").strip() == before  # nothing was merged
    assert edited.read_text() == "X = 1\n# правка человека во время приёмки\n"  # the edit is not thrown away
    assert not (Path(project.root) / "core" / "b.py").exists()  # the task files never landed in main


def test_rollback_refuses_to_destroy_a_local_edit(store, project, monkeypatch):
    """Verify-before-move: no rollback is attempted, so a local edit of a file the task touches is safe.

    Red acceptance leaves the work branch tip unchanged and the edit untouched; the task files never land.
    """
    from ahub import gates as g
    t, _, _ = done_code(store, project, [work(path="core/a.py", text="Y = 9\n")])
    before = git_out(project.root, "rev-parse", "HEAD").strip()
    tracked = Path(project.root) / "core" / "a.py"

    def red_with_a_local_edit(project_, cwd, nodes, **kw):
        tracked.write_text(tracked.read_text() + "# правка человека во время приёмки\n")
        return False, "FAILED", "pytest"

    monkeypatch.setattr(g, "run_acceptance", red_with_a_local_edit)
    with pytest.raises(accept.DecisionError, match="приёмка красная — ничего не слито"):
        accept.accept(store, project, t.id)
    assert "# правка человека" in tracked.read_text()  # the edit is untouched
    assert git_out(project.root, "rev-parse", "HEAD").strip() == before  # nothing was merged
    assert len(git_out(project.root, "log", "-1", "--format=%P", "HEAD").split()) != 2  # no merge commit
    t2 = store.get_task(t.id)
    assert t2.state is State.NEEDS_DECISION and '"accept_red"' in t2.state_reason


def test_not_merged_still_needs_the_copy(store, project):
    t, _, _ = done_code(store, project)
    import shutil

    shutil.rmtree(t.worktree)
    with pytest.raises(accept.DecisionError, match="нет копии задачи"):
        accept.accept(store, project, t.id)


def test_a_copy_that_is_not_a_git_worktree(store, project, tmp_path):
    """Something that is not a worktree took the place of the copy: one line, not a git traceback.

    The copy is read when the branch carries accept's own merge — then it must still be the branch tip.
    """
    import shutil

    t, _, _ = done_code(store, project)
    interrupted_accept(store, project, tmp_path, t.id)
    shutil.rmtree(t.worktree)
    Path(t.worktree).mkdir()
    with pytest.raises(accept.DecisionError, match="нет копии задачи"):
        accept.accept(store, project, t.id)
    assert store.get_task(t.id).state is State.NEEDS_DECISION  # refused before the transition — task untouched


def test_accept_renews_the_lease_during_acceptance(store, project, monkeypatch):
    """Acceptance outlives the lease — the accept keeps the lease alive (engine-style keeper)."""
    from ahub import gates as g

    t, _, _ = done_code(store, project)
    leases = []

    def slow(project_, cwd, nodes, **kw):
        first = store.get_task(t.id).lease_until
        # the keeper renews in a thread of its own — wait for the lease to move, a pause is not a promise
        wait_until(lambda: (store.get_task(t.id).lease_until or 0) > (first or 0))
        leases.append((first, store.get_task(t.id).lease_until))
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", slow)
    monkeypatch.setattr(accept, "RENEW_S", 0.05)
    monkeypatch.setattr(accept, "ACCEPT_LEASE_MS", 200)
    accept.accept(store, project, t.id)
    assert leases[0][1] > leases[0][0]  # the lease moved forward while the tests ran
    assert store.get_task(t.id).state is State.ACCEPTED


def test_slow_acceptance_is_not_an_orphan(store, project, tmp_path, monkeypatch):
    """A long acceptance with a stale lease and this process as the owner: the service leaves the accept alone."""
    import os

    from ahub import gates as g
    from ahub import service
    from ahub.time import now_ms
    from tests.test_service import fake_proc

    t, _, _ = done_code(store, project)
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    fake_proc(root, os.getpid(), ["python", "-m", "ahub", "accept", f"T{t.id}"])  # the accept process, not a worker
    seen = []

    def slow(project_, cwd, nodes, **kw):
        # acceptance longer than the lease + grace: the lease on the row is stale
        old = now_ms() - 10 * 60_000
        with store.tx() as c:
            c.execute("UPDATE task SET lease_until=?, updated_at=? WHERE id=?", (old, old, t.id))
        service.Service(store, [project], spawn=lambda i: 1, proc_root=root,
                        lock_busy=lambda p: False).tick()
        cur = store.get_task(t.id)
        seen.append((cur.state, cur.owner_pid))
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", slow)
    accept.accept(store, project, t.id)
    assert seen == [(State.ACCEPTING, os.getpid())]  # no orphan event, no "acceptance interrupted"
    assert store.get_task(t.id).state is State.ACCEPTED
    assert "orphan" not in [e.kind for e in store.events(task_id=t.id)]


def two_done_tasks(store, project, b_text="B = 1\n", c_text="C = 1\n"):
    """Two code tasks of one project, both done, each touching its own file."""
    t1, res1, _ = done_code(store, project, scenarios=[work(path="core/b.py", text=b_text)],
                            paths=["core/**", "tests/**"], accept=["tests/test_a.py::test_x"])
    t2, res2, _ = done_code(store, project, scenarios=[work(path="core/c.py", text=c_text)],
                            paths=["core/**", "tests/**"], accept=["tests/test_a.py::test_x"])
    assert res1.state is State.DONE and res2.state is State.DONE
    return t1, t2


def test_two_accepts_started_together_merge_one_after_another(store, project, monkeypatch):
    """Two accepts entered at the same moment merge one after another — each acceptance runs on its own merge."""
    import threading
    import time

    from ahub import gates as g

    t1, t2 = two_done_tasks(store, project)
    own = {f"T{t1.id}": "core/b.py", f"T{t2.id}": "core/c.py"}
    runs = []  # one entry per acceptance run: the merge it ran on and whether the root moved under it
    together = threading.Barrier(2)  # the two accepts enter accept() at the same moment

    def tracking_run_acceptance(project_, cwd, nodes, **kw):
        run = {"label": kw.get("task_label", ""), "start": time.monotonic(),
               "head": git_out(cwd, "rev-parse", "HEAD").strip(),
               "files": sorted(f for f in own.values() if (Path(cwd) / f).exists())}
        time.sleep(0.5)  # the lock is held for a while — a second accept has to wait it out
        run["end"] = time.monotonic()
        run["head_after"] = git_out(cwd, "rev-parse", "HEAD").strip()
        run["subject"] = git_out(cwd, "log", "-1", "--format=%s").strip()
        runs.append(run)
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", tracking_run_acceptance)
    errors = []

    def run_accept(tid):
        try:
            together.wait(timeout=10)
            accept.accept(store, project, tid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run_accept, args=(tid,)) for tid in (t1.id, t2.id)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    assert not errors, errors
    assert [store.get_task(t.id).state for t in (t1, t2)] == [State.ACCEPTED, State.ACCEPTED]

    first, second = sorted(runs, key=lambda r: r["start"])  # the two runs are one after another
    assert first["end"] <= second["start"]  # never interleaved
    assert first["label"] != second["label"] and set(r["label"] for r in runs) == set(own)
    for run in runs:
        assert run["subject"].startswith(f"merge {run['label']}:"), run  # its own merge commit
        assert run["head_after"] == run["head"], run  # and no other merge landed under the running tests
        assert store.get_task(int(run["label"][1:])).accepted_sha == run["head"]
    assert first["files"] == [own[first["label"]]]  # the first acceptance saw its own merge only
    assert second["files"] == sorted(own.values())  # the second one saw both


@pytest.mark.parametrize("tty", [True, False])
def test_second_accept_waits_and_names_the_holder_on_a_tty(store, project, monkeypatch, tty, caplog):
    """A second accept waits for the project lock; on a terminal it names the accept it waits for."""
    import io
    import logging
    import sys
    import threading

    from ahub import gates as g
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()

    t1, t2 = two_done_tasks(store, project, b_text="B = 2\n", c_text="C = 2\n")
    first_in = threading.Event()
    heads = []
    line = f"waiting for the accept of T{t1.id}…\n"
    wait_msg = f"accept T{t2.id}: waiting for the project accept lock"
    out = io.StringIO()
    out.isatty = lambda: tty  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="ahub.accept")

    def waiting() -> bool:
        return any(wait_msg in r.getMessage() for r in caplog.records)

    def slow_acceptance(project_, cwd, nodes, **kw):
        if kw.get("task_label") == f"T{t1.id}":
            head = git_out(cwd, "rev-parse", "HEAD").strip()
            first_in.set()
            # Hold the lock until the second accept is seen waiting on it: under load it may
            # take a while to reach the lock, and a fixed sleep would release too early.
            # Bounded below the join timeout below, so a stuck waiter fails fast.
            wait_until(waiting, timeout=20)
            heads.append((head, git_out(cwd, "rev-parse", "HEAD").strip()))
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", slow_acceptance)
    monkeypatch.setattr(sys, "stdout", out)

    threads = [threading.Thread(target=accept.accept, args=(store, project, tid)) for tid in (t1.id, t2.id)]
    threads[0].start()
    assert first_in.wait(timeout=10), "the first accept did not reach the acceptance"
    threads[1].start()  # the second one has to wait for the lock the first one holds
    for th in threads:
        th.join(timeout=30)

    assert [store.get_task(t.id).state for t in (t1, t2)] == [State.ACCEPTED, State.ACCEPTED]
    assert heads and heads[0][0] == heads[0][1]  # nothing merged into the root while the tests ran
    assert waiting()  # the second accept really contended for the lock (without the flock it never waits)
    if tty:
        assert out.getvalue().count(line) == 1  # one line, naming the holder, printed once
    else:
        assert out.getvalue() == ""  # in a pipe the accept output is unchanged


def test_accept_waiting_for_the_lock_keeps_the_lease(store, project, monkeypatch):
    """A long wait for the project lock is not an expired lease — the service must not call the waiter an orphan."""
    import threading
    import time

    from ahub import gates as g
    from ahub import transitions
    from ahub.time import now_ms

    # Load-safe margins: under parallel suites a keeper thread can stall past a
    # millisecond-scale lease even while working — the property under test (the
    # lease advances while waiting for the flock) is scale-invariant.
    lease_ms = 2000
    t1, t2 = two_done_tasks(store, project, b_text="B = 3\n", c_text="C = 3\n")
    first_in = threading.Event()
    seen = []

    def slow_acceptance(project_, cwd, nodes, **kw):
        if kw.get("task_label") == f"T{t1.id}":
            first_in.set()
            claimed = wait_until(lambda: store.get_task(t2.id).lease_until)  # the waiter is queued with a lease
            assert claimed, "T2 never acquired a lease while waiting for the project lock"
            # the wait is longer than the lease below — the keeper of the waiter has to move it past that
            wait_until(lambda: (store.get_task(t2.id).lease_until or 0) > (claimed or 0) + lease_ms)
            # the keeper must keep the lease valid while T2 waits: poll for a fresh snapshot —
            # one delayed sample under parallel-suite load is scheduling jitter, not a dead keeper
            waiter = store.get_task(t2.id)
            deadline = time.monotonic() + 5
            while transitions.is_orphan(waiter, now_ms()) and time.monotonic() < deadline:
                time.sleep(0.05)
                waiter = store.get_task(t2.id)
            seen.append((waiter, transitions.is_orphan(waiter, now_ms())))
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", slow_acceptance)
    monkeypatch.setattr(accept, "RENEW_S", 0.05)
    monkeypatch.setattr(accept, "ACCEPT_LEASE_MS", lease_ms)  # without renewal it would be gone before the wait is over
    threads = [threading.Thread(target=accept.accept, args=(store, project, tid)) for tid in (t1.id, t2.id)]
    threads[0].start()
    assert first_in.wait(timeout=30)
    threads[1].start()
    for th in threads:
        th.join(timeout=60)

    assert store.get_task(t2.id).state is State.ACCEPTED
    waiter, orphan = seen[0]
    assert waiter.state is State.ACCEPTING  # claimed and queued, its merge is not in the root yet
    assert not orphan  # the lease keeper runs while it waits


def test_second_accept_merges_after_the_first_rolled_back(store, project, monkeypatch):
    """The first accept goes red and rolls its merge back — the waiting second one then merges onto a clean root."""
    import threading
    import time

    from ahub import gates as g

    t1, t2 = two_done_tasks(store, project, b_text="B = 4\n", c_text="C = 4\n")
    first_in = threading.Event()

    def red_first_acceptance(project_, cwd, nodes, **kw):
        if kw.get("task_label") == f"T{t1.id}":
            first_in.set()
            time.sleep(0.4)
            return False, "FAILED tests/test_a.py", "pytest"
        return True, "", "pytest"

    monkeypatch.setattr(g, "run_acceptance", red_first_acceptance)

    def run_first():
        with pytest.raises(accept.DecisionError, match="приёмка красная"):
            accept.accept(store, project, t1.id)

    threads = [threading.Thread(target=run_first),
               threading.Thread(target=accept.accept, args=(store, project, t2.id))]
    threads[0].start()
    assert first_in.wait(timeout=10)
    threads[1].start()
    for th in threads:
        th.join(timeout=30)

    assert store.get_task(t1.id).state is State.NEEDS_DECISION
    assert store.get_task(t2.id).state is State.ACCEPTED
    assert not (Path(project.root) / "core" / "b.py").exists()  # the red merge was rolled back
    assert (Path(project.root) / "core" / "c.py").read_text() == "C = 4\n"
    assert git_out(project.root, "status", "--porcelain", "--untracked-files=no") == ""
