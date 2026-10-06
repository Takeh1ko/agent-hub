"""T166 busy-loop guard: loop detector, stuck session, visibility, observer alarm, test guards."""

from __future__ import annotations

import dataclasses
import time

import pytest

from ahub import loops, reasons, tasks
from ahub.model import Ev, Kind, State
from ahub.providers.base import QuotaBucket
from ahub.store import Store
from tests.enginekit import ensure_fake_model, install_fake, make_project


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


def _load_safe(project):
    return dataclasses.replace(project, timeouts=dataclasses.replace(project.timeouts, idle_s=120))


def _quota_hold_task(store, project, scenarios):
    fake = install_fake(store, scenarios)
    ensure_fake_model(store, "plain", "plain")
    ensure_fake_model(store, "gemini-rev", "gemini-rev")
    with store.tx() as c:
        c.execute("DELETE FROM role_model WHERE role='reviewer'")
    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.10, now + 3600_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="plain", review_models=["gemini-rev"],
                                           review_rounds=1, paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)
    return fake, t


def _work(session, files=("core/b.py",)):
    return {"session": session, "steps": [
        {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
        {"write": {"path": "core/b.py", "text": "Y = 2\n"}},
        {"git_commit": "feat: y"},
        {"result": {"summary": "did y", "files": list(files)}},
        {"event": {"type": "text", "text": "done"}}]}


def test_loop_detector_same_reason_three_times_goes_to_decision(store, project):
    """Same QUEUED reason 3× without progress → needs_decision `loop` with evidence, one event."""
    from ahub import engine

    project = _load_safe(project)
    _, t = _quota_hold_task(store, project, [_work("ses_w", ["core/b.py"])])

    for _ in range(2):
        settled = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
        assert settled.state is State.QUEUED
        assert "waiting for Gemini quota" in settled.reason
    assert loops.loop_of(store.get_task(t.id)).get("n") == 2

    settled3 = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled3.state is State.NEEDS_DECISION
    assert '"code":"loop"' in store.get_task(t.id).state_reason
    assert "wait_quota" in settled3.reason or "loop" in settled3.reason
    decisions = [e for e in store.events(task_id=t.id) if e.kind == Ev.NEEDS_DECISION.value]
    assert len(decisions) == 1

    # stays until the owner acts: another engine run does not requeue it
    settled4 = engine.Engine(store, project, t.id, sleep=lambda s: None).run()
    assert settled4.state is State.NEEDS_DECISION
    assert len([e for e in store.events(task_id=t.id) if e.kind == Ev.NEEDS_DECISION.value]) == 1


def test_loop_resets_on_progress_new_commit(store, project):
    """A new commit between settles restarts the loop streak."""
    ensure_fake_model(store, "plain", "plain")
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look",
                                           model="plain"), project, collect=False)
    now = int(time.time() * 1000)
    loops.note_settle(store, store.get_task(t.id), "wait_quota", head="aaa", verdicts=0, now=now)
    assert loops.loop_of(store.get_task(t.id)).get("n") == 1
    loops.note_settle(store, store.get_task(t.id), "wait_quota", head="aaa", verdicts=0,
                      now=now + 60_000)
    assert loops.loop_of(store.get_task(t.id)).get("n") == 2
    loops.note_settle(store, store.get_task(t.id), "wait_quota", head="bbb", verdicts=0,
                      now=now + 120_000)
    assert loops.loop_of(store.get_task(t.id)).get("n") == 1


def _silent(session: str) -> dict:
    """A worker turn with no tools and no files: text only, nothing committed."""
    return {"session": session, "steps": [{"event": {"type": "text", "text": "still thinking"}}]}


def _seed_resume(store, project, session: str = "ses_stuck"):
    """Code task parked in WORKING with a live session, so the next run resumes it with continue."""
    from ahub import transitions, workspace
    from ahub.model import Role

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="stuck?",
                                           model="plain", paths=["core/**"],
                                           accept=["tests/test_a.py"], review_level=0),
                     project, collect=False)
    transitions.move(store, t.id, State.PREPARING)
    ws = workspace.ensure(project, t.id)
    transitions.move(store, t.id, State.WORKING,
                     fields={"worktree": ws.path, "branch": ws.branch,
                             "base_sha": ws.base_sha, "round": 1})
    store.add_session(task_id=t.id, provider="fake", role=Role.EXECUTOR.value,
                      model="plain", external_id=session)
    return store.get_task(t.id)


def test_stuck_session_continues_without_progress(store, project, monkeypatch):
    """K continue turns with no commit/tool activity → needs_decision `stuck_session`.

    Three real worker continue turns through the fake provider (silent text only —
    no tool events, no commits), each re-picked after a hub lock: the third stops
    with `stuck_session` instead of another continue.
    """
    import sqlite3

    from ahub import engine, gates

    project = _load_safe(project)
    install_fake(store, [_silent("ses_stuck"), _silent("ses_stuck"), _silent("ses_stuck")])
    ensure_fake_model(store, "plain", "plain")

    def _locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gates, "check", _locked)
    tid = _seed_resume(store, project).id
    first = engine.Engine(store, project, tid, sleep=lambda s: None).run()
    assert first.state is State.QUEUED
    assert loops.stuck_of(store.get_task(tid)).get("n") == 1
    second = engine.Engine(store, project, tid, sleep=lambda s: None).run()
    assert second.state is State.QUEUED
    assert loops.stuck_of(store.get_task(tid)).get("n") == 2
    third = engine.Engine(store, project, tid, sleep=lambda s: None).run()
    assert third.state is State.NEEDS_DECISION
    assert '"code":"stuck_session"' in store.get_task(tid).state_reason
    assert "stuck" in third.reason.lower()


def test_stuck_resets_on_repair_with_tools(store, project, monkeypatch):
    """A repair turn with tool activity resets the streak: no false `stuck_session` after a fix."""
    from ahub import engine, gates

    project = _load_safe(project)
    repair = {"session": "ses_stuck", "steps": [
        {"event": {"type": "tool_end", "tool": "read"}},
        {"event": {"type": "text", "text": "fixing"}}]}
    install_fake(store, [_silent("ses_stuck"), _silent("ses_stuck"), repair])
    ensure_fake_model(store, "plain", "plain")

    import sqlite3

    def _locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gates, "check", _locked)
    tid = _seed_resume(store, project).id
    assert engine.Engine(store, project, tid, sleep=lambda s: None).run().state is State.QUEUED
    assert loops.stuck_of(store.get_task(tid)).get("n") == 1

    calls = {"n": 0}

    def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return gates.GateResult(base="b", head="h", repairable=["uncommitted changes: core/b.py"])
        return gates.GateResult(base="b", head="h")

    monkeypatch.setattr(gates, "check", _flaky)
    done = engine.Engine(store, project, tid, sleep=lambda s: None).run()
    assert done.state is State.DONE, done.reason
    assert loops.stuck_of(store.get_task(tid)).get("n") == 0


def test_visibility_held_task_shows_count_next_check_and_picks(store, project, tmp_path):
    """Queued hold reads as queued + wait + (n×, next check), not the last phase; L2 shows picks."""
    from ahub import views

    install_fake(store, [])
    ensure_fake_model(store, "plain", "plain")
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look",
                                           model="plain"), project, collect=False)
    now = int(time.time() * 1000)
    stored_reason = reasons.dump("wait_quota", group="Gemini", window="5h", pct=10, reset="08.10 16:39")
    hold = {"stage": "executor", "reason": stored_reason, "since": now,
            "not_before": now + 900_000, "n": 2, "notified": False}
    store.update_task(t.id, state_reason=stored_reason, phase="testing",
                      limits={"picks": 3, "pick_ts": [now],
                              "loop": {"code": "wait_quota", "n": 2, "first": now - 900_000,
                                       "head": "", "verdicts": 0, "round": 1},
                              "quota_hold": hold})
    fresh = store.get_task(t.id)
    assert fresh.state is State.QUEUED
    l1 = views.status_text(store, now=now, w=200)
    assert "queued" in l1 and "2×" in l1
    assert "testing" not in l1  # the stale phase of the last attempt, not what the task is
    l2 = views.task_text(store, fresh, now=now, w=200)
    assert "queued" in l2 and "2×" in l2
    assert "testing" not in l2
    assert "picks 3" in l2 or "Picks" in l2
    from ahub.tui import console as _console

    stage = _console._stage_word(fresh)
    assert stage.startswith("queued") and "2×" in stage
    assert "testing" not in stage


def test_observer_alarms_on_many_repicks(store, monkeypatch):
    """Re-picked more than M/hour → cheap ALARM without a model."""
    from ahub import comms, observer
    from ahub.providers.base import Health

    class _Healthy:
        name = "opencode"

        def health(self) -> Health:
            return Health(True, ())

    from ahub import providers

    monkeypatch.setattr(providers, "names", lambda: ["opencode"])
    monkeypatch.setitem(providers._cache, "opencode", _Healthy())
    store.meta_set("service_heartbeat", str(int(time.time() * 1000)))
    install_fake(store, [])
    ensure_fake_model(store, "plain", "plain")
    import tempfile
    from pathlib import Path

    from tests.enginekit import make_project as _mk

    with tempfile.TemporaryDirectory() as td:
        proj = _mk(Path(td))
        t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="looping",
                                               model="plain"), proj, collect=False)
        now = int(time.time() * 1000)
        store.update_task(t.id, limits={"picks": 7, "pick_ts": [now - i * 5 * 60_000 for i in range(7)]})
        sus = [s for s in observer.quick_check(store, projects=[], health=False, live={})
               if s.sig.startswith("loop:")]
        assert sus and str(t.id) in sus[0].sig
        assert observer.cycle(store, use_model=False, projects=[]) == "alarm"
        alarms = comms.alarms(store)
        assert alarms and str(t.id) in alarms[-1].payload.get("text", "")


def test_test_guard_refuses_real_provider_runner(monkeypatch):
    """Runner with a real provider under test fails at once naming alias and provider."""
    from ahub import providers
    from ahub.providers.base import RunSpec
    from ahub.providers.fake import FakeProvider

    monkeypatch.setenv("AHUB_UNDER_TEST", "1")
    monkeypatch.delenv("AHUB_LIVE", raising=False)
    fake = FakeProvider()
    providers.register("fake-guard", fake)

    class _Real:
        name = "opencode"

        def build_command(self, spec):
            return ["opencode", "run"]

    providers.register("opencode", _Real())
    try:
        with pytest.raises(AssertionError, match="opencode"):
            from ahub.providers import runner as _runner

            _runner.run(providers.get("opencode"), RunSpec(prompt="hi", cwd="/tmp",
                                                           model_id="m", log_path="/tmp/x.log"))
    finally:
        providers.register("fake", fake)


def test_loud_helper_repoints_seeded_alias(store):
    """Seeded bunny → fake is an explicit re-point, never a silent no-op."""
    from ahub import registry

    assert registry.get(store, "bunny").provider == "opencode"
    ensure_fake_model(store, "bunny", "bunny")
    assert registry.get(store, "bunny").provider == "fake"
