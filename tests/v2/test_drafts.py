from __future__ import annotations

import json

import pytest

from ahub import drafts, registry
from ahub.model import Role, State
from ahub.store import Store
from tests.v2.enginekit import install_fake, make_project


@pytest.fixture
def store() -> Store:
    return Store()


def answer(data: dict) -> dict:
    return {"session": "ses_d", "steps": [
        {"write": {"path": ".ahub/draft.json", "text": json.dumps(data, ensure_ascii=False)}},
        {"event": {"type": "text", "text": "готово"}}]}


GOOD = {"kind": "code", "title": "кнопка «повторить оплату»", "spec": "добавить Y в core/b.py",
        "paths": ["core/**", "tests/**"], "accept": ["tests/test_a.py::test_x"], "review_level": 2,
        "read": ["core/a.py", "нет/такого.md"]}


def setup(store, scenarios):
    fake = install_fake(store, scenarios)
    registry.add_to_role(store, Role.DRAFTER, "fake", default=True)
    return fake


def test_ready_preview_start(store, tmp_path):
    project = make_project(tmp_path)
    fake = setup(store, [answer(GOOD)])
    did = drafts.create(store, project, "хочу кнопку повторной оплаты")
    text = drafts.preview(store, did)
    assert "Черновик" in text and "код" in text and "Проверка: tests/test_a.py::test_x" in text
    assert "хочу кнопку" in fake.calls[0]["prompt"]
    assert fake.calls[0]["cwd"] != project.root  # отдельная копия
    tid = drafts.start(store, project, did)
    t = store.get_task(tid)
    assert t.state is State.QUEUED and t.review == {"models": ["spark"], "rounds": 2}
    assert t.limits["read"] == ["core/a.py"]  # несуществующее выброшено
    assert drafts.start(store, project, did) == tid  # повтор — та же задача
    assert not drafts.cancel(store, did)


def test_retry_with_errors_then_fail(store, tmp_path):
    project = make_project(tmp_path)
    bad = dict(GOOD, paths=["bot/**"])
    fake = setup(store, [answer(bad), answer(GOOD)])
    did = drafts.create(store, project, "x")
    assert "вне разрешённых" in fake.calls[1]["prompt"]
    assert drafts.list_drafts(store)[0]["status"] == "ready"
    setup(store, [answer(bad), answer(bad)])
    d2 = drafts.create(store, project, "y")
    assert "failed" in drafts.preview(store, d2)
    with pytest.raises(ValueError):
        drafts.start(store, project, d2)
