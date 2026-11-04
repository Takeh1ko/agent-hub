"""Engine routing context for OpenCode DB isolation (contract 3, 5, 8).

RunSpec context stays immutable; the cached singleton provider is never mutated.
Crash-before-SESSION recovery is honest; eligibility survives restart. Fake HOME only.
"""

from __future__ import annotations

import os

from ahub import providers
from ahub.engine import Engine
from ahub.model import Role, State
from ahub.providers.base import Outcome, RunResult
from ahub.providers.opencode import OpencodeProvider
from ahub.store import Store
from tests.enginekit import make_project


def test_runspec_context_immutable_and_singleton_stable(tmp_path):
    """Route is selected from RunSpec context; the cached singleton is never mutated."""
    before = dict(os.environ)
    spec = providers.get("opencode")
    assert getattr(spec, "db_path", None) is None
    OpencodeProvider(db_path=str(tmp_path / "shard.db"))
    assert providers.get("opencode") is spec  # explicit instance did not replace the cache
    assert getattr(providers.get("opencode"), "db_path", None) is None
    assert dict(os.environ) == before


def test_outcome_to_state_honest_no_silent_restart(tmp_path):
    """A lost SESSION is ERROR/NEEDS_DECISION, never a silently started fresh conversation."""
    store = Store()
    project = make_project(tmp_path)
    eng = Engine(store, project, 1)
    crash = RunResult(Outcome.CRASH, None, error="no id")
    assert eng._outcome_to_state(crash) is not None
    state, _reason = eng._outcome_to_state(crash)
    assert state in (State.ERROR, State.NEEDS_DECISION)
    ok = RunResult(Outcome.OK, "ses_1")
    assert eng._outcome_to_state(ok) is None  # finished step has nothing to continue


def _capture_session(store, project, tid, monkeypatch, role, log_name, cwd):
    from ahub import engine as _engine

    captured: dict = {}

    def _fake_run(prov, spec, **kw):
        captured["env"] = dict(spec.env)
        captured["cwd"] = spec.cwd
        captured["session_id"] = spec.session_id
        return RunResult(Outcome.OK, "ses_new", final_text="ok")

    monkeypatch.setattr(_engine, "run_session", _fake_run)
    eng = Engine(store, project, tid, sleep=lambda s: None)
    eng.session(role, "oc-test", "prompt", log_name=log_name, cwd=cwd, prompt_kind="start")
    return captured


def _oc_task(store, created_ms: int, cutoff: int, tmp_path, monkeypatch):
    from ahub import paths as _paths
    from ahub import registry as _registry
    from tests.conftest import write as _write

    _write(_paths.global_config_path(), "[providers.opencode]\n"
           f"isolate_sqlite = true\nisolate_new_tasks_after = {cutoff}\n")
    try:
        _registry.add_model(store, "oc-test", "opencode", "opencode/m")
    except _registry.RegistryError:
        pass
    from ahub.model import Kind as _Kind

    tid = store.create_task(project="P", kind=_Kind.CODE, title="t", executor="oc-test", now=created_ms)
    return tid


def test_engine_enrolls_only_tasks_after_cutoff(tmp_path, monkeypatch):
    """Eligibility is creation-time: old/queued tasks stay legacy, new ones get the isolate route."""
    from ahub import providers as _providers

    store = Store()
    project = make_project(tmp_path)
    cutoff = 1_700_000_000_000
    old = _oc_task(store, cutoff - 1_000, cutoff, tmp_path, monkeypatch)
    new = _oc_task(store, cutoff + 1_000, cutoff, tmp_path, monkeypatch)
    cwd = str(tmp_path / "wt")

    class _Stub(_providers.get("opencode").__class__):
        def __init__(self):
            super().__init__(binary="/nonexistent/opencode-fake")

    _providers.register("opencode", _Stub())
    import json as _json
    from pathlib import Path as _Path

    def _expected(tid: int, role: str, log: str) -> tuple[str, str]:
        t = store.get_task(tid)
        assert t is not None
        task = _json.dumps([str(_Path(project.root).resolve()), t.id, t.created_at],
                            separators=(",", ":"))
        return task, f"task:{task}:role:{role}:log:{log}"

    old_task, old_route = _expected(old, "executor", "executor.log")
    old_env = _capture_session(store, project, old, monkeypatch, Role.EXECUTOR, "executor.log", cwd)["env"]
    assert old_env.get("AHUB_OPENCODE_ISOLATE") is None  # old task stays legacy
    assert old_env["AHUB_OPENCODE_TASK"] == old_task
    assert old_env["AHUB_OPENCODE_ROUTE"] == old_route
    new_task, new_route = _expected(new, "executor", "executor.log")
    new_env = _capture_session(store, project, new, monkeypatch, Role.EXECUTOR, "executor.log", cwd)["env"]
    assert new_env["AHUB_OPENCODE_ISOLATE"] == "1"
    assert new_env["AHUB_OPENCODE_TASK"] == new_task
    assert new_env["AHUB_OPENCODE_ROUTE"] == new_route
    assert "cwd:" not in new_env["AHUB_OPENCODE_ROUTE"]


def test_engine_future_turns_keep_shard_roles_distinct(tmp_path, monkeypatch):
    """Future turns keep the shard (same log_name); independent reviewers get distinct routes."""
    from ahub import providers as _providers

    store = Store()
    project = make_project(tmp_path)
    cutoff = 1_700_000_000_000
    tid = _oc_task(store, cutoff + 5_000, cutoff, tmp_path, monkeypatch)
    cwd = str(tmp_path / "wt")

    class _Stub(_providers.get("opencode").__class__):
        def __init__(self):
            super().__init__(binary="/nonexistent/opencode-fake")

    _providers.register("opencode", _Stub())
    first = _capture_session(store, project, tid, monkeypatch, Role.EXECUTOR, "executor.log", cwd)
    again = _capture_session(store, project, tid, monkeypatch, Role.EXECUTOR, "executor.log", cwd)
    assert first["env"]["AHUB_OPENCODE_ROUTE"] == again["env"]["AHUB_OPENCODE_ROUTE"]
    rev_a = _capture_session(store, project, tid, monkeypatch, Role.REVIEWER, "reviewer_r1_m.log", cwd)
    rev_b = _capture_session(store, project, tid, monkeypatch, Role.REVIEWER, "reviewer_r2_m.log", cwd)
    assert rev_a["env"]["AHUB_OPENCODE_ROUTE"] != rev_b["env"]["AHUB_OPENCODE_ROUTE"]
    assert rev_a["env"]["AHUB_OPENCODE_ISOLATE"] == "1" == rev_b["env"]["AHUB_OPENCODE_ISOLATE"]


def test_enrolled_task_keeps_isolate_when_flag_off(tmp_path, monkeypatch):
    """Durable enrollment: a fresh reviewer keeps ISOLATE after the flag is turned off."""
    from ahub import paths as _paths
    from ahub import providers as _providers
    from ahub.providers import opencode_storage as _storage
    from ahub.providers.base import RunSpec as _Spec
    from tests.conftest import write as _write

    store = Store()
    project = make_project(tmp_path)
    cutoff = 1_700_000_000_000
    tid = _oc_task(store, cutoff + 5_000, cutoff, tmp_path, monkeypatch)
    cwd = str(tmp_path / "wt")

    class _Stub(_providers.get("opencode").__class__):
        def __init__(self):
            super().__init__(binary="/nonexistent/opencode-fake")

    _providers.register("opencode", _Stub())
    first = _capture_session(store, project, tid, monkeypatch, Role.EXECUTOR, "executor.log", cwd)
    assert first["env"]["AHUB_OPENCODE_ISOLATE"] == "1"
    _storage.register(_Spec(prompt="x", cwd=cwd, model_id="m", env=first["env"]), cutoff + 5_000)
    assert _storage.task_enrolled(first["env"]["AHUB_OPENCODE_TASK"])
    _write(_paths.global_config_path(), "[providers.opencode]\nisolate_sqlite = false\n")
    fresh = _capture_session(store, project, tid, monkeypatch, Role.REVIEWER, "reviewer_r9_m.log", cwd)
    assert fresh["env"]["AHUB_OPENCODE_TASK"] == first["env"]["AHUB_OPENCODE_TASK"]
    assert fresh["env"]["AHUB_OPENCODE_ISOLATE"] == "1"  # enrollment outlives the flag
    assert fresh["env"]["AHUB_OPENCODE_ROUTE"] != first["env"]["AHUB_OPENCODE_ROUTE"]
    assert _storage.route_key(_Spec(prompt="x", cwd=cwd, model_id="m", env=fresh["env"])) is not None


def test_same_id_different_project_uses_distinct_route(tmp_path, monkeypatch):
    """Numeric id reuse across projects/creations never shares a shard or conversation."""
    from ahub import providers as _providers

    cutoff = 1_700_000_000_000

    class _Stub(_providers.get("opencode").__class__):
        def __init__(self):
            super().__init__(binary="/nonexistent/opencode-fake")

    _providers.register("opencode", _Stub())
    store_a = Store(path=tmp_path / "sa.db")
    project_a = make_project(tmp_path / "pa")
    tid_a = _oc_task(store_a, cutoff + 1_000, cutoff, tmp_path, monkeypatch)
    store_b = Store(path=tmp_path / "sb.db")
    project_b = make_project(tmp_path / "pb")
    tid_b = _oc_task(store_b, cutoff + 2_000, cutoff, tmp_path, monkeypatch)
    assert tid_a == tid_b == 1  # same numeric id, different project and creation marker
    cwd = str(tmp_path / "wt")
    env_a = _capture_session(store_a, project_a, tid_a, monkeypatch,
                             Role.EXECUTOR, "executor.log", cwd)["env"]
    env_b = _capture_session(store_b, project_b, tid_b, monkeypatch,
                             Role.EXECUTOR, "executor.log", cwd)["env"]
    assert env_a["AHUB_OPENCODE_TASK"] != env_b["AHUB_OPENCODE_TASK"]
    assert env_a["AHUB_OPENCODE_ROUTE"] != env_b["AHUB_OPENCODE_ROUTE"]
