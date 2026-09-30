"""Движок на задачах «код» и «рутина»: ворота, repair, приёмка, панель ревью, круги, бюджет, подготовка."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import tasks
from ahub.engine import Engine
from ahub.model import Kind, State
from ahub.store import Store
from tests.v2.enginekit import git, install_fake, make_project


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def code_task(store, project, **kw):
    kw.setdefault("paths", ["core/**", "tests/**"])
    kw.setdefault("accept", ["tests/test_a.py::test_x"])
    kw.setdefault("review_level", 0)
    kind = kw.pop("kind", Kind.CODE)
    return tasks.create(store, tasks.TaskSpec(project="P", kind=kind, title="починить", model="fake", **kw),
                        project, collect=False)


def run(store, project, tid):
    return Engine(store, project, tid, sleep=lambda s: None).run()


def work(session="ses_x", text="Y = 2\n", path="core/b.py", cost=0.01, files=None):
    return {"session": session, "steps": [
        {"event": {"type": "usage", "in": 100, "out": 10, "go": cost}},
        {"write": {"path": path, "text": text}},
        {"git_commit": "feat: сделал"},
        {"result": {"summary": "сделал", "files": files if files is not None else [path]}},
        {"event": {"type": "text", "text": "готово"}}]}


def verdict(round_no=1, v="approve", findings=None, model="fake", session="ses_rev"):
    body = {"verdict": v, "summary": "ок", "findings": findings or []}
    return {"session": session, "steps": [
        {"write": {"path": f".ahub/review_r{round_no}_{model}.json", "text": json.dumps(body, ensure_ascii=False)}},
        {"event": {"type": "text", "text": "готово"}}]}


HIGH = [{"severity": "high", "file": "core/b.py", "line": 1, "issue": "Y должен быть 3", "fix": "поставить 3"}]


def test_code_no_review(store, project):
    fake = install_fake(store, [work()])
    t = code_task(store, project)
    res = run(store, project, t.id)
    assert res.state is State.DONE, res.reason
    done = store.events(task_id=t.id, needs_reaction=True)[-1].payload
    assert done["tests"] == "зелёная" and "1 file changed" in done["diffstat"] and done["summary"] == "сделал"
    assert "Разрешённые файлы" in fake.calls[0]["prompt"] and "tests/test_a.py::test_x" in fake.calls[0]["prompt"]


def test_review_approve(store, project):
    fake = install_fake(store, [work(), verdict()])
    t = code_task(store, project, review_models=["fake"], review_rounds=2)
    assert run(store, project, t.id).state is State.DONE
    rev_prompt = fake.calls[1]["prompt"]
    assert "diff --git" in rev_prompt and "core/b.py" in rev_prompt and fake.calls[1]["session_id"] is None


def test_review_changes_then_fix(store, project):
    fake = install_fake(store, [work(), verdict(v="changes", findings=HIGH),
                                work(text="Y = 3\n"), verdict(round_no=2)])
    t = code_task(store, project, review_models=["fake"], review_rounds=2)
    assert run(store, project, t.id).state is State.DONE
    assert store.get_task(t.id).round == 2
    assert "Y должен быть 3" in fake.calls[2]["prompt"] and fake.calls[2]["session_id"] == "ses_x"
    kinds = [e.payload.get("to") for e in store.events(task_id=t.id) if e.kind == "state"]
    assert kinds[:6] == ["preparing", "working", "checking", "reviewing", "fixing", "checking"]


def test_rounds_exhausted(store, project):
    install_fake(store, [work(), verdict(v="changes", findings=HIGH)])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "круги ревью кончились" in res.reason and "high: 1" in res.reason


def test_low_findings_do_not_block(store, project):
    low = [{"severity": "low", "file": "core/b.py", "line": 1, "issue": "имя переменной"}]
    install_fake(store, [work(), verdict(v="changes", findings=low)])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    assert run(store, project, t.id).state is State.DONE


def test_outside_allowed_is_decision(store, project):
    install_fake(store, [work(path="docs/x.md")])
    t = code_task(store, project, paths=["core/**", "tests/**"])
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "вне разрешённых: docs/x.md" in res.reason


def test_no_commit_repair(store, project):
    nocommit = {"session": "ses_x", "steps": [{"write": {"path": "core/b.py", "text": "Y = 2\n"}},
                                              {"event": {"type": "text", "text": "готово"}}]}
    fix = {"session": "ses_x", "steps": [{"git_commit": "feat: b"}, {"result": {"summary": "ок", "files": ["core/b.py"]}}]}
    fake = install_fake(store, [nocommit, fix])
    t = code_task(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert "Итог не сдан по форме" in fake.calls[1]["prompt"] and "незакоммиченные" in fake.calls[1]["prompt"]


def test_red_tests_fixed_once(store, project):
    red = work(path="core/a.py", text="X = 0\n")
    green = work(path="core/a.py", text="X = 1\n# fixed\n")
    fake = install_fake(store, [red, green])
    t = code_task(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert "Приёмка красная" in fake.calls[1]["prompt"]


def test_red_tests_twice(store, project):
    red = work(path="core/a.py", text="X = 0\n")
    install_fake(store, [red, work(path="core/a.py", text="X = 5\n")])
    t = code_task(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "приёмка красная" in res.reason


def test_routine_no_tests(store, project):
    install_fake(store, [work(path="docs/readme.md", text="# порядок\n")])
    t = code_task(store, project, kind=Kind.ROUTINE, paths=["docs/**"], accept=[])
    res = run(store, project, t.id)
    assert res.state is State.DONE and "приёмка" not in res.reason


def test_budget_exhausted_stops_with_save(store, project):
    stop_step = {"session": "ses_x", "steps": [{"event": {"type": "text", "text": "сохранил"}}]}
    fake = install_fake(store, [work(cost=0.02), stop_step])
    t = code_task(store, project, review_models=["fake"], budget_go=0.01)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "бюджет исчерпан" in res.reason
    assert "остановиться" in fake.calls[1]["prompt"] and fake.calls[1]["session_id"] == "ses_x"
    assert "budget_hard" in [e.kind for e in store.events(task_id=t.id)]


def test_hook_failure_is_error(store, tmp_path):
    project = make_project(tmp_path, hooks={"task_setup": "echo нет базы; exit 3"})
    install_fake(store, [])
    t = code_task(store, project)
    res = run(store, project, t.id)
    assert res.state is State.ERROR and "хук task_setup: код 3" in res.reason


def test_secrets_hidden_from_copy(store, tmp_path):
    project = make_project(tmp_path, secrets={"exclude": ["*.secret"]})
    root = Path(project.root)
    (root / "core" / "keys.secret").write_text("TOKEN=123\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "секрет")
    install_fake(store, [work()])
    t = code_task(store, project)
    assert run(store, project, t.id).state is State.DONE
    wt = Path(store.get_task(t.id).worktree)
    assert not (wt / "core" / "keys.secret").exists() and (wt / "core" / "a.py").exists()


def test_reviewer_changes_reverted(store, project):
    naughty = verdict()
    naughty["steps"].insert(0, {"write": {"path": "core/b.py", "text": "испорчено\n"}})
    install_fake(store, [work(), naughty])
    t = code_task(store, project, review_models=["fake"])
    assert run(store, project, t.id).state is State.DONE
    assert (Path(store.get_task(t.id).worktree) / "core" / "b.py").read_text() == "Y = 2\n"
