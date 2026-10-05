"""Tests for quota-aware scheduling, caching, visibility, and quota error recovery (T137)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ahub import paths, quota, reasons, registry, service, tasks, transitions
from ahub.doctor import Check
from ahub.home import data as home_data
from ahub.home import text as home_text
from ahub.model import Ev, Kind, Role, State
from ahub.providers.agy import AgyProvider, parse_reset_time, parse_usage_json
from ahub.providers.base import Outcome, QuotaBucket, RunResult
from ahub.store import Store
from tests.enginekit import install_fake, make_project

DATA_DIR = Path(__file__).parent / "data" / "agy"
USAGE_FIXTURE = DATA_DIR / "usage.json"


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


def ensure_model(store: Store, alias: str, provider: str = "fake", model_id: str = "") -> None:
    try:
        registry.add_model(store, alias, provider, model_id or alias)
    except registry.RegistryError:
        pass


def set_hub_quota(cfg_text: str) -> None:
    p = paths.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cfg_text, encoding="utf-8")


def test_parse_real_usage_json():
    """Verify parsing of real agy /usage JSON fixture."""
    data = json.loads(USAGE_FIXTURE.read_text(encoding="utf-8"))
    buckets = parse_usage_json(data)
    assert len(buckets) == 4

    gemini_5h = next((b for b in buckets if b.group == "Gemini" and b.window == "5h"), None)
    gemini_week = next((b for b in buckets if b.group == "Gemini" and b.window == "weekly"), None)
    claude_5h = next((b for b in buckets if b.group == "Claude and GPT" and b.window == "5h"), None)

    assert gemini_5h is not None
    assert gemini_5h.remaining == 0.29
    assert gemini_5h.reset_at == parse_reset_time("2026-10-04T17:16:00Z")
    assert gemini_5h.models("gemini-2.5-pro")
    assert gemini_5h.models("gemini-flash")
    assert not gemini_5h.models("claude-3-5-sonnet")

    assert gemini_week is not None
    assert gemini_week.remaining == 0.60
    assert gemini_week.reset_at == parse_reset_time("2026-10-08T11:39:48Z")

    assert claude_5h is not None
    assert claude_5h.models("claude-3-5-sonnet")
    assert claude_5h.models("gpt-4o")
    assert not claude_5h.models("gemini-flash")


def test_quota_cache_one_probe_per_minute(tmp_path):
    """agy.quota() probes at most once per minute, cached via small file."""
    cache_file = tmp_path / "quota_agy.json"
    prov = AgyProvider(binary="/bin/echo")

    calls = 0

    def fake_capture(*args, **kw):
        nonlocal calls
        calls += 1
        return 0, USAGE_FIXTURE.read_text(encoding="utf-8"), ""

    with patch("ahub.providers.agy.agy_quota_cache_file", return_value=cache_file), \
         patch("ahub.providers.agy.run_capture", side_effect=fake_capture):
        # 1st call: runs capture
        b1 = prov.quota()
        assert len(b1) == 4
        assert calls == 1
        assert cache_file.is_file()

        # 2nd call: hits cache, capture not called
        b2 = prov.quota()
        assert len(b2) == 4
        assert calls == 1

        # force=True: ignores cache, runs capture
        b3 = prov.quota(force=True)
        assert len(b3) == 4
        assert calls == 2

        # Expire cache (> 60s)
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        cached["ts"] = time.time() - 65
        cache_file.write_text(json.dumps(cached), encoding="utf-8")

        # 4th call: cache expired, runs capture
        b4 = prov.quota()
        assert len(b4) == 4
        assert calls == 3


class Recorder:
    def __init__(self):
        self.spawned = []

    def __call__(self, tid):
        self.spawned.append(tid)
        return 10_000 + tid


def svc(store, project, tmp_path):
    rec = Recorder()
    (tmp_path / "proc").mkdir(exist_ok=True)
    return service.Service(store, [project], spawn=rec, proc_root=tmp_path / "proc"), rec


def test_scheduling_above_threshold_starts(store, tmp_path):
    """When quota is above threshold, task starts normally."""
    project = make_project(tmp_path, max_parallel=2)
    fake = install_fake(store, [])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")

    # Set quota: 5h = 29% (> 15% default), weekly = 60% (> 5% default)
    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.29, now + 3600_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="gemini-flash", paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)
    s, rec = svc(store, project, tmp_path)
    s.tick()

    assert rec.spawned == [t.id]
    assert store.get_task(t.id).state_reason == ""


def test_scheduling_below_threshold_with_fallback(store, tmp_path, monkeypatch):
    """Below threshold: starts on fallback and records event."""
    project = make_project(tmp_path, max_parallel=2)
    fake = install_fake(store, [])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")
    ensure_model(store, "bunny", "fake", "bunny")

    # Configure fallback
    set_hub_quota('[quota]\nfallback = "bunny"\nmin_5h = 0.15\n')

    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.12, now + 3600_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="gemini-flash", paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)
    s, rec = svc(store, project, tmp_path)
    s.tick()

    # Starts on fallback
    assert rec.spawned == [t.id]
    task_after = store.get_task(t.id)
    assert task_after.executor == "bunny"
    assert task_after.limits.get("fresh_session") is True  # never resume another model's session

    # Event recorded: "T1: Gemini 5h quota 12% → running on bunny"
    events = store.events(task_id=t.id)
    model_ev = next((e for e in events if e.kind == Ev.MODEL_CHANGED.value), None)
    assert model_ev is not None
    assert "Gemini 5h quota 12% → running on bunny" in model_ev.payload.get("text", "")


def test_scheduling_below_threshold_without_fallback_waits(store, tmp_path, monkeypatch):
    """Below threshold without fallback: keeps queued with wait reason."""
    project = make_project(tmp_path, max_parallel=2)
    fake = install_fake(store, [])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")

    set_hub_quota('[quota]\nmin_5h = 0.15\n')

    dt = datetime(2026, 10, 4, 17, 16, tzinfo=timezone.utc)
    reset_ms = int(dt.timestamp() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.12, reset_ms, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, reset_ms + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    # Fix now to be before reset_ms
    with patch("ahub.quota.now_ms", return_value=reset_ms - 1000):
        t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                               model="gemini-flash", paths=["core/**"],
                                               accept=["tests/test_a.py"]),
                         project, collect=False)
        s, rec = svc(store, project, tmp_path)
        s.tick()

        assert rec.spawned == []
        task_after = store.get_task(t.id)
        assert task_after.state is State.QUEUED
        assert "waiting for Gemini quota: 5h 12%" in reasons.text(task_after.state_reason)
        assert "17:16" in reasons.text(task_after.state_reason)


def test_concurrency_cap_per_quota_group(store, tmp_path):
    """Concurrency cap per quota group: ceil(remaining_5h * 6), never below 1."""
    project = make_project(tmp_path, max_parallel=10)
    fake = install_fake(store, [])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")

    now = int(time.time() * 1000)
    # 29% -> ceil(0.29 * 6) = 2
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.29, now + 3600_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t1 = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="t1", model="gemini-flash",
                                            paths=["core/**"], accept=["tests/test_a.py"]), project, collect=False)
    t2 = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="t2", model="gemini-flash",
                                            paths=["core/**"], accept=["tests/test_a.py"]), project, collect=False)
    t3 = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="t3", model="gemini-flash",
                                            paths=["core/**"], accept=["tests/test_a.py"]), project, collect=False)

    s, rec = svc(store, project, tmp_path)
    s.tick()

    # Only 2 spawned due to cap = 2
    assert rec.spawned == [t1.id, t2.id]
    t3_row = store.get_task(t3.id)
    assert t3_row.state is State.QUEUED
    assert "waiting for Gemini quota concurrency (2/2)" in reasons.text(t3_row.state_reason)


def test_turn_quota_error_requeue_and_fresh_start_after_reset(store, tmp_path):
    """Turn failing with quota error requeues to State.QUEUED and restarts fresh after reset."""
    from ahub import engine

    project = make_project(tmp_path, max_parallel=2)
    res_json = '{"summary": "ok", "status": "done"}'
    fake = install_fake(store, [
        {"session": "ses_q", "steps": [
            {"event": {"type": "error", "message": "quota limit reached: 429 RESOURCE_EXHAUSTED"}}],
         "exit": 1},
        {"session": "ses_new", "steps": [{"write": {"path": ".ahub/report.md", "text": "## Суть\nok\n"}},
                                         {"write": {"path": ".ahub/result.json", "text": res_json}},
                                         {"event": {"type": "text", "text": "ok"}}]}
    ])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")

    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.12, now + 10_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="scout task",
                                           model="gemini-flash"), project, collect=False)

    # 1. Run engine turn: fails with Outcome.QUOTA
    eng = engine.Engine(store, project, t.id, sleep=lambda s: None)
    settled = eng.run()

    # Requeued to QUEUED, not NEEDS_DECISION; the poisoned session is abandoned (fresh next time)
    assert settled.state is State.QUEUED
    assert "waiting for Gemini quota" in settled.reason
    task_after = store.get_task(t.id)
    assert task_after.state is State.QUEUED
    assert task_after.limits.get("fresh_session") is True

    # 2. While quota is below threshold and before reset, service does not start it
    s, rec = svc(store, project, tmp_path)
    with patch("ahub.quota.now_ms", return_value=now + 5000):
        s.tick()
        assert rec.spawned == []

    # 3. After reset_at passes and quota is restored, service launches it
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.60, now + 20_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])
    with patch("ahub.quota.now_ms", return_value=now + 15_000):
        s.tick()
        assert rec.spawned == [t.id]

    # 4. Engine runs again: a new session (the old one would repeat the quota error), same task done
    eng2 = engine.Engine(store, project, t.id, sleep=lambda s: None)
    settled2 = eng2.run()
    assert settled2.state is State.DONE
    assert fake.calls[-1]["session_id"] is None


def test_reviewer_quota_error_on_code_task_swaps_panel_not_executor(store, tmp_path):
    """A reviewer quota error on a code task moves only the panel entry to the fallback."""
    from ahub import engine

    project = make_project(tmp_path, max_parallel=2)
    fake = install_fake(store, [])
    ensure_model(store, "gemini-exec", "fake", "gemini-exec")
    ensure_model(store, "gemini-rev", "fake", "gemini-rev")
    ensure_model(store, "bunny", "fake", "bunny")

    set_hub_quota('[quota]\nfallback_reviewer = "bunny"\n')

    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.12, now + 3600_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="gemini-exec", review_models=["gemini-rev"],
                                           review_rounds=1, paths=["core/**"],
                                           accept=["tests/test_a.py"]),
                     project, collect=False)

    eng = engine.Engine(store, project, t.id, sleep=lambda s: None)
    state, _reason = eng._outcome_to_state(
        RunResult(Outcome.QUOTA, None, error="quota limit reached: 429 RESOURCE_EXHAUSTED"),
        role=Role.REVIEWER, model_alias="gemini-rev")

    assert state is State.QUEUED
    after = store.get_task(t.id)
    assert after.executor == "gemini-exec"
    assert after.review.get("models") == ["bunny"]
    events = store.events(task_id=t.id)
    model_ev = next((e for e in events if e.kind == Ev.MODEL_CHANGED.value), None)
    assert model_ev is not None
    assert model_ev.payload.get("from") == "gemini-rev"
    assert "Gemini 5h quota 12% → running on bunny" in model_ev.payload.get("text", "")


def test_turn_quota_error_moves_to_fallback(store, tmp_path, monkeypatch):
    """Turn failing with quota error moves to fallback if configured."""
    from ahub import engine

    project = make_project(tmp_path, max_parallel=2)
    fake = install_fake(store, [
        {"session": "ses_q", "steps": [
            {"event": {"type": "error", "message": "quota limit reached: 429 RESOURCE_EXHAUSTED"}}],
         "exit": 1},
    ])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")
    ensure_model(store, "bunny", "fake", "bunny")

    set_hub_quota('[quota]\nfallback = "bunny"\n')

    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.12, now + 10_000, lambda m: "gemini" in m.lower()),
        QuotaBucket("Gemini", "weekly", 0.60, now + 86400_000, lambda m: "gemini" in m.lower()),
    ])

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="scout task",
                                           model="gemini-flash"), project, collect=False)

    eng = engine.Engine(store, project, t.id, sleep=lambda s: None)
    settled = eng.run()

    assert settled.state is State.QUEUED
    task_after = store.get_task(t.id)
    assert task_after.executor == "bunny"

    events = store.events(task_id=t.id)
    model_ev = next((e for e in events if e.kind == Ev.MODEL_CHANGED.value), None)
    assert model_ev is not None
    assert "Gemini 5h quota 12% → running on bunny" in model_ev.payload.get("text", "")


def test_turn_quota_error_without_buckets_requeues_with_error(store, tmp_path):
    """Quota error on a provider with no quota windows: back to the queue with the provider error."""
    from ahub import engine

    project = make_project(tmp_path, max_parallel=2)
    install_fake(store, [
        {"session": "ses_a", "steps": [
            {"event": {"type": "error", "message": "quota limit reached: 429 RESOURCE_EXHAUSTED"}}],
         "exit": 1},
        {"session": "ses_b", "steps": [
            {"event": {"type": "error", "message": "quota limit reached: 429 RESOURCE_EXHAUSTED"}}],
         "exit": 1},
    ])
    ensure_model(store, "plain", "fake", "plain-model")
    ensure_model(store, "bunny", "fake", "bunny")

    t1 = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="first",
                                            model="plain"), project, collect=False)
    settled = engine.Engine(store, project, t1.id, sleep=lambda s: None).run()
    assert settled.state is State.QUEUED
    assert "provider quota" in settled.reason
    assert "Gemini" not in settled.reason

    set_hub_quota('[quota]\nfallback = "bunny"\n')
    t2 = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="second",
                                            model="plain"), project, collect=False)
    settled2 = engine.Engine(store, project, t2.id, sleep=lambda s: None).run()
    assert settled2.state is State.QUEUED
    assert store.get_task(t2.id).executor == "bunny"
    events = store.events(task_id=t2.id)
    model_ev = next((e for e in events if e.kind == Ev.MODEL_CHANGED.value), None)
    assert model_ev is not None
    assert "running on bunny" in model_ev.payload.get("text", "")


def test_providers_cli_shows_buckets_text_and_json(capsys, monkeypatch):
    """`ahub providers` prints the bucket lines; --json carries the buckets."""
    from ahub import cli, doctor

    today = datetime.now(timezone.utc).date()  # reset today: the line shows bare "17:16"
    reset_ms = int(datetime(today.year, today.month, today.day, 17, 16, tzinfo=timezone.utc).timestamp() * 1000)
    buckets = [QuotaBucket("Gemini", "5h", 0.29, reset_ms, lambda m: True),
               QuotaBucket("Gemini", "weekly", 0.60, reset_ms + 86400_000, lambda m: True)]

    class _Q:
        def quota(self, force: bool = False) -> list:
            return buckets

    monkeypatch.setattr(doctor, "provider_states",
                        lambda *a, **k: [doctor.ProviderState("agy", True, True, detail="found", note="", hint="")])
    monkeypatch.setattr("ahub.providers.get", lambda name: _Q())

    assert cli.main(["providers"]) == 0
    assert "Gemini · 5h 29% (resets 17:16) · week 60%" in capsys.readouterr().out

    assert cli.main(["--json", "providers"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(b["group"] == "Gemini" and b["window"] == "5h" and b["remaining"] == 0.29
               for b in data["buckets"])


def test_reviewer_quota_below_threshold_with_fallback(store, tmp_path, monkeypatch):

    """Reviewer model below quota threshold switches to fallback."""
    from ahub import engine

    project = make_project(tmp_path, max_parallel=2)
    fake = install_fake(store, [
        {"session": "ses_rv", "steps": [
            {"write": {"path": ".ahub/review_r1_bunny.json",
                       "text": '{"verdict": "approve", "findings": [], "notes": "good"}'}},
            {"event": {"type": "text", "text": "approved"}}
        ]}
    ])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")
    ensure_model(store, "bunny", "fake", "bunny")

    set_hub_quota('[quota]\nfallback_reviewer = "bunny"\n')

    now = int(time.time() * 1000)
    fake.set_quota([
        QuotaBucket("Gemini", "5h", 0.10, now + 10_000, lambda m: "gemini" in m.lower()),
    ])

    # Review task specifying gemini-flash as model
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.REVIEW, title="review task",
                                           model="gemini-flash", review_input="core/a.py"),
                     project, collect=False)

    eng = engine.Engine(store, project, t.id, sleep=lambda s: None)
    settled = eng.run()

    assert settled.state is State.DONE
    events = store.events(task_id=t.id)
    model_ev = next((e for e in events if e.kind == Ev.MODEL_CHANGED.value), None)
    assert model_ev is not None
    assert "Gemini 5h quota 10% → running on bunny" in model_ev.payload.get("text", "")


def test_visibility_providers_doctor_home(store, tmp_path):
    """Visibility in ahub providers, ahub doctor, and ahub home."""
    today = datetime.now(timezone.utc).date()  # reset today: the header shows bare "17:16"
    reset_ms = int(datetime(today.year, today.month, today.day, 17, 16, tzinfo=timezone.utc).timestamp() * 1000)
    b_5h = QuotaBucket("Gemini", "5h", 0.29, reset_ms, lambda m: True)
    b_week = QuotaBucket("Gemini", "weekly", 0.60, reset_ms + 86400_000, lambda m: True)
    buckets = [b_5h, b_week]

    # 1. format_bucket_group
    line = quota.format_bucket_group("Gemini", buckets)
    assert "Gemini · 5h 29% (resets 17:16) · week 60%" == line

    # 2. format_5h_line
    line_5h = quota.format_5h_line(b_5h)
    assert "Gemini 5h 29% (resets 17:16)" == line_5h

    # 3. doctor.Check with buckets
    chk = Check("agy", True, f"agy found\n{line}", "", buckets=[b.to_dict() for b in buckets])
    assert len(chk.buckets) == 2
    assert chk.buckets[0]["window"] == "5h"

    # 4. home header shows Gemini 5h when active task uses Gemini
    project = make_project(tmp_path)
    fake = install_fake(store, [])
    ensure_model(store, "gemini-flash", "fake", "gemini-flash")
    fake.set_quota(buckets)

    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="code task",
                                           model="gemini-flash", paths=["core/**"],
                                           accept=["tests/test_a.py"]), project, collect=False)
    transitions.move(store, t.id, State.PREPARING)
    transitions.move(store, t.id, State.WORKING)

    header_text = home_text()
    assert "Gemini 5h 29% (resets 17:16)" in header_text

    # 5. home_data carries buckets
    h_data = home_data()
    assert "buckets" in h_data
    assert any(b["group"] == "Gemini" and b["remaining"] == 0.29 for b in h_data["buckets"])
