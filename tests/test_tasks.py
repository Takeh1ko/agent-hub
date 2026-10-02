from __future__ import annotations

import sys

import pytest

from ahub import config, tasks
from ahub.model import Kind, State
from ahub.store import Store
from tests.conftest import write


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    write(root / "core" / "a.py", "X = 1\n")
    write(root / "docs" / "readme.md", "# r\n")
    write(root / "tests" / "test_a.py", "from core.a import X\n\ndef test_x():\n    assert X == 1\n")
    write(root / "core" / "__init__.py", "")
    return config.parse_project({
        "schema_version": 2, "name": "P", "python": sys.executable,
        "allowed_paths": ["core/**", "tests/**", "docs/**"],
        "resources": {"test_db": {"lock": "/tmp/x.lock"}, "payments": {}}, "test_resource": "test_db",
        "models": {"deny": ["deepseek"]}, "budget": {"go": 1.5},
    }, root)


def spec(**kw) -> tasks.TaskSpec:
    base = {"project": "P", "kind": Kind.SCOUT, "title": "узнать, где утечка"}
    base.update(kw)
    return tasks.TaskSpec(**base)


def test_scout_defaults(store, project):
    t = tasks.create(store, spec(spec="подробно"), project)
    assert t.kind is Kind.SCOUT and t.state is State.QUEUED
    assert t.executor == "spark" and t.review == {}
    assert t.limits["time_limit_min"] == 60 and t.limits["paths"] == []
    assert t.budget_go == 1.5 and t.budget_usd == 0.0 and t.spec_hash


def test_code_defaults_and_resource(store, project):
    t = tasks.create(store, spec(kind=Kind.CODE, title="починить", paths=["core/**", "tests/test_a.py"],
                                 accept=["tests/test_a.py::test_x"]), project)
    assert t.review == {"models": ["spark"], "rounds": 2}
    assert t.limits["resources"] == ["test_db"]  # the acceptance run goes under the test resource
    assert t.limits["time_limit_min"] == 180


def test_review_levels_and_explicit(store, project):
    t = tasks.create(store, spec(kind=Kind.ROUTINE, title="порядок", paths=["docs/**"], review_level=4), project)
    assert t.review == {"models": ["spark", "mimo-flash"], "rounds": 2}
    t = tasks.create(store, spec(kind=Kind.ROUTINE, title="порядок", paths=["docs/**"], review_level=0), project)
    assert t.review == {}
    t = tasks.create(store, spec(kind=Kind.ROUTINE, title="x", paths=["docs/**"], review_models=["mimo-flash"],
                                 review_rounds=3), project)
    assert t.review == {"models": ["mimo-flash"], "rounds": 3}


def test_all_errors_at_once(store, project):
    with pytest.raises(tasks.TaskInvalid) as ei:
        tasks.resolve(store, spec(kind=Kind.CODE, title="", model="deepseek-flash", paths=["/etc/**", "bot/**"],
                                  accept=["tests/test_new.py::t"], read=["nope.md"], resources=["gpu"],
                                  after=[99], review_level=7, budget_go=-1), project)
    errs = " | ".join(ei.value.errors)
    for needle in ("нужна цель", "запрещена в проекте", "«/etc/**» вне", "«bot/**» вне", "нет файла для чтения",
                   "нет файла приёмки", "ресурс 'gpu'", "нет задачи T99", "уровень ревью 7", "отрицательным"):
        assert needle in errs, needle


def test_kind_rules(store, project):
    with pytest.raises(tasks.TaskInvalid, match="нужны разрешённые файлы"):
        tasks.resolve(store, spec(kind=Kind.ROUTINE, title="x"), project)
    with pytest.raises(tasks.TaskInvalid, match="нужна приёмка"):
        tasks.resolve(store, spec(kind=Kind.CODE, title="x", paths=["core/**"]), project)
    with pytest.raises(tasks.TaskInvalid, match="файлы не меняет"):
        tasks.resolve(store, spec(title="x", paths=["core/**"]), project)
    with pytest.raises(tasks.TaskInvalid, match="нужен вход"):
        tasks.resolve(store, spec(kind=Kind.REVIEW, title="x"), project)
    t = tasks.create(store, spec(kind=Kind.REVIEW, title="x", review_input="main..ahub/T1"), project)
    assert t.limits["input"] == "main..ahub/T1" and t.executor == "spark"


def test_new_accept_file_allowed_if_covered(store, project):
    t = tasks.create(store, spec(kind=Kind.CODE, title="x", paths=["core/**", "tests/**"],
                                 accept=["tests/test_new.py::test_y"]), project)
    assert t.limits["accept"] == ["tests/test_new.py::test_y"]


def test_collect_failure_reported(store, project):
    write(__import__("pathlib").Path(project.root) / "tests" / "test_broken.py", "import nonexistent_mod\n")
    with pytest.raises(tasks.TaskInvalid, match="не собирается"):
        tasks.resolve(store, spec(kind=Kind.CODE, title="x", paths=["tests/**"],
                                  accept=["tests/test_broken.py"]), project)
    tasks.resolve(store, spec(kind=Kind.CODE, title="x", paths=["tests/**"], accept=["tests/test_broken.py"]),
                  project, collect=False)


def test_after_rules(store, project):
    a = tasks.create(store, spec(), project)
    b = tasks.create(store, spec(after=[a.id]), project)
    assert b.after == [a.id]
    other = store.create_task(project="Q", kind="scout", title="чужая")
    rej = store.create_task(project="P", kind="scout", title="r")
    from ahub import transitions
    transitions.move(store, rej, State.REJECTED)
    with pytest.raises(tasks.TaskInvalid) as ei:
        tasks.resolve(store, spec(after=[other, rej]), project)
    assert "другого проекта" in str(ei.value) and "отклонена" in str(ei.value)


def test_idempotent_key_and_draft(store, project):
    t1 = tasks.create(store, spec(), project, key="abc")
    t2 = tasks.create(store, spec(), project, key="abc")
    assert t1.id == t2.id and len(store.list_tasks()) == 1
    d = tasks.create(store, spec(), project, draft=True)
    assert d.state is State.DRAFT


def test_spec_hash_changes_with_spec():
    a = tasks.spec_hash(spec(spec="один"))
    assert a == tasks.spec_hash(spec(spec="один", model="mimo-flash"))  # the model is not part of the spec
    assert a != tasks.spec_hash(spec(spec="два"))
