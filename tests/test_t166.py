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


def test_stuck_session_continues_without_progress(store, project):
    """K continue turns with no commit/tool activity → needs_decision `stuck_session`."""
    from ahub import engine

    project = _load_safe(project)
    install_fake(store, [])
    ensure_fake_model(store, "plain", "plain")
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look",
                                           model="plain"), project, collect=False)
    # drive the streak directly: same session, same head, no tools
    for _ in range(2):
        loops.note_continue(store, store.get_task(t.id), "", "ses_stuck", False)
    assert loops.stuck_of(store.get_task(t.id)).get("n") == 2
    eng = engine.Engine(store, project, t.id, sleep=lambda s: None)
    eng._saw_tools = False
    from ahub.providers.base import Outcome, RunResult

    stuck = eng._stuck_guard("continue", "ses_stuck", RunResult(Outcome.OK, "ses_stuck"))
    assert stuck is not None
    assert stuck.state is State.NEEDS_DECISION
    assert '"code":"stuck_session"' in store.get_task(t.id).state_reason or \
        "stuck" in stuck.reason.lower()


def test_stuck_resets_on_tool_activity(store, project):
    """Tool activity restarts the stuck streak."""
    ensure_fake_model(store, "plain", "plain")
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look",
                                           model="plain"), project, collect=False)
    head = "abc"
    loops.note_continue(store, store.get_task(t.id), head, "ses1", False)
    assert loops.stuck_of(store.get_task(t.id)).get("n") == 1
    loops.note_continue(store, store.get_task(t.id), head, "ses1", True)
    assert loops.stuck_of(store.get_task(t.id)).get("n") == 0


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
    assert "checking" not in l1 or "queued" in l1.split("checking")[0]
    l2 = views.task_text(store, fresh, now=now, w=200)
    assert "queued" in l2 and "2×" in l2
    assert "picks 3" in l2 or "Picks" in l2
    from ahub.tui import console as _console

    stage = _console._stage_word(fresh)
    assert stage.startswith("queued") and "2×" in stage


def test_observer_alarms_on_many_repicks(store):
    """Re-picked more than M/hour → cheap ALARM without a model."""
    from ahub import comms, observer

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
