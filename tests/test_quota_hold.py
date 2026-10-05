"""Quota-hold loop (T165): resume at the held stage, back-off re-picks, one owner notice."""

from __future__ import annotations

import dataclasses
import time

import pytest

from ahub import providers, quota, reasons, tasks, transitions
from ahub.model import Ev, Kind, State
from ahub.providers.base import QuotaBucket
from ahub.store import Store
from tests.enginekit import git, install_fake, make_project


@pytest.fixture(autouse=True)
def _en_env(monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "en")
    monkeypatch.setenv("AHUB_TZ", "UTC")
    from ahub.i18n import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path, max_parallel=2)


def set_hub_quota(cfg_text: str) -> None:
    from ahub import paths

    p = paths.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cfg_text, encoding="utf-8")


def ensure_model(store: Store, alias: str, provider: str = "fake", model_id: str = "") -> None:
    from ahub import registry
    from tests.enginekit import ensure_fake_model

    if provider != "fake":
        try:
            registry.add_model(store, alias, provider, model_id or alias)
        except registry.RegistryError as e:
            raise AssertionError(
                f"alias '{alias}' already exists for another provider: {e} — re-point explicitly") from e
        return
    ensure_fake_model(store, alias, model_id or alias)


class Recorder:
    def __init__(self):
        self.spawned = []

    def __call__(self, tid):
        self.spawned.append(tid)
        return 10_000 + tid


def svc(store, project, tmp_path):
    from ahub import service

    rec = Recorder()
    (tmp_path / "proc").mkdir(exist_ok=True)
    return service.Service(store, [project], spawn=rec, proc_root=tmp_path / "proc"), rec


def _load_safe(project):
    # Load-safe: slow fake startup under parallel suites is not silence.
    return dataclasses.replace(project, timeouts=dataclasses.replace(project.timeouts, idle_s=120))


def _work(session: str, files: list[str], text: str = "Y = 2\n") -> dict:
    return {"session": session, "steps": [
        {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
        {"write": {"path": "core/b.py", "text": text}},
        {"git_commit": "feat: y"},
        {"result": {"summary": "did y", "files": files}},
        {"event": {"type": "text", "text": "done"}}]}


def _code_hold_task(store: Store, project, scenarios: list[dict]):
    """Code task whose worker delivers but whose reviewer is quota-held (no fallback, no menu)."""
    fake = install_fake(store, scenarios)
    ensure_model(store, "plain", "fake", "plain")
    ensure_model(store, "gemini-rev", "fake", "gemini-rev")
    with store.tx() as c:  # no reviewer-menu candidates: the hold must hold, not fall back
        c.execute("DELETE FROM role_model WHERE role='reviewer'")
    now = int(time.time() * 1000)
    providers.get("fake").set_quota([
        QuotaBucket("Gemini", "5h", 0.10, now + 3600_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="plain", review_models=["gemini-rev"],
                                           review_rounds=1, paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)
    return fake, t


def test_review_hold_resumes_at_review_without_worker_turn_or_gates(store, project, tmp_path,
                                                                    monkeypatch):
    """A review-stage hold re-picks straight at the review: no worker turn, no gates re-run."""
    from ahub import engine, gates

    project = _load_safe(project)
    fake, t = _code_hold_task(store, project, [_work("ses_w", ["core/b.py"])])

    gate_calls: list[int] = []
    real_check = gates.check

    def counting(*a, **kw):
        gate_calls.append(1)
        return real_check(*a, **kw)

    monkeypatch.setattr("ahub.gates.check", counting)

    settled = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled.state is State.QUEUED
    assert "waiting for Gemini quota" in settled.reason
    assert len(fake.calls) == 1 and len(gate_calls) == 1
    hold = store.get_task(t.id).limits.get("quota_hold") or {}
    assert hold.get("stage") == "review" and hold.get("head") and hold.get("n") == 1
    assert hold.get("not_before", 0) > hold.get("since", 0)

    settled2 = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled2.state is State.QUEUED
    assert "waiting for Gemini quota" in settled2.reason
    assert len(fake.calls) == 1, "the resume must not run another worker turn"
    assert len(gate_calls) == 1, "the resume must not re-run the gates"
    hold2 = store.get_task(t.id).limits.get("quota_hold") or {}
    assert hold2.get("n") == 2 and hold2.get("since") == hold.get("since")


def test_review_hold_resume_invalid_after_head_moves(store, project):
    """The work changed under a hold: the re-pick runs the worker turn again."""
    from pathlib import Path

    from ahub import engine

    project = _load_safe(project)
    fake, t = _code_hold_task(store, project, [_work("ses_w1", ["core/b.py"]),
                                               _work("ses_w2", ["core/b.py", "core/c.py"],
                                                     text="Y = 3\n")])

    settled = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled.state is State.QUEUED
    assert len(fake.calls) == 1

    wt = store.get_task(t.id).worktree
    assert wt, "no worktree"
    Path(wt, "core/c.py").write_text("Z = 3\n", encoding="utf-8")
    git(wt, "add", "core/c.py")
    git(wt, "commit", "-q", "-m", "manual tweak")

    settled2 = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled2.state is State.QUEUED
    assert len(fake.calls) == 2, "a moved HEAD needs a new worker turn"
    hold2 = store.get_task(t.id).limits.get("quota_hold") or {}
    assert hold2.get("stage") == "review" and hold2.get("head")


def test_service_defers_review_held_task_until_wake(store, project, tmp_path):
    """A review-held task is not re-picked every tick: backoff, early wake on fallback/recovery."""
    install_fake(store, [])
    ensure_model(store, "plain", "fake", "plain")
    ensure_model(store, "gemini-rev", "fake", "gemini-rev")
    ensure_model(store, "bunny", "fake", "bunny")

    now = int(time.time() * 1000)
    providers.get("fake").set_quota([
        QuotaBucket("Gemini", "5h", 0.10, now + 3600_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="plain", review_models=["gemini-rev"],
                                           review_rounds=1, paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)
    stored_reason = reasons.dump("wait_quota", group="Gemini", window="5h", pct=10, reset="08.10 16:39")
    notice = reasons.dump("quota_hold", group="Gemini", window="5h", pct=10, reset="08.10 16:39",
                          setting="fallback_reviewer", cmd=f"ahub task edit T{t.id} --review <alias>")
    hold = {"stage": "review", "reason": stored_reason, "notice": notice, "since": now,
            "not_before": now + 900_000, "n": 1, "notified": False,
            "head": "abc", "commit": "abc", "base": "def"}
    store.update_task(t.id, limits={**store.get_task(t.id).limits, "quota_hold": hold})

    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert rec.spawned == []
    assert store.get_task(t.id).state_reason == stored_reason

    # A newly configured fallback wakes the task at once (panel entry moves, resume info stays).
    set_hub_quota('[quota]\nfallback_reviewer = "bunny"\n')
    s.tick()
    assert rec.spawned == [t.id]
    after = store.get_task(t.id)
    assert after.review.get("models") == ["bunny"]
    assert quota.active_hold(after) == {}
    model_ev = next(e for e in store.events(task_id=t.id) if e.kind == Ev.MODEL_CHANGED.value)
    assert "running on bunny" in model_ev.payload.get("text", "")

    # A recovered quota wakes another held task early too.
    providers.get("fake").set_quota([
        QuotaBucket("Gemini", "5h", 0.60, now + 3600_000, lambda m: "gemini" in m.lower()),
    ])
    t2 = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="second",
                                            model="plain", review_models=["gemini-rev"],
                                            review_rounds=1, paths=["core/**"],
                                            accept=["tests/test_a.py"]),
                      project, collect=False)
    store.update_task(t2.id, limits={**store.get_task(t2.id).limits, "quota_hold": hold})
    set_hub_quota('[quota]\nmin_5h = 0.15\n')
    s.tick()
    assert t2.id in rec.spawned


def test_long_hold_notifies_owner_once(store, project, tmp_path):
    """A hold past 30 min with no fallback raises one DECISION event per task."""
    install_fake(store, [])
    ensure_model(store, "plain", "fake", "plain")
    ensure_model(store, "gemini-rev", "fake", "gemini-rev")

    now = int(time.time() * 1000)
    providers.get("fake").set_quota([
        QuotaBucket("Gemini", "weekly", 0.03, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="plain", review_models=["gemini-rev"],
                                           review_rounds=1, paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)
    notice = reasons.dump("quota_hold", group="Gemini", window="weekly", pct=3, reset="08.10 16:39",
                          setting="fallback_reviewer", cmd=f"ahub task edit T{t.id} --review <alias>")
    hold = {"stage": "review",
            "reason": reasons.dump("wait_quota", group="Gemini", window="weekly", pct=3,
                                   reset="08.10 16:39"),
            "notice": notice, "since": now - 31 * 60_000, "not_before": now + 3600_000,
            "n": 3, "notified": False}
    store.update_task(t.id, limits={**store.get_task(t.id).limits, "quota_hold": hold})

    s, rec = svc(store, project, tmp_path)
    s.tick()
    assert rec.spawned == []
    decisions = [e for e in store.events(task_id=t.id) if e.kind == Ev.NEEDS_DECISION.value]
    assert len(decisions) == 1
    assert decisions[0].payload.get("reason") == notice
    assert "fallback_reviewer" in reasons.text(notice)
    assert store.get_task(t.id).limits["quota_hold"]["notified"] is True

    s.tick()
    assert len([e for e in store.events(task_id=t.id) if e.kind == Ev.NEEDS_DECISION.value]) == 1


def test_menu_reviewer_fallback_without_configured_fallback(store, project):
    """No fallback configured: the next reviewer-menu entry above threshold takes the turn."""
    from ahub import engine

    project = _load_safe(project)
    fake = install_fake(store, [
        {"session": "ses_menu", "steps": [
            {"write": {"path": ".ahub/review_r1_spark.json",
                       "text": '{"verdict": "approve", "summary": "ok", "findings": []}'}},
            {"event": {"type": "text", "text": "approved"}}
        ]}
    ])
    ensure_model(store, "gemini-rev", "fake", "gemini-rev")
    ensure_model(store, "spark", "fake", "spark")  # first reviewer-menu entry, above threshold

    now = int(time.time() * 1000)
    providers.get("fake").set_quota([
        QuotaBucket("Gemini", "5h", 0.10, now + 3600_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="review task",
                                           review_models=["gemini-rev"], review_input="core/a.py"),
                     project, collect=False)

    settled = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled.state is State.DONE
    assert len(fake.calls) == 1
    model_ev = next(e for e in store.events(task_id=t.id) if e.kind == Ev.MODEL_CHANGED.value)
    assert "running on spark" in model_ev.payload.get("text", "")
    # The swap is local to the run (like the configured pre-check fallback): the panel stays.
    assert store.get_task(t.id).review.get("models") == ["gemini-rev"]


def test_owner_actions_expire_quota_hold(store, project):
    """continue / model / task edit wake a held task at the next tick (resume info stays)."""
    from ahub import accept

    install_fake(store, [])
    ensure_model(store, "plain", "fake", "plain")
    ensure_model(store, "fake", "fake", "fake/model")

    def held_task(**kw):
        t = tasks.create(store, tasks.TaskSpec(project="P", title="held", **kw), project, collect=False)
        now = int(time.time() * 1000)
        hold = {"stage": "executor",
                "reason": reasons.dump("wait_quota", group="Gemini", window="5h", pct=10,
                                       reset="08.10 16:39"),
                "since": now, "not_before": now + 900_000, "n": 1, "notified": False}
        store.update_task(t.id, limits={**store.get_task(t.id).limits, "quota_hold": hold})
        return t

    t1 = held_task(kind=Kind.SCOUT, model="plain")
    transitions.move(store, t1.id, State.STOPPED)
    accept.continue_task(store, t1.id)
    assert store.get_task(t1.id).state is State.QUEUED
    assert store.get_task(t1.id).limits["quota_hold"]["not_before"] == 0

    t2 = held_task(kind=Kind.SCOUT, model="plain")
    accept.change_model(store, project, t2.id, "fake")
    assert store.get_task(t2.id).executor == "fake"
    assert store.get_task(t2.id).limits["quota_hold"]["not_before"] == 0

    t3 = held_task(kind=Kind.CODE, model="plain", paths=["core/**"], accept=["tests/test_a.py"],
                   review_models=["plain"], review_rounds=1)
    accept.edit(store, project, t3.id, review=["fake"])
    assert store.get_task(t3.id).review.get("models") == ["fake"]
    assert store.get_task(t3.id).limits["quota_hold"]["not_before"] == 0
